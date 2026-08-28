"""A receiver: one per guild, paired with that guild's own Station.

Owns the voice connection and plays whatever its station tells it to, seeking
to the station's current offset so a late joiner lands mid-track. It never
chooses a track itself — that is the station's job.

Joining is deliberately narrow: on its own the bot only ever enters a 24/7
channel from VOICE_CHANNEL_IDS. Any other channel needs a human — /join,
/summon, or dragging the bot in — and that choice is remembered ("pinned")
only until that channel empties out.
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

# How long /leave keeps the bot out before the usual auto-rejoin resumes.
# Without it, "disconnect" and "reconnect immediately" are the same command.
LEAVE_GRACE_SECONDS = 60

# Voice close codes that mean "this session is dead, start a fresh one".
# 4006 = session no longer valid, 4009 = session timeout, 4014 = disconnected.
STALE_SESSION_CODES = {4006, 4009, 4014}


def _describe_error(exc: Exception) -> str:
    """asyncio.TimeoutError and friends stringify to "", which logs as nothing."""
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out (no response from Discord voice)"
    return type(exc).__name__


def _is_stale_session(exc: Exception) -> bool:
    if isinstance(exc, discord.ConnectionClosed) and exc.code in STALE_SESSION_CODES:
        return True
    return "already connected" in str(exc).lower()


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

        # Imported here, not at module scope: Station needs Track/QueueItem
        # from this module, so a top-level import would be circular.
        from .station import Station

        self.station = Station(bot, cfg, db, self)

        self.active_channel_id: int | None = None
        # Set when a human put us somewhere that isn't a 24/7 channel. Survives
        # reconnects (so a dropped connection returns where it was) and is
        # dropped once that channel empties, sending us back to 24/7 duty.
        self.pinned_channel_id: int | None = None
        self.idle_since: float | None = None
        self.last_connect_error: str | None = None
        # Set by /leave: until then, being out of voice is intentional.
        self.rejoin_blocked_until: float = 0.0

        self._source: discord.PCMVolumeTransformer | None = None
        self._playing_id: str | None = None
        self._connect_lock = asyncio.Lock()
        self._watchdog_task: asyncio.Task | None = None
        self._rejoin_task: asyncio.Task | None = None
        self._stopping = False
        self._notified_no_channel = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.station.start()
        self._watchdog_task = self.bot.loop.create_task(
            self._watchdog(), name=f"watchdog-{self.guild.id}"
        )

    async def stop(self) -> None:
        self._stopping = True
        await self.station.stop()
        if self._rejoin_task is not None:
            self._rejoin_task.cancel()
            self._rejoin_task = None
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

    def is_home(self, channel_id: int | None) -> bool:
        """Is this one of the configured 24/7 channels?"""
        return channel_id is not None and channel_id in self.cfg.voice_channel_ids

    def home_channels(self) -> list[discord.VoiceChannel | discord.StageChannel]:
        """The 24/7 channels that exist in this guild and that we can join.

        More than one may be configured; a guild can only hold us in one at a
        time, so the one with listeners wins, ties broken by config order.
        """
        found: list[tuple[int, discord.VoiceChannel | discord.StageChannel]] = []
        for rank, cid in enumerate(self.cfg.voice_channel_ids):
            ch = self.guild.get_channel(cid)
            if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                continue
            if not self._joinable(ch):
                continue
            found.append((rank, ch))
        found.sort(key=lambda pair: (-self.human_count(pair[1]), pair[0]))
        return [ch for _, ch in found]

    def _candidate_channels(
        self, explicit: discord.abc.GuildChannel | None = None
    ) -> list[discord.VoiceChannel | discord.StageChannel]:
        """Where we may connect, best first.

        Asked for a specific channel, that is the only answer — no wandering
        off to a second choice. Otherwise: the channel a human pinned us to
        (so a dropped connection comes back), then the 24/7 channels. Never
        anything else; if nothing here is available we simply stay out.
        """
        if explicit is not None:
            if isinstance(
                explicit, (discord.VoiceChannel, discord.StageChannel)
            ) and self._joinable(explicit):
                return [explicit]
            return []

        ordered: list[discord.abc.GuildChannel | None] = []
        if self.pinned_channel_id:
            ordered.append(self.guild.get_channel(self.pinned_channel_id))
        ordered.extend(self.home_channels())

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
        """Join `channel` if given, else the 24/7 channel (or a pinned one).

        Returns None — quietly — when there is nowhere we're allowed to be.
        A guild without a 24/7 channel is a normal state, not a failure: we
        wait there for a /join rather than picking a room ourselves.
        """
        async with self._connect_lock:
            vc = self.voice_client
            if vc is not None and vc.is_connected() and channel is None:
                return vc

            candidates = self._candidate_channels(channel)
            if not candidates:
                if channel is not None:
                    self.last_connect_error = (
                        f"#{getattr(channel, 'name', channel.id)} isn't joinable "
                        "(check Connect/Speak permissions)"
                    )
                    log.warning("voice connect failed: %s", self.last_connect_error)
                    return None
                self.last_connect_error = (
                    "no 24/7 channel available here — waiting for /join"
                )
                log.debug("[%s] %s", self.guild.name, self.last_connect_error)
                return None

            for candidate in candidates:
                for attempt in (1, 2):
                    try:
                        vc = self.voice_client
                        if vc is not None and vc.is_connected():
                            await vc.move_to(candidate)
                        else:
                            # Never call connect() with a half-open client still
                            # registered — Discord answers "Already connected".
                            await self._force_cleanup()
                            # reconnect=False on purpose: discord.py's internal
                            # retry reuses the same voice session, so a 4006
                            # ("session no longer valid") loops forever and
                            # never reaches our handler. Letting it fail out
                            # means we tear down and come back with a fresh
                            # session; the watchdog covers mid-stream drops.
                            vc = await candidate.connect(
                                timeout=20.0, reconnect=False, self_deaf=True
                            )
                        self.active_channel_id = candidate.id
                        self._remember(candidate.id)
                        self.last_connect_error = None
                        self._notified_no_channel = False
                        log.info(
                            "[%s] connected to #%s", self.guild.name, candidate.name
                        )
                        await self.tune_in()
                        return vc
                    except Exception as exc:  # noqa: BLE001 - try the next candidate
                        reason = _describe_error(exc)
                        self.last_connect_error = f"#{candidate.name}: {reason}"
                        log.warning("could not join #%s: %s", candidate.name, reason)
                        # A stale session poisons every later attempt, so always
                        # tear down fully before moving on.
                        await self._force_cleanup()
                        if attempt == 1 and _is_stale_session(exc):
                            log.info("stale voice session — retrying #%s", candidate.name)
                            await asyncio.sleep(2)
                            continue
                        break

            log.error("voice connect failed: %s", self.last_connect_error)
            if channel is None:  # a failed /join already answers the user
                await self._notify_connect_failure()
            return None

    async def _force_cleanup(self) -> None:
        """Drop any voice client, connected or not, and let Discord release it.

        discord.py can leave a client registered on the guild after a failed or
        timed-out handshake; it still answers is_connected(), so a conditional
        teardown skips it and every later connect fails with "Already
        connected to a voice channel".
        """
        vc = self.guild.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                vc.cleanup()
            except Exception:  # noqa: BLE001
                pass
        # Tell the gateway we've left, even if no client object survived. A
        # ghost session server-side is what makes every reconnect answer 4006,
        # and only a voice state update clears it.
        try:
            await self.guild.change_voice_state(channel=None)
        except Exception:  # noqa: BLE001
            pass
        self._playing_id = None
        self._source = None
        await asyncio.sleep(1.0)  # Discord needs a moment to free the session

    def block_rejoin(self, seconds: float = LEAVE_GRACE_SECONDS) -> None:
        """Stay out of voice for a while — /leave asked us to."""
        self.rejoin_blocked_until = time.monotonic() + seconds

    def _rejoin_allowed(self) -> bool:
        return not self._stopping and time.monotonic() >= self.rejoin_blocked_until

    async def _rejoin_now(self) -> None:
        """Come straight back to a 24/7 channel after being thrown out."""
        try:
            await asyncio.sleep(1)  # let Discord settle the voice state first
            if not self._rejoin_allowed() or self.bot.is_closed():
                return
            vc = self.voice_client
            if vc is not None and vc.is_connected():
                return  # something already brought us back
            log.info("[%s] disconnected from voice — rejoining", self.guild.name)
            await self.connect()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("rejoin failed")

    def _remember(self, channel_id: int) -> None:
        """Pin a non-24/7 channel so a reconnect returns to it; a 24/7 channel
        clears the pin — we're back on normal duty."""
        self.pinned_channel_id = None if self.is_home(channel_id) else channel_id

    async def _unpin_and_return_home(self) -> None:
        """Drop a stale pin and go back to 24/7 duty, or off entirely."""
        self.pinned_channel_id = None
        vc = self.voice_client
        home = self.home_channels()
        if home:
            await self.connect(home[0])
            return
        if vc is not None:
            log.info("[%s] leaving — no 24/7 channel to fall back to", self.guild.name)
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
            self.active_channel_id = None
            self.stop_audio()

    async def _notify_connect_failure(self) -> None:
        if self._notified_no_channel or not self.cfg.text_channel_id:
            return
        self._notified_no_channel = True
        ch = self.bot.get_channel(self.cfg.text_channel_id)
        if isinstance(ch, discord.abc.Messageable):
            try:
                await ch.send(
                    "⚠️ I can't join my 24/7 channel right now "
                    f"({self.last_connect_error}). I'll keep retrying every "
                    f"{self.cfg.reconnect_interval}s — or use `/join` to pull me "
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
        if self.bot.user and member.id == self.bot.user.id:
            if after.channel is not None:
                # Dragged into a channel by a moderator: that's a human choosing
                # for us, so adopt it exactly like a /join.
                self.active_channel_id = after.channel.id
                self._remember(after.channel.id)
            elif before.channel is not None:
                # Kicked from voice, or the connection dropped. Head back to a
                # 24/7 channel instead of waiting out the watchdog interval —
                # unless we're mid-connect (our own teardown fires this too),
                # shutting down, or /leave just asked us to stay out.
                self.active_channel_id = None
                self.pinned_channel_id = None
                if not self._connect_lock.locked() and self._rejoin_allowed():
                    self._rejoin_task = self.bot.loop.create_task(
                        self._rejoin_now(), name=f"rejoin-{self.guild.id}"
                    )
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
        # Join the 24/7 channel as soon as we're up, rather than after the
        # first watchdog interval. No-op in a guild that has none.
        await self.connect()
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(self.cfg.watchdog_interval)
                vc = self.voice_client
                if vc is None or not vc.is_connected():
                    if self._rejoin_allowed():
                        await self.connect()
                    continue

                listeners = self.human_count()
                if listeners == 0:
                    if self.idle_since is None:
                        self.idle_since = time.monotonic()
                    elapsed = time.monotonic() - self.idle_since
                    # A channel a human sent us to is only ours while someone is
                    # in it. Once it's been empty a while, the pin expires and
                    # we go back to the 24/7 channel (or leave, if there is none).
                    if (
                        self.pinned_channel_id is not None
                        and elapsed >= self.cfg.idle_stop_after
                    ):
                        log.info(
                            "[%s] #%s empty for %ds — releasing it",
                            self.guild.name,
                            getattr(self.channel, "name", "?"),
                            self.cfg.idle_stop_after,
                        )
                        await self._unpin_and_return_home()
                        self.station.notify_listeners()
                        continue
                    # Several 24/7 channels can be configured, but we can only
                    # hold one per guild — sit where the listeners actually are.
                    if self.pinned_channel_id is None:
                        best = next(iter(self.home_channels()), None)
                        if (
                            best is not None
                            and best.id != self.active_channel_id
                            and self.human_count(best) > 0
                        ):
                            log.info(
                                "[%s] moving to busier 24/7 channel #%s",
                                self.guild.name,
                                best.name,
                            )
                            await self.connect(best)
                            self.station.notify_listeners()
                            continue
                    # Empty for long enough: tear the stream down, stay connected.
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
