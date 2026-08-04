"""The broadcast.

One station picks what plays and when; every guild is a receiver tuned to it,
so all servers hear the same song at the same moment — like a radio feed
rather than independent jukeboxes.

The station owns the clock: it advances on the track's duration (or a skip),
and guilds join mid-track by seeking to its current offset. It keeps airing to
whichever guilds are listening, but holds between tracks when nobody anywhere
is (see IDLE_PAUSE) rather than burning through the library unheard.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

import discord

from .config import Config
from .db import Database
from .player import QueueItem, Track

log = logging.getLogger(__name__)

# Fallback when a track has no known duration; the receiver finishing early is
# harmless, we just hold the slot.
DEFAULT_TRACK_SECONDS = 300


class Station:
    def __init__(self, bot, cfg: Config, db: Database) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db

        self.queue: deque[QueueItem] = deque()
        self.current: QueueItem | None = None
        self.volume = cfg.volume
        self.waiting_for_tracks = False
        self.started_at = 0.0
        self.active_playlist_ids: list[str] = []

        self._bag: list[str] = []
        self._recent: deque[str] = deque(maxlen=25)
        self._skip = asyncio.Event()
        self._listeners = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._presence: str | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        stored = await self.db.get_setting("volume")
        if stored:
            try:
                self.volume = max(0.0, min(2.0, float(stored)))
            except ValueError:
                pass
        self._task = self.bot.loop.create_task(self._run(), name="station")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @property
    def elapsed(self) -> float:
        if self.current is None:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    def listener_count(self) -> int:
        return sum(p.human_count() for p in self.bot.players.values())

    def notify_listeners(self) -> None:
        """Called by receivers when their voice channel population changes."""
        if not self.cfg.idle_pause or self.listener_count() > 0:
            self._listeners.set()
        else:
            self._listeners.clear()

    # ------------------------------------------------------------ track pick

    async def _refill_bag(self) -> None:
        self.active_playlist_ids = await self.db.resolve_active_playlist_ids()
        ids = await self.db.downloaded_ids(self.active_playlist_ids)
        random.shuffle(ids)
        if len(ids) > len(self._recent):
            recent = set(self._recent)
            ids = [i for i in ids if i not in recent] + [i for i in ids if i in recent]
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
            await self.db.mark_missing_file(video_id)
            log.warning("file missing for %s, marked for re-download", video_id)
        return track

    async def _next_item(self) -> QueueItem | None:
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
            track = await self._resolve(self._bag.pop())
            if track is not None:
                return QueueItem(track=track)
        return None

    # -------------------------------------------------------------- the loop

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                if self.cfg.idle_pause:
                    self.notify_listeners()
                    # Hold the broadcast between tracks while nobody listens,
                    # so an empty night doesn't burn through the library.
                    await self._listeners.wait()

                item = await self._next_item()
                if item is None:
                    self.waiting_for_tracks = True
                    await asyncio.sleep(15)
                    continue
                self.waiting_for_tracks = False

                self.current = item
                self.started_at = time.monotonic()
                self._skip.clear()
                log.info("station now playing: %s", item.track.title)
                await self.db.bump_play(item.track.video_id)
                self._recent.append(item.track.video_id)

                await self._broadcast()
                await self._set_presence(item.track.title)
                await self._hold(item)

                self.current = None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the broadcast must never die
                log.exception("station loop error")
                await asyncio.sleep(5)

    async def _hold(self, item: QueueItem) -> None:
        """Occupy the slot for the track's length, unless skipped."""
        seconds = item.track.duration or DEFAULT_TRACK_SECONDS
        remaining = max(1.0, seconds - self.elapsed)
        try:
            await asyncio.wait_for(self._skip.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            pass

    async def _broadcast(self) -> None:
        """Start the current track on every tuned-in receiver."""
        for player in list(self.bot.players.values()):
            try:
                await player.tune_in()
            except Exception:  # noqa: BLE001 - one bad guild must not stop the rest
                log.exception("failed to start playback in guild %s", player.guild.id)

    async def _set_presence(self, title: str) -> None:
        if title == self._presence:
            return
        self._presence = title
        try:
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.listening, name=title[:128]
                )
            )
        except discord.HTTPException as exc:
            log.debug("presence update failed: %s", exc)

    # -------------------------------------------------------------- controls

    def skip(self) -> bool:
        if self.current is None:
            return False
        self._skip.set()
        return True

    async def set_volume(self, value: float) -> float:
        self.volume = max(0.0, min(2.0, value))
        for player in self.bot.players.values():
            player.apply_volume(self.volume)
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
