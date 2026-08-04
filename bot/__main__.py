"""Entry point: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import discord

from .client import MusicBot
from .config import Config


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.ERROR)


def check_js_runtime() -> str | None:
    """yt-dlp needs a JS runtime + the EJS solver to get past YouTube's "n"
    challenge. Without both, YouTube returns no audio formats at all and every
    download fails with "Requested format is not available"."""
    import shutil

    runtime = next(
        (r for r in ("deno", "node", "quickjs", "bun") if shutil.which(r)), None
    )
    try:
        import yt_dlp_ejs  # noqa: F401  (probed for availability, not used here)

        solver = True
    except ImportError:
        solver = False
    return runtime if (runtime and solver) else None


def ensure_opus() -> bool:
    """discord.py loads libopus lazily, so is_loaded() is False until a voice
    connection needs it. Load it eagerly to report the truth at startup."""
    if discord.opus.is_loaded():
        return True
    try:
        return bool(discord.opus._load_default())
    except Exception:  # noqa: BLE001
        return False


def load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def check_writable(cfg: Config) -> str | None:
    """Return an actionable message if the data directory isn't writable.

    In Docker this is the single most common startup failure: ./data was
    created by root on the host, but the container runs as uid 1000, so SQLite
    fails deep inside connect() with a bare "readonly database".
    """
    for path in (cfg.data_dir, cfg.audio_dir, cfg.db_path.parent):
        probe = path / ".write-test"
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe.touch()
            probe.unlink()
        except OSError as exc:
            uid, gid = os.getuid(), os.getgid()
            try:
                st = path.stat()
                owner = f"owned by uid={st.st_uid} gid={st.st_gid}"
            except OSError:
                owner = "unreadable"
            in_docker = Path("/.dockerenv").exists()
            fix = (
                f"On the host, run:  sudo chown -R {uid}:{gid} data"
                if in_docker
                else f"Run:  sudo chown -R {uid}:{gid} {path}"
            )
            return (
                f"Cannot write to {path} ({exc.strerror}).\n"
                f"  The directory is {owner}, but this process runs as "
                f"uid={uid} gid={gid}.\n"
                f"  {fix}"
            )
    return None


async def amain() -> None:
    log = logging.getLogger("bot")
    cfg = Config.from_env()
    try:
        cfg.ensure_dirs()
    except OSError:
        pass  # check_writable turns this into something actionable

    problem = check_writable(cfg)
    if problem:
        log.error("%s", problem)
        raise SystemExit(1)

    if ensure_opus():
        log.info("libopus loaded — voice encoding available")
    else:
        log.warning(
            "libopus could not be loaded — the bot will join voice but stay "
            "silent. Install it with: sudo apt install libopus0"
        )

    runtime = check_js_runtime()
    if runtime:
        log.info("JS runtime for YouTube challenges: %s", runtime)
    else:
        log.warning(
            "No JavaScript runtime and/or yt-dlp-ejs found. YouTube will serve "
            "no audio formats and every download will fail with 'Requested "
            "format is not available'. Fix: install deno and "
            "`pip install yt-dlp-ejs`."
        )

    bot = MusicBot(cfg)
    try:
        await bot.start(cfg.token)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "MEMBER_INTENT=true but the Server Members intent is not enabled for this "
            "application. Enable it in the Developer Portal, or set MEMBER_INTENT=false."
        )
    except discord.LoginFailure:
        log.error("Discord rejected the token. Check DISCORD_TOKEN.")
    finally:
        if not bot.is_closed():
            await bot.close()


def main() -> None:
    load_dotenv_if_present()
    setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
