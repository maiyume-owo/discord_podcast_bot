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

# Cookie names YouTube sets only for a signed-in session.
AUTH_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
}

_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "confirm your age",
    "use --cookies",
)


def is_bot_check(message: str) -> bool:
    low = message.lower()
    return any(marker in low for marker in _BOT_CHECK_MARKERS)


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
        # Set when YouTube demands sign-in; cleared once a download succeeds.
        self.bot_check_blocked = False
        self.cookie_browser = cfg.cookies_from_browser

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
        if self.cookies_available():
            opts["cookiefile"] = str(self.cfg.cookies_file)
        elif self.cookie_browser:
            spec = self.cookie_browser.split(":")
            opts["cookiesfrombrowser"] = tuple(spec) + (None,) * (4 - len(spec))
        return opts

    def cookies_available(self) -> bool:
        path = self.cfg.cookies_file
        return bool(path and path.exists() and path.stat().st_size > 0)

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
            message = str(exc)
            if is_bot_check(message):
                if not self.bot_check_blocked:
                    log.error(
                        "YouTube is demanding sign-in — downloads are blocked until "
                        "cookies are supplied. Run /cookies guide in Discord."
                    )
                self.bot_check_blocked = True
                # Don't burn the retry budget on something cookies will fix.
                await self.db.mark_failed(
                    video_id, "blocked: YouTube sign-in required (needs cookies)"
                )
                await self.db.execute(
                    "UPDATE tracks SET attempts = 0 WHERE video_id = ?", (video_id,)
                )
                return False
            log.warning("download failed for %s (%s): %s", video_id, title, message)
            await self.db.mark_failed(video_id, message)
            return False
        finally:
            self.current = None

        path = self._find_file(video_id)
        if path is None:
            await self.db.mark_failed(video_id, "post-processing produced no mp3 file")
            return False

        self.bot_check_blocked = False  # a success proves we're not blocked
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

    # --------------------------------------------------------------- cookies

    def cookie_status(self) -> dict[str, Any]:
        path = self.cfg.cookies_file
        out: dict[str, Any] = {
            "path": str(path) if path else "(not configured)",
            "exists": False,
            "size": 0,
            "age_hours": None,
            "youtube_cookies": 0,
            "auth_cookies": [],
            "browser": self.cookie_browser,
            "blocked": self.bot_check_blocked,
        }
        if not (path and path.exists()):
            return out
        stat = path.stat()
        out["exists"] = True
        out["size"] = stat.st_size
        out["age_hours"] = (time.time() - stat.st_mtime) / 3600
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return out
        names: list[str] = []
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 7 or "youtube.com" not in parts[0]:
                continue
            out["youtube_cookies"] += 1
            if parts[5] in AUTH_COOKIE_NAMES:
                names.append(parts[5])
        out["auth_cookies"] = sorted(set(names))
        return out

    @staticmethod
    def validate_cookie_text(text: str) -> tuple[bool, str]:
        """Check an uploaded file really is a Netscape jar with YouTube auth."""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return False, "The file is empty."
        data = [ln for ln in lines if not ln.startswith("#")]
        if not data:
            return False, "No cookie entries found — only comments."
        yt = [ln for ln in data if "youtube.com" in ln.split("\t")[0]]
        if not yt:
            return False, (
                "No `youtube.com` cookies in this file. Make sure you exported "
                "**while on a YouTube page**, not another site."
            )
        malformed = [ln for ln in yt if len(ln.split("\t")) < 7]
        if malformed:
            return False, (
                "This isn't Netscape format (columns must be tab-separated). "
                "Re-export choosing **Netscape**, not JSON."
            )
        found = {ln.split("\t")[5] for ln in yt}
        if not (found & AUTH_COOKIE_NAMES):
            return False, (
                "No login cookies present — you were exported as a signed-out "
                "visitor. Log in to YouTube first, then export."
            )
        return True, f"{len(yt)} YouTube cookies, logged in ✓"

    async def install_cookie_text(self, text: str) -> tuple[bool, str]:
        ok, message = self.validate_cookie_text(text)
        if not ok:
            return False, message
        path = self.cfg.cookies_file
        if path is None:
            return False, "COOKIES_FILE is not configured."

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o600)  # it's a live session credential

        await asyncio.to_thread(_write)
        self.bot_check_blocked = False
        log.info("installed new cookie jar (%s)", message)
        return True, message

    async def import_browser_cookies(
        self, browser: str, profile: str | None = None
    ) -> tuple[bool, str]:
        """Best-effort auto-extraction. Unreliable for YouTube, see /cookies guide."""
        path = self.cfg.cookies_file
        if path is None:
            return False, "COOKIES_FILE is not configured."

        def _extract() -> int:
            from yt_dlp.cookies import extract_cookies_from_browser
            from yt_dlp.utils import YoutubeDLCookieJar

            jar = extract_cookies_from_browser(browser, profile)
            keep = YoutubeDLCookieJar()
            count = 0
            for cookie in jar:
                if "youtube.com" in cookie.domain or "google.com" in cookie.domain:
                    keep.set_cookie(cookie)
                    count += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            keep.save(str(path))
            path.chmod(0o600)
            return count

        try:
            count = await asyncio.to_thread(_extract)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if count == 0:
            return False, (
                f"Found no YouTube cookies in **{browser}**. Are you logged in "
                "in that browser, on this machine?"
            )
        self.bot_check_blocked = False
        return True, f"Imported {count} cookie(s) from {browser}."

    async def clear_cookies(self) -> bool:
        path = self.cfg.cookies_file
        if path is None or not path.exists():
            return False
        await asyncio.to_thread(path.unlink)
        return True

    async def test_cookies(self, video_id: str | None = None) -> tuple[bool, str]:
        """Ask YouTube for one video's metadata and see if it lets us through."""
        if video_id is None:
            row = await self.db.fetchone(
                "SELECT video_id FROM tracks WHERE status IN ('pending', 'failed') "
                "LIMIT 1"
            )
            if row is None:
                row = await self.db.fetchone("SELECT video_id FROM tracks LIMIT 1")
            if row is None:
                return False, "No tracks in the library to test with — add a playlist."
            video_id = row["video_id"]

        def _probe() -> dict[str, Any]:
            opts = self._base_opts()
            opts["skip_download"] = True
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=False
                ) or {}

        try:
            info = await asyncio.to_thread(_probe)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if is_bot_check(text):
                self.bot_check_blocked = True
                return False, (
                    "YouTube is still demanding sign-in. The cookies are missing, "
                    "expired, or were exported from a signed-out session."
                )
            return False, f"Failed on `{video_id}`: {text[:250]}"
        self.bot_check_blocked = False
        return True, f"YouTube accepted the request — read **{info.get('title', video_id)}**"

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
