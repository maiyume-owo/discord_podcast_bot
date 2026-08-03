"""24/7 shuffle player: voice connection, queue, and empty-channel handling."""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import discord

from .config import Config
from .db import Database

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Track:
    video_id: str
    title: str
    uploader: str | None
    duration: int | None
    path: Path

    @classmethod
    def from_row(cls, row: sqlite3.Row, audio_dir: Path) -> "Track | None":
        filename = row["filename"]
        if not filename:
            return None
        path = audio_dir / filename
        if not path.exists():
            return None
        return cls(
            video_id=row["video_id"],
            title=row["title"],
            uploader=row["uploader"],
            duration=row["duration"],
            path=path,
        )


@dataclass(slots=True)
class QueueItem:
    track: Track
    requester_id: int | None = None
    requester_name: str | None = None
    offset: float = 0.0


class GuildPlayer:
    """One per guild. Owns the voice connection and the playback loop."""

    def __init__(self, bot, cfg: Config, db: Database, guild: discord.Guild) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db
        self.guild = guild

        self.queue: deque[QueueItem] = deque()
        self.current: QueueItem | None = None
        self.volume = cfg.volume
        self.active_channel_id: int | None = cfg.voice_channel_id
        self.idle_since: float | None = None
        self.paused_for_idle = False
        self.last_connect_error: str | None = None
        self.waiting_for_tracks = False
        self.active_playlist_ids: list[str] = []

        self._bag: list[str] = []
        self._recent: deque[str] = deque(maxlen=25)
        self._requeue: QueueItem | None = None
        self._track_done = asyncio.Event()
        self._listeners = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        self._source: discord.PCMVolumeTransformer | None = None
        self._started_at = 0.0
        self._paused_total = 0.0
        self._paused_at: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._notified_no_channel = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        stored = await self.db.get_setting("volume")
        if stored:
            try:
                self.volume = max(0.0, min(2.0, float(stored)))
            except ValueError:
                pass
        self._tasks = [
            self.bot.loop.create_task(self._run(), name=f"player-{self.guild.id}"),
            self.bot.loop.create_task(self._watchdog(), name=f"watchdog-{self.guild.id}"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        vc = self.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass

    @property
    def voice_client(self) -> discord.VoiceClient | None:
        vc = self.guild.voice_client
        return vc if isinstance(vc, discord.VoiceClient) else None

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        vc = self.voice_client
        if vc is not None and vc.channel is not None:
            return vc.channel  # type: ignore[return-value]
        if self.active_channel_id:
            ch = self.guild.get_channel(self.active_channel_id)
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                return ch
        return None

    # ------------------------------------------------------------ connection

    def _candidate_channels(
        self, explicit: discord.abc.GuildChannel | None = None
    ) -> list[discord.VoiceChannel | discord.StageChannel]:
        """Preferred channel first, then configured fallbacks, then anything joinable."""
        ordered: list[discord.abc.GuildChannel | None] = [explicit]
        if self.cfg.voice_channel_id:
            ordered.append(self.guild.get_channel(self.cfg.voice_channel_id))
        for cid in self.cfg.fallback_channel_ids:
            ordered.append(self.guild.get_channel(cid))

        auto = [ch for ch in self.guild.voice_channels]
        auto.sort(key=lambda ch: (-self.human_count(ch), ch.position))
        ordered.extend(auto)

        out: list[discord.VoiceChannel | discord.StageChannel] = []
        seen: set[int] = set()
        for ch in ordered:
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                continue
            if ch.id in seen or not self._joinable(ch):
                continue
            seen.add(ch.id)
            out.append(ch)
        return out

    def _joinable(self, channel: discord.VoiceChannel | discord.StageChannel) -> bool:
        me = self.guild.me
        if me is None:
            return False
        perms = channel.permissions_for(me)
        if not (perms.connect and perms.speak):
            return False
        limit = getattr(channel, "user_limit", 0)
        if limit and len(channel.voice_states) >= limit and not perms.move_members:
            return False
        return True

    async def connect(
        self, channel: discord.abc.GuildChannel | None = None
    ) -> discord.VoiceClient | None:
        """Connect to `channel`, else the configured channel, else any fallback."""
        async with self._connect_lock:
            vc = self.voice_client
            if vc is not None and vc.is_connected() and channel is None:
                return vc

            for candidate in self._candidate_channels(channel):
                try:
                    vc = self.voice_client
                    if vc is not None and vc.is_connected():
                        await vc.move_to(candidate)
                    else:
                        vc = await candidate.connect(
                            timeout=20.0, reconnect=True, self_deaf=True
                        )
                    self.active_channel_id = candidate.id
                    self.last_connect_error = None
                    self._notified_no_channel = False
                    self._refresh_presence()
                    log.info("connected to voice channel #%s", candidate.name)
                    return vc
                except Exception as exc:  # noqa: BLE001 - try the next candidate
                    self.last_connect_error = f"#{candidate.name}: {exc}"
                    log.warning("could not join #%s: %s", candidate.name, exc)
                    await self._force_cleanup()
                    continue

            if not self._candidate_channels(channel):
                self.last_connect_error = "no voice channel is joinable (check permissions)"
            log.error("voice connect failed: %s", self.last_connect_error)
            await self._notify_connect_failure()
            return None

    async def _force_cleanup(self) -> None:
        vc = self.voice_client
        if vc is not None and not vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass

    async def _notify_connect_failure(self) -> None:
        if self._notified_no_channel or not self.cfg.text_channel_id:
            return
        self._notified_no_channel = True
        ch = self.bot.get_channel(self.cfg.text_channel_id)
        if isinstance(ch, discord.abc.Messageable):
            try:
                await ch.send(
                    "⚠️ I can't join any voice channel right now "
                    f"({self.last_connect_error}). I'll keep retrying every "
                    f"{self.cfg.reconnect_interval}s — or use `/summon` to pull me "
                    "into your channel."
                )
            except discord.HTTPException:
                pass

    # -------------------------------------------------------------- presence

    def human_count(self, channel: discord.abc.GuildChannel | None = None) -> int:
        """Non-bot members in the channel. Unknown members count as human."""
        channel = channel or self.channel
        if channel is None or not hasattr(channel, "voice_states"):
            return 0
        me_id = self.bot.user.id if self.bot.user else 0
        count = 0
        for user_id in channel.voice_states:
            if user_id == me_id:
                continue
            member = self.guild.get_member(user_id)
            if member is not None and member.bot:
                continue
            count += 1
        return count

    def _refresh_presence(self) -> None:
        """Pause when the channel empties, resume when someone comes back."""
        vc = self.voice_client
        listeners = self.human_count()

        if listeners > 0:
            self.idle_since = None
            self._listeners.set()
            if vc is not None and vc.is_paused() and self.paused_for_idle:
                vc.resume()
                self.paused_for_idle = False
                if self._paused_at is not None:
                    self._paused_total += time.monotonic() - self._paused_at
                    self._paused_at = None
                log.info("listener joined — resuming playback")
            return

        self._listeners.clear()
        if self.idle_since is None:
            self.idle_since = time.monotonic()
        if self.cfg.idle_pause and vc is not None and vc.is_playing():
            vc.pause()
            self.paused_for_idle = True
            self._paused_at = time.monotonic()
            log.info("channel is empty — pausing (staying connected)")

    def handle_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.guild.id != self.guild.id:
            return
        if self.bot.user and member.id == self.bot.user.id:
            # Moved or dragged out by a moderator.
            if after.channel is not None:
                self.active_channel_id = after.channel.id
        self._refresh_presence()

    @property
    def elapsed(self) -> float:
        if self.current is None:
            return 0.0
        paused = self._paused_total
        if self._paused_at is not None:
            paused += time.monotonic() - self._paused_at
        return max(0.0, self.current.offset + (time.monotonic() - self._started_at) - paused)

    # ----------------------------------------------------------- track picks

    async def _refill_bag(self) -> None:
        # Shuffle only across the playlists the owner has the bot running on.
        self.active_playlist_ids = await self.db.resolve_active_playlist_ids()
        ids = await self.db.downloaded_ids(self.active_playlist_ids)
        random.shuffle(ids)
        if len(ids) > len(self._recent):
            # Push recently played tracks towards the back of the new bag.
            recent = set(self._recent)
            head = [i for i in ids if i not in recent]
            tail = [i for i in ids if i in recent]
            ids = head + tail
        self._bag = ids
        log.info("reshuffled %d track(s)", len(ids))

    async def reshuffle(self) -> int:
        await self._refill_bag()
        return len(self._bag)

    @property
    def bag_size(self) -> int:
        return len(self._bag)

    async def _resolve(self, video_id: str) -> Track | None:
        row = await self.db.get_track(video_id)
        if row is None:
            return None
        track = Track.from_row(row, self.cfg.audio_dir)
        if track is None and row["status"] == "downloaded":
            # File vanished under us — queue it for re-download.
            await self.db.mark_missing_file(video_id)
            log.warning("file missing for %s, marked for re-download", video_id)
        return track

    async def _next_item(self) -> QueueItem | None:
        if self._requeue is not None:
            item, self._requeue = self._requeue, None
            return item
        while self.queue:
            item = self.queue.popleft()
            if item.track.path.exists():
                return item
            await self.db.mark_missing_file(item.track.video_id)

        for _ in range(3):
            if not self._bag:
                await self._refill_bag()
            if not self._bag:
                return None
            video_id = self._bag.pop()
            track = await self._resolve(video_id)
            if track is not None:
                return QueueItem(track=track)
        return None

    # ------------------------------------------------------------- main loop

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                vc = self.voice_client
                if vc is None or not vc.is_connected():
                    vc = await self.connect()
                if vc is None:
                    await asyncio.sleep(self.cfg.reconnect_interval)
                    continue

                self._refresh_presence()
                if self.cfg.idle_pause:
                    await self._listeners.wait()

                item = await self._next_item()
                if item is None:
                    self.waiting_for_tracks = True
                    await asyncio.sleep(15)
                    continue
                self.waiting_for_tracks = False

                await self._play(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("player loop error")
                await asyncio.sleep(5)

    async def _play(self, item: QueueItem) -> None:
        vc = self.voice_client
        if vc is None or not vc.is_connected():
            self._requeue = item
            return

        before = f"-ss {item.offset:.2f}" if item.offset > 1 else None
        try:
            audio = discord.FFmpegPCMAudio(
                str(item.track.path),
                before_options=before,
                options="-vn -loglevel error",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("ffmpeg failed for %s: %s", item.track.path, exc)
            await self.db.mark_failed(item.track.video_id, f"ffmpeg: {exc}")
            return

        source = discord.PCMVolumeTransformer(audio, volume=self.volume)
        self._source = source
        self.current = item
        self._started_at = time.monotonic()
        self._paused_total = 0.0
        self._paused_at = None
        self._track_done.clear()

        vc.play(source, after=self._after_play)
        log.info("now playing: %s", item.track.title)
        if item.offset <= 1:
            await self.db.bump_play(item.track.video_id)
            self._recent.append(item.track.video_id)

        # A paused stream still needs presence checks applied immediately.
        self._refresh_presence()

        await self._track_done.wait()
        self.current = None
        self._source = None

    def _after_play(self, error: Exception | None) -> None:
        if error:
            log.error("playback error: %s", error)
        self.bot.loop.call_soon_threadsafe(self._track_done.set)

    # -------------------------------------------------------------- watchdog

    async def _watchdog(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(self.cfg.watchdog_interval)
                vc = self.voice_client

                if vc is None or not vc.is_connected():
                    await self.connect()
                    continue

                self._refresh_presence()

                # Empty long enough: tear the stream down but stay in the channel.
                if (
                    self.cfg.idle_stop_after > 0
                    and self.idle_since is not None
                    and self.current is not None
                    and time.monotonic() - self.idle_since >= self.cfg.idle_stop_after
                    and (vc.is_paused() or vc.is_playing())
                ):
                    resume_at = max(0.0, self.elapsed - 2)
                    self._requeue = QueueItem(
                        track=self.current.track,
                        requester_id=self.current.requester_id,
                        requester_name=self.current.requester_name,
                        offset=resume_at,
                    )
                    self.paused_for_idle = False
                    log.info(
                        "idle for %ds — stopping stream, staying in #%s",
                        self.cfg.idle_stop_after,
                        getattr(self.channel, "name", "?"),
                    )
                    vc.stop()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("watchdog error")

    # -------------------------------------------------------------- controls

    def skip(self) -> bool:
        vc = self.voice_client
        if vc is None or not (vc.is_playing() or vc.is_paused()):
            return False
        self._skipping = True
        self._requeue = None
        vc.stop()
        return True

    async def set_volume(self, value: float) -> float:
        self.volume = max(0.0, min(2.0, value))
        if self._source is not None:
            self._source.volume = self.volume
        await self.db.set_setting("volume", str(self.volume))
        return self.volume

    def enqueue(self, item: QueueItem, front: bool = False) -> int:
        if len(self.queue) >= self.cfg.max_queue:
            raise ValueError(f"queue is full ({self.cfg.max_queue} tracks)")
        if front:
            self.queue.appendleft(item)
            return 1
        self.queue.append(item)
        return len(self.queue)

    def clear_queue(self) -> int:
        count = len(self.queue)
        self.queue.clear()
        return count

    def remove_at(self, position: int) -> QueueItem | None:
        if 1 <= position <= len(self.queue):
            item = self.queue[position - 1]
            del self.queue[position - 1]
            return item
        return None
