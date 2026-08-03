"""A receiver: one per guild.

Owns the voice connection (including the fallback chain and /summon), and
plays whatever the Station tells it to, seeking to the station's current
offset so every server stays in sync. It never chooses a track itself.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
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


class GuildPlayer:
    def __init__(self, bot, cfg: Config, db: Database, guild: discord.Guild) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db
        self.guild = guild

        self.active_channel_id: int | None = cfg.voice_channel_id
        self.idle_since: float | None = None
        self.last_connect_error: str | None = None

        self._source: discord.PCMVolumeTransformer | None = None
        self._playing_id: str | None = None
        self._connect_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task | None = None
        self._notified_no_channel = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._watchdog_task = self.bot.loop.create_task(
            self._watchdog(), name=f"watchdog-{self.guild.id}"
        )

    async def stop(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None
        vc = self.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass

    @property
    def station(self):
        return self.bot.station

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

        auto = list(self.guild.voice_channels)
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
                    log.info(
                        "[%s] connected to #%s", self.guild.name, candidate.name
                    )
                    await self.tune_in()
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

    def handle_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.guild.id != self.guild.id:
            return
        if self.bot.user and member.id == self.bot.user.id and after.channel is not None:
            self.active_channel_id = after.channel.id
        self.bot.loop.create_task(self.refresh())

    async def refresh(self) -> None:
        """React to the channel filling up or emptying out."""
        listeners = self.human_count()
        if listeners > 0:
            self.idle_since = None
            # Someone's here but we're silent (or on the wrong track) — resync.
            if not self.is_playing_current():
                await self.tune_in()
        elif self.idle_since is None:
            self.idle_since = time.monotonic()
        self.station.notify_listeners()

    # -------------------------------------------------------------- playback

    def is_playing_current(self) -> bool:
        vc = self.voice_client
        current = self.station.current
        if vc is None or not vc.is_connected() or current is None:
            return False
        return (
            (vc.is_playing() or vc.is_paused())
            and self._playing_id == current.track.video_id
        )

    async def tune_in(self) -> None:
        """Play whatever the station is airing, from its current offset."""
        vc = self.voice_client
        item = self.station.current
        if vc is None or not vc.is_connected() or item is None:
            return
        if self.cfg.idle_pause and self.human_count() == 0:
            self.stop_audio()  # nobody here: hold the channel, make no sound
            return
        if self.is_playing_current():
            return

        offset = self.station.elapsed
        if item.track.duration and offset >= item.track.duration - 1:
            return  # track is basically over; wait for the next one

        before = f"-ss {offset:.2f}" if offset > 1 else None
        try:
            audio = discord.FFmpegPCMAudio(
                str(item.track.path),
                before_options=before,
                options="-vn -loglevel error",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("ffmpeg failed for %s: %s", item.track.path, exc)
            return

        if vc.is_playing() or vc.is_paused():
            vc.stop()
        source = discord.PCMVolumeTransformer(audio, volume=self.station.volume)
        self._source = source
        self._playing_id = item.track.video_id
        vc.play(source, after=self._after_play)
        log.debug(
            "[%s] tuned in to %s at %.1fs", self.guild.name, item.track.title, offset
        )

    def stop_audio(self) -> None:
        vc = self.voice_client
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        self._playing_id = None
        self._source = None

    def apply_volume(self, volume: float) -> None:
        if self._source is not None:
            self._source.volume = volume

    def _after_play(self, error: Exception | None) -> None:
        if error:
            log.error("playback error in %s: %s", self.guild.id, error)
        self._playing_id = None

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

                listeners = self.human_count()
                if listeners == 0:
                    if self.idle_since is None:
                        self.idle_since = time.monotonic()
                    # Empty for long enough: tear the stream down, stay connected.
                    elapsed = time.monotonic() - self.idle_since
                    if (
                        self.cfg.idle_pause
                        and elapsed >= self.cfg.idle_stop_after
                        and (vc.is_playing() or vc.is_paused())
                    ):
                        log.info(
                            "[%s] empty for %ds — muting, holding #%s",
                            self.guild.name,
                            self.cfg.idle_stop_after,
                            getattr(self.channel, "name", "?"),
                        )
                        self.stop_audio()
                else:
                    self.idle_since = None
                    # Drifted off-air (track ended early, ffmpeg died): resync.
                    if not self.is_playing_current():
                        await self.tune_in()
                self.station.notify_listeners()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("watchdog error")
