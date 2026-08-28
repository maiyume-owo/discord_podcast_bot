"""What plays, and when — one station per server.

Each guild gets its own station: its own queue, shuffle bag, clock and volume.
A skip or a request in one server changes nothing in any other. What *is*
shared is everything below playback — the library, the playlists in rotation
and the downloads — because those are one copy on disk for every server.

The station owns the clock: it advances on the track's duration (or a skip),
and a listener joining mid-track hears it from the station's current offset.
It holds between tracks while its own guild has nobody listening (see
IDLE_PAUSE) rather than burning through the library unheard.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque

from .config import Config
from .db import Database
from .player import QueueItem, Track

log = logging.getLogger(__name__)

# Fallback when a track has no known duration; the receiver finishing early is
# harmless, we just hold the slot.
DEFAULT_TRACK_SECONDS = 300


class Station:
    def __init__(self, bot, cfg: Config, db: Database, player) -> None:
        self.bot = bot
        self.cfg = cfg
        self.db = db
        # The one receiver this station plays to. Guild-scoped from here down.
        self.player = player
        self.guild = player.guild

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

    # ------------------------------------------------------------- lifecycle

    @property
    def _volume_key(self) -> str:
        return f"volume:{self.guild.id}"

    async def start(self) -> None:
        # This guild's saved level, else the level from before volume went
        # per-guild, else VOLUME from the environment.
        stored = await self.db.get_setting(self._volume_key) or await self.db.get_setting(
            "volume"
        )
        if stored:
            try:
                self.volume = max(0.0, min(2.0, float(stored)))
            except ValueError:
                pass
        self._task = self.bot.loop.create_task(
            self._run(), name=f"station-{self.guild.id}"
        )

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
        """Humans hearing *this* station."""
        return self.player.human_count()

    def notify_listeners(self) -> None:
        """Called by the receiver when its voice channel population changes."""
        if not self.cfg.idle_pause or self.listener_count() > 0:
            self._listeners.set()
        else:
            self._listeners.clear()

    # ------------------------------------------------------------ track pick

    async def _refill_bag(self) -> None:
        self.active_playlist_ids = await self.db.resolve_active_playlist_ids(
            self.guild.id
        )
        ids = await self.db.downloaded_ids(self.active_playlist_ids)
        random.shuffle(ids)
        if len(ids) > len(self._recent):
            recent = set(self._recent)
            ids = [i for i in ids if i not in recent] + [i for i in ids if i in recent]
        self._bag = ids
        log.info("[%s] reshuffled %d track(s)", self.guild.name, len(ids))

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
                    # Hold between tracks while nobody in this guild listens,
                    # so an empty night doesn't burn through the library.
                    if not self._listeners.is_set():
                        # Genuinely idle, not just between tracks: stop
                        # claiming to play. Back-to-back tracks skip this, so
                        # the presence isn't cleared and re-set every song.
                        await self.bot.refresh_presence()
                        await self._listeners.wait()

                item = await self._next_item()
                if item is None:
                    self.waiting_for_tracks = True
                    await self.bot.refresh_presence()
                    await asyncio.sleep(15)
                    continue
                self.waiting_for_tracks = False

                self.current = item
                self.started_at = time.monotonic()
                self._skip.clear()
                log.info("[%s] now playing: %s", self.guild.name, item.track.title)
                await self.db.bump_play(item.track.video_id)
                self._recent.append(item.track.video_id)

                await self._broadcast()
                # Presence is one status for the whole bot, so it is decided
                # across stations rather than by whichever one started last.
                await self.bot.refresh_presence()
                await self._hold(item)

                self.current = None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one guild's station must never die
                log.exception("[%s] station loop error", self.guild.name)
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
        """Start the current track on this station's receiver."""
        try:
            await self.player.tune_in()
        except Exception:  # noqa: BLE001 - a bad track must not kill the loop
            log.exception("[%s] failed to start playback", self.guild.name)

    # -------------------------------------------------------------- controls

    def skip(self) -> bool:
        if self.current is None:
            return False
        self._skip.set()
        return True

    async def set_volume(self, value: float) -> float:
        self.volume = max(0.0, min(2.0, value))
        self.player.apply_volume(self.volume)
        await self.db.set_setting(self._volume_key, str(self.volume))
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
