"""SQLite-backed track/playlist library.

All blocking sqlite3 work is pushed onto a worker thread so the event loop
never stalls behind disk I/O.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    title        TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_by     INTEGER,
    added_at     TEXT,
    last_synced  TEXT,
    item_count   INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    video_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    uploader      TEXT,
    duration      INTEGER,
    filename      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT,
    downloaded_at TEXT,
    play_count    INTEGER NOT NULL DEFAULT 0,
    last_played   TEXT
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, video_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE INDEX IF NOT EXISTS idx_pt_video ON playlist_tracks(video_id);
"""

ACTIVE_PLAYLISTS_KEY = "active_playlists"

STATUS_PENDING = "pending"
STATUS_DOWNLOADED = "downloaded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ core

    def _connect_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    async def connect(self) -> None:
        await asyncio.to_thread(self._connect_sync)

    async def close(self) -> None:
        def _close() -> None:
            with self._lock:
                if self._conn is not None:
                    self._conn.commit()
                    self._conn.close()
                    self._conn = None

        await asyncio.to_thread(_close)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    def _execute_sync(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def _executemany_sync(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, list(rows))
            self.conn.commit()

    def _fetchall_sync(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _fetchone_sync(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await asyncio.to_thread(self._execute_sync, sql, params)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await asyncio.to_thread(self._executemany_sync, sql, rows)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._fetchall_sync, sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._fetchone_sync, sql, params)

    # ------------------------------------------------------------- settings

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ playlists

    async def add_playlist(
        self, playlist_id: str, url: str, title: str | None, added_by: int | None
    ) -> None:
        await self.execute(
            "INSERT INTO playlists(id, url, title, added_by, added_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET url = excluded.url, enabled = 1",
            (playlist_id, url, title, added_by, utcnow()),
        )

    async def remove_playlist(self, playlist_id: str) -> int:
        await self.execute(
            "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
        )
        return await self.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    async def set_playlist_enabled(self, playlist_id: str, enabled: bool) -> int:
        return await self.execute(
            "UPDATE playlists SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, playlist_id),
        )

    async def get_playlists(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = (
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS tracked "
            "FROM playlists p"
        )
        if enabled_only:
            sql += " WHERE p.enabled = 1"
        sql += " ORDER BY COALESCE(p.title, p.id) COLLATE NOCASE"
        return await self.fetchall(sql)

    async def find_playlist(self, needle: str) -> sqlite3.Row | None:
        row = await self.fetchone("SELECT * FROM playlists WHERE id = ?", (needle,))
        if row:
            return row
        return await self.fetchone(
            "SELECT * FROM playlists WHERE title LIKE ? OR id LIKE ? LIMIT 1",
            (f"%{needle}%", f"%{needle}%"),
        )

    async def update_playlist_sync(
        self, playlist_id: str, title: str | None, item_count: int, error: str | None
    ) -> None:
        await self.execute(
            "UPDATE playlists SET title = COALESCE(?, title), item_count = ?, "
            "last_synced = ?, last_error = ? WHERE id = ?",
            (title, item_count, utcnow(), error, playlist_id),
        )

    async def playlist_tracks(
        self, playlist_id: str, offset: int = 0, limit: int = 1000
    ) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT t.*, pt.position FROM playlist_tracks pt "
            "JOIN tracks t ON t.video_id = pt.video_id "
            "WHERE pt.playlist_id = ? ORDER BY pt.position LIMIT ? OFFSET ?",
            (playlist_id, limit, offset),
        )

    async def playlist_counts(self, playlist_id: str) -> tuple[int, int]:
        row = await self.fetchone(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN t.status = 'downloaded' THEN 1 ELSE 0 END) AS done "
            "FROM playlist_tracks pt JOIN tracks t ON t.video_id = pt.video_id "
            "WHERE pt.playlist_id = ?",
            (playlist_id,),
        )
        if not row:
            return (0, 0)
        return (row["total"] or 0, row["done"] or 0)

    # --------------------------------------------------------------- tracks

    async def upsert_track(
        self, video_id: str, title: str, uploader: str | None, duration: int | None
    ) -> None:
        await self.execute(
            "INSERT INTO tracks(video_id, title, uploader, duration, added_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(video_id) DO UPDATE SET "
            "  title = CASE WHEN excluded.title != '' THEN excluded.title ELSE tracks.title END, "
            "  uploader = COALESCE(excluded.uploader, tracks.uploader), "
            "  duration = COALESCE(excluded.duration, tracks.duration)",
            (video_id, title, uploader, duration, utcnow()),
        )

    async def link_tracks(
        self, playlist_id: str, rows: Iterable[tuple[str, int]]
    ) -> None:
        """rows: (video_id, position)."""
        await self.executemany(
            "INSERT INTO playlist_tracks(playlist_id, video_id, position) "
            "VALUES(?, ?, ?) "
            "ON CONFLICT(playlist_id, video_id) DO UPDATE SET "
            "  position = excluded.position",
            [(playlist_id, vid, pos) for vid, pos in rows],
        )

    async def unlink_absent(self, playlist_id: str, keep: Sequence[str]) -> int:
        if not keep:
            return await self.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
            )
        marks = ",".join("?" * len(keep))
        return await self.execute(
            f"DELETE FROM playlist_tracks WHERE playlist_id = ? AND video_id NOT IN ({marks})",
            (playlist_id, *keep),
        )

    async def pending_tracks(self, limit: int = 500, max_attempts: int = 3) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT t.* FROM tracks t "
            "WHERE t.status IN ('pending', 'failed') AND t.attempts < ? "
            "AND EXISTS (SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id "
            "            WHERE pt.video_id = t.video_id AND p.enabled = 1) "
            "ORDER BY t.attempts, t.added_at LIMIT ?",
            (max_attempts, limit),
        )

    async def mark_downloaded(
        self, video_id: str, filename: str, duration: int | None, title: str | None = None
    ) -> None:
        await self.execute(
            "UPDATE tracks SET status = 'downloaded', filename = ?, error = NULL, "
            "duration = COALESCE(?, duration), title = COALESCE(?, title), downloaded_at = ? "
            "WHERE video_id = ?",
            (filename, duration, title, utcnow(), video_id),
        )

    async def mark_failed(self, video_id: str, error: str, status: str = STATUS_FAILED) -> None:
        await self.execute(
            "UPDATE tracks SET status = ?, error = ?, attempts = attempts + 1 WHERE video_id = ?",
            (status, error[:500], video_id),
        )

    async def mark_missing_file(self, video_id: str) -> None:
        await self.execute(
            "UPDATE tracks SET status = 'pending', filename = NULL, attempts = 0 "
            "WHERE video_id = ?",
            (video_id,),
        )

    async def reset_failures(self) -> int:
        return await self.execute(
            "UPDATE tracks SET attempts = 0, status = 'pending' WHERE status = 'failed'"
        )

    async def get_track(self, video_id: str) -> sqlite3.Row | None:
        return await self.fetchone("SELECT * FROM tracks WHERE video_id = ?", (video_id,))

    # -------------------------------------------------- active playlist set

    async def resolve_active_playlist_ids(self) -> list[str]:
        """Which playlists the bot currently plays from.

        Unset or "*" means every enabled playlist. Stored selections are
        intersected with the enabled set, so removing or disabling a playlist
        drops it from rotation without leaving a dangling reference.
        """
        enabled = [row["id"] for row in await self.get_playlists(enabled_only=True)]
        raw = await self.get_setting(ACTIVE_PLAYLISTS_KEY)
        if not raw or raw == "*":
            return enabled
        try:
            chosen = json.loads(raw)
        except (ValueError, TypeError):
            return enabled
        if not isinstance(chosen, list):
            return enabled
        allowed = set(enabled)
        return [pid for pid in chosen if pid in allowed]

    async def is_following_all(self) -> bool:
        raw = await self.get_setting(ACTIVE_PLAYLISTS_KEY)
        return not raw or raw == "*"

    async def set_active_playlists(self, playlist_ids: list[str] | None) -> None:
        """None = follow every enabled playlist."""
        if playlist_ids is None:
            await self.set_setting(ACTIVE_PLAYLISTS_KEY, "*")
        else:
            await self.set_setting(ACTIVE_PLAYLISTS_KEY, json.dumps(playlist_ids))

    @staticmethod
    def _scope_clause(playlist_ids: Sequence[str] | None) -> tuple[str, list[str]]:
        """SQL fragment restricting tracks to a set of playlists."""
        if playlist_ids is None:
            return "", []
        if not playlist_ids:
            return " AND 0 ", []
        marks = ",".join("?" * len(playlist_ids))
        clause = (
            f" AND EXISTS (SELECT 1 FROM playlist_tracks pt "
            f"WHERE pt.video_id = tracks.video_id AND pt.playlist_id IN ({marks})) "
        )
        return clause, list(playlist_ids)

    # --------------------------------------------------------- track lookups

    async def downloaded_ids(
        self, playlist_ids: Sequence[str] | None = None
    ) -> list[str]:
        scope, params = self._scope_clause(playlist_ids)
        rows = await self.fetchall(
            "SELECT video_id FROM tracks WHERE status = 'downloaded' "
            "AND filename IS NOT NULL" + scope,
            params,
        )
        return [row["video_id"] for row in rows]

    async def search_tracks(
        self,
        query: str,
        limit: int = 25,
        playlist_ids: Sequence[str] | None = None,
    ) -> list[sqlite3.Row]:
        """Search *downloaded* tracks only — nothing else is playable."""
        query = query.strip()
        scope, scope_params = self._scope_clause(playlist_ids)
        if not query:
            return await self.fetchall(
                "SELECT * FROM tracks WHERE status = 'downloaded'" + scope
                + " ORDER BY last_played IS NULL DESC, play_count DESC, "
                "  title COLLATE NOCASE LIMIT ?",
                [*scope_params, limit],
            )
        like = f"%{query}%"
        prefix = f"{query}%"
        return await self.fetchall(
            "SELECT * FROM tracks WHERE status = 'downloaded' "
            "AND (title LIKE ? OR uploader LIKE ? OR video_id = ?)" + scope
            + " ORDER BY CASE WHEN title LIKE ? THEN 0 WHEN title LIKE ? THEN 1 "
            "  ELSE 2 END, title COLLATE NOCASE LIMIT ?",
            [like, like, query, *scope_params, prefix, like, limit],
        )

    async def bump_play(self, video_id: str) -> None:
        await self.execute(
            "UPDATE tracks SET play_count = play_count + 1, last_played = ? WHERE video_id = ?",
            (utcnow(), video_id),
        )

    async def stats(self) -> dict[str, int]:
        rows = await self.fetchall("SELECT status, COUNT(*) AS n FROM tracks GROUP BY status")
        out = {row["status"]: row["n"] for row in rows}
        out["total"] = sum(out.values())
        return out

    async def orphan_tracks(self) -> list[sqlite3.Row]:
        """Tracks that no longer belong to any enabled playlist."""
        return await self.fetchall(
            "SELECT * FROM tracks t WHERE NOT EXISTS ("
            "  SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id = pt.playlist_id "
            "  WHERE pt.video_id = t.video_id AND p.enabled = 1)"
        )

    async def delete_track(self, video_id: str) -> None:
        await self.execute("DELETE FROM playlist_tracks WHERE video_id = ?", (video_id,))
        await self.execute("DELETE FROM tracks WHERE video_id = ?", (video_id,))
