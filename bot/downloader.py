"""Playlist indexing + mp3 downloading, both via yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

UNAVAILABLE_TITLES = {
    "[Private video]",
    "[Deleted video]",
    "[Unavailable video]",
    "Private video",
    "Deleted video",
}


def parse_playlist_id(value: str) -> str | None:
    """Accept a full URL or a bare playlist id."""
    value = (value or "").strip()
    if not value:
        return None
    if "://" in value or value.startswith("www."):
        url = value if "://" in value else f"https://{value}"
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "list" in params and params["list"]:
            return params["list"][0]
        return None
    return value if _ID_RE.match(value) else None


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


@dataclass
class SyncReport:
    playlists: int = 0
    discovered: int = 0
    downloaded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def summary(self) -> str:
        bits = [
            f"{self.playlists} playlist(s)",
            f"{self.discovered} new track(s)",
            f"{self.downloaded} downloaded",
        ]
        if self.failed:
            bits.append(f"{self.failed} failed")
        return ", ".join(bits)


class Downloader:
    """Reads playlists, then pulls anything not on disk yet."""

    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(cfg.download_concurrency)
        self.state = "idle"
        self.current: str | None = None
        self.remaining = 0
        self.last_report: SyncReport | None = None
        self.last_run: float | None = None
        self.on_new_tracks: Any = None  # optional async callback()

    # ------------------------------------------------------------ yt-dlp opts

    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "cachedir": str(self.cfg.cache_dir),
            "retries": 5,
            "socket_timeout": 30,
            "logger": _YtdlLogger(),
        }
        if self.cfg.cookies_file:
            opts["cookiefile"] = str(self.cfg.cookies_file)
        elif self.cfg.cookies_from_browser:
            spec = self.cfg.cookies_from_browser.split(":")
            opts["cookiesfrombrowser"] = tuple(spec) + (None,) * (4 - len(spec))
        return opts

    def _flat_opts(self) -> dict[str, Any]:
        opts = self._base_opts()
        opts.update(
            {
                "extract_flat": "in_playlist",
                "skip_download": True,
                "ignoreerrors": True,
            }
        )
        return opts

    def _download_opts(self) -> dict[str, Any]:
        opts = self._base_opts()
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": str(self.cfg.audio_dir / "%(id)s.%(ext)s"),
                "ignoreerrors": False,
                "overwrites": False,
                "concurrent_fragment_downloads": 2,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": self.cfg.audio_quality,
                    },
                    {"key": "FFmpegMetadata", "add_metadata": True},
                ],
            }
        )
        return opts

    # ------------------------------------------------------------------ sync

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def sync(self, retry_failed: bool = False) -> SyncReport:
        if self._lock.locked():
            raise RuntimeError("a sync is already running")
        async with self._lock:
            report = SyncReport()
            self.state = "indexing"
            try:
                if retry_failed:
                    await self.db.reset_failures()

                for row in await self.db.get_playlists(enabled_only=True):
                    report.playlists += 1
                    try:
                        report.discovered += await self._index_playlist(
                            row["id"], row["url"]
                        )
                    except Exception as exc:  # noqa: BLE001 - surfaced to the user
                        msg = f"{row['id']}: {exc}"
                        log.warning("playlist index failed %s", msg)
                        report.errors.append(msg)
                        await self.db.update_playlist_sync(
                            row["id"], None, 0, str(exc)[:300]
                        )

                self.state = "downloading"
                await self._download_pending(report)
            finally:
                self.state = "idle"
                self.current = None
                self.remaining = 0
                report.finished_at = time.time()
                self.last_report = report
                self.last_run = report.finished_at
            if report.downloaded and self.on_new_tracks:
                try:
                    await self.on_new_tracks()
                except Exception:  # noqa: BLE001
                    log.exception("on_new_tracks callback failed")
            return report

    async def describe_playlist(self, playlist_id: str) -> tuple[str, int]:
        """(title, item_count) — used by /playlist add to validate input."""
        info = await asyncio.to_thread(self._extract_flat, playlist_url(playlist_id))
        return info.get("title") or playlist_id, len(info.get("entries") or [])

    def _extract_flat(self, url: str) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(self._flat_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError(
                "yt-dlp returned nothing — the playlist may not exist, or may be "
                "private (a cookies.txt would be needed to read it)."
            )
        return info

    async def _index_playlist(self, playlist_id: str, url: str) -> int:
        info = await asyncio.to_thread(self._extract_flat, url)
        entries = [e for e in (info.get("entries") or []) if e]
        seen: list[str] = []
        links: list[tuple[str, int]] = []
        new = 0

        for position, entry in enumerate(entries):
            video_id = entry.get("id")
            if not video_id:
                continue
            title = entry.get("title") or video_id
            seen.append(video_id)
            links.append((video_id, position))

            if title in UNAVAILABLE_TITLES:
                # Keep the row so /playlist view explains the gap, but never
                # hand it to the downloader.
                await self.db.upsert_track(video_id, title, None, None)
                await self.db.mark_failed(video_id, title, status="skipped")
                continue

            if await self.db.get_track(video_id) is None:
                new += 1
            duration = entry.get("duration")
            await self.db.upsert_track(
                video_id,
                title,
                entry.get("uploader") or entry.get("channel"),
                int(duration) if duration else None,
            )

        if links:
            await self.db.link_tracks(playlist_id, links)
        await self.db.unlink_absent(playlist_id, seen)
        await self.db.update_playlist_sync(playlist_id, info.get("title"), len(seen), None)
        log.info("indexed playlist %s (%d items, %d new)", playlist_id, len(seen), new)
        return new

    # -------------------------------------------------------------- download

    async def _download_pending(self, report: SyncReport) -> None:
        pending = await self.db.pending_tracks()
        self.remaining = len(pending)
        if not pending:
            return
        log.info("downloading %d pending track(s)", len(pending))

        async def run(row) -> None:
            async with self._sem:
                if await self._download_one(row["video_id"], row["title"]):
                    report.downloaded += 1
                else:
                    report.failed += 1
                self.remaining = max(0, self.remaining - 1)

        await asyncio.gather(*(run(row) for row in pending), return_exceptions=True)

    async def _download_one(self, video_id: str, title: str) -> bool:
        # Already on disk from a previous run? Just re-link it.
        existing = self._find_file(video_id)
        if existing is not None:
            await self.db.mark_downloaded(video_id, existing.name, None)
            return True

        self.current = title
        try:
            info = await asyncio.to_thread(self._download_sync, video_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed for %s (%s): %s", video_id, title, exc)
            await self.db.mark_failed(video_id, str(exc))
            return False
        finally:
            self.current = None

        path = self._find_file(video_id)
        if path is None:
            await self.db.mark_failed(video_id, "post-processing produced no mp3 file")
            return False

        duration = info.get("duration") if info else None
        await self.db.mark_downloaded(
            video_id,
            path.name,
            int(duration) if duration else None,
            (info or {}).get("title"),
        )
        log.info("downloaded %s -> %s", video_id, path.name)
        return True

    def _download_sync(self, video_id: str) -> dict[str, Any]:
        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(self._download_opts()) as ydl:
            return ydl.extract_info(url, download=True) or {}

    def _find_file(self, video_id: str) -> Path | None:
        mp3 = self.cfg.audio_dir / f"{video_id}.mp3"
        if mp3.exists() and mp3.stat().st_size > 0:
            return mp3
        return None

    # ----------------------------------------------------------------- prune

    async def prune(self, delete_files: bool = True) -> int:
        """Drop tracks that fell out of every enabled playlist."""
        removed = 0
        for row in await self.db.orphan_tracks():
            if delete_files and row["filename"]:
                path = self.cfg.audio_dir / row["filename"]
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", path, exc)
            await self.db.delete_track(row["video_id"])
            removed += 1
        return removed


class _YtdlLogger:
    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp error: %s", msg)
