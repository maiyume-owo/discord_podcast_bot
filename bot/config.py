"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _get(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_opt_int(name: str) -> int | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _get_float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _split(raw: str | None) -> list[str]:
    if not raw:
        return []
    for sep in (",", "\n", " "):
        raw = raw.replace(sep, ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def _get_str_list(name: str) -> list[str]:
    return _split(_get(name))


def _get_id_list(name: str) -> list[int]:
    out: list[int] = []
    for part in _get_str_list(name):
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


@dataclass(frozen=True)
class Config:
    # --- discord ---
    token: str
    guild_ids: list[int]
    voice_channel_id: int | None
    fallback_channel_ids: list[int]
    text_channel_id: int | None
    dj_role_ids: list[int]
    owner_ids: list[int]
    member_intent: bool
    restrict_requests_to_active: bool

    # --- storage ---
    data_dir: Path
    audio_dir: Path
    db_path: Path
    cache_dir: Path

    # --- youtube ---
    seed_playlists: list[str]
    cookies_file: Path | None
    cookies_from_browser: str | None
    audio_quality: str
    download_concurrency: int
    sync_on_start: bool
    sync_interval: int  # seconds, 0 disables periodic sync

    # --- playback ---
    volume: float
    idle_pause: bool
    idle_stop_after: int  # seconds of empty channel before the stream is torn down
    reconnect_interval: int
    watchdog_interval: int
    max_queue: int

    @classmethod
    def from_env(cls) -> "Config":
        token = _get("DISCORD_TOKEN")
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        data_dir = Path(_get("DATA_DIR", "/data")).expanduser()
        audio_dir = Path(_get("AUDIO_DIR", str(data_dir / "audio"))).expanduser()
        db_path = Path(_get("DB_PATH", str(data_dir / "library.db"))).expanduser()
        cache_dir = Path(_get("CACHE_DIR", str(data_dir / "cache"))).expanduser()

        # Keep the configured path even when the file is absent, so cookies
        # dropped in at runtime (/cookies upload) take effect without a restart.
        cookies_raw = _get("COOKIES_FILE", str(data_dir / "cookies.txt"))
        cookies_file = Path(cookies_raw).expanduser() if cookies_raw else None

        return cls(
            token=token,
            guild_ids=_get_id_list("GUILD_IDS"),
            voice_channel_id=_get_opt_int("VOICE_CHANNEL_ID"),
            fallback_channel_ids=_get_id_list("FALLBACK_VOICE_CHANNEL_IDS"),
            text_channel_id=_get_opt_int("TEXT_CHANNEL_ID"),
            dj_role_ids=_get_id_list("DJ_ROLE_IDS"),
            owner_ids=_get_id_list("OWNER_IDS"),
            member_intent=_get_bool("MEMBER_INTENT", False),
            restrict_requests_to_active=_get_bool("RESTRICT_REQUESTS_TO_ACTIVE", True),
            data_dir=data_dir,
            audio_dir=audio_dir,
            db_path=db_path,
            cache_dir=cache_dir,
            seed_playlists=_get_str_list("PLAYLISTS"),
            cookies_file=cookies_file,
            cookies_from_browser=_get("COOKIES_FROM_BROWSER"),
            audio_quality=_get("AUDIO_QUALITY", "192"),
            download_concurrency=max(1, _get_int("DOWNLOAD_CONCURRENCY", 2)),
            sync_on_start=_get_bool("SYNC_ON_START", True),
            sync_interval=_get_int("SYNC_INTERVAL", 3600),
            volume=max(0.0, min(2.0, _get_float("VOLUME", 0.5))),
            idle_pause=_get_bool("IDLE_PAUSE", True),
            idle_stop_after=_get_int("IDLE_STOP_AFTER", 300),
            reconnect_interval=max(10, _get_int("RECONNECT_INTERVAL", 30)),
            watchdog_interval=max(5, _get_int("WATCHDOG_INTERVAL", 15)),
            max_queue=max(1, _get_int("MAX_QUEUE", 200)),
        )

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.audio_dir, self.cache_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)

    def is_dj(self, member) -> bool:
        """DJ = server manager, or holder of a configured DJ role."""
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and (perms.manage_guild or perms.administrator):
            return True
        if not self.dj_role_ids:
            return False
        role_ids = {role.id for role in getattr(member, "roles", [])}
        return bool(role_ids & set(self.dj_role_ids))
