"""Bot wiring: intents, cogs, players, periodic sync."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import discord
from discord.ext import commands

from .config import Config
from .db import Database
from .downloader import Downloader, parse_playlist_id, playlist_url
from .player import GuildPlayer
from .utils import invite_url

log = logging.getLogger(__name__)


class MusicBot(commands.Bot):
    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.members = cfg.member_intent

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.downloader = Downloader(cfg, self.db)
        # One player + station per guild; playback is never shared between them.
        self.players: dict[int, GuildPlayer] = {}
        self._sync_task: asyncio.Task | None = None
        self._presence: str | None = None
        if cfg.owner_ids:
            self.owner_ids = set(cfg.owner_ids)

    # ------------------------------------------------------------- lifecycle

    async def setup_hook(self) -> None:
        self.cfg.ensure_dirs()
        await self.db.connect()
        await self._seed_playlists()
        self.downloader.on_new_tracks = self._on_new_tracks

        from .cogs.music import MusicCog
        from .cogs.library import LibraryCog
        from .cogs.cookies import CookiesCog
        from .cogs.meta import MetaCog

        await self.add_cog(MusicCog(self))
        await self.add_cog(LibraryCog(self))
        await self.add_cog(CookiesCog(self))
        await self.add_cog(MetaCog(self))

        self._sync_task = self.loop.create_task(self._sync_loop(), name="library-sync")

    # -------------------------------------------------------- command sync

    def _command_signature(self) -> str:
        """Fingerprint of the command tree, so a restart re-syncs only when
        the commands actually changed. Discord caps command writes per guild
        per day, and this bot can restart in a loop."""
        parts = sorted(
            f"{cmd.qualified_name}:{getattr(cmd, 'description', '')}"
            for cmd in self.tree.walk_commands()
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    async def sync_guild_commands(
        self, guild: discord.abc.Snowflake, *, force: bool = False
    ) -> bool:
        """Register every command in one guild. Instant, unlike a global sync.

        Commands are registered per guild rather than globally: a global sync
        can take up to an hour to appear, which reads as "This application has
        no commands" in a freshly invited server.
        """
        key = f"cmdsync:{guild.id}"
        signature = self._command_signature()
        if not force and await self.db.get_setting(key) == signature:
            return False
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.Forbidden:
            link = invite_url(self.application_id) or "<set your application id>"
            log.error(
                "Could not register slash commands in guild %s (403 Missing "
                "Access).\n"
                "  The bot was invited without the 'applications.commands' "
                "scope. Re-invite it with BOTH scopes:\n"
                "  %s\n"
                "  Continuing without slash commands there.",
                guild.id,
                link,
            )
            return False
        except discord.HTTPException as exc:
            log.error("Command sync failed for guild %s (%s). Continuing.", guild.id, exc)
            return False
        await self.db.set_setting(key, signature)
        log.info("slash commands synced to guild %s", guild.id)
        return True

    async def _sync_all_commands(self) -> None:
        """Every guild we're in, plus any explicitly listed in GUILD_IDS."""
        await self._clear_global_commands()
        seen = set()
        targets: list[discord.abc.Snowflake] = list(self.guilds)
        seen.update(g.id for g in self.guilds)
        for guild_id in self.cfg.guild_ids:
            if guild_id not in seen:
                targets.append(discord.Object(id=guild_id))
                seen.add(guild_id)
        synced = 0
        for target in targets:
            if await self.sync_guild_commands(target):
                synced += 1
        log.info(
            "commands up to date in %d guild(s) (%d re-synced)", len(targets), synced
        )

    async def _clear_global_commands(self) -> None:
        """Drop globally-registered commands, once.

        Older versions synced globally when GUILD_IDS was empty. Leaving those
        in place alongside the per-guild copies shows every command twice.
        """
        if await self.db.get_setting("cmdsync:global-cleared") == "1":
            return
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, [])
        except discord.HTTPException as exc:
            log.debug("could not clear global commands: %s", exc)
            return
        await self.db.set_setting("cmdsync:global-cleared", "1")
        log.info("cleared globally-registered commands (they are per-guild now)")

    async def _seed_playlists(self) -> None:
        """PLAYLISTS env var seeds the library on first boot."""
        for raw in self.cfg.seed_playlists:
            playlist_id = parse_playlist_id(raw)
            if not playlist_id:
                log.warning("ignoring unparseable playlist entry: %r", raw)
                continue
            await self.db.add_playlist(playlist_id, playlist_url(playlist_id), None, None)
            log.info("seeded playlist %s", playlist_id)

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        # Guilds are only known once we're ready, so commands are registered
        # here rather than in setup_hook.
        await self._sync_all_commands()
        for guild in self.guilds:
            await self.ensure_player(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        # force=True: leaving a guild deletes its commands, so a re-invite has
        # to re-register them even though the tree hasn't changed.
        await self.sync_guild_commands(guild, force=True)
        await self.ensure_player(guild)

    async def close(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
        for player in list(self.players.values()):
            await player.stop()
        await self.db.close()
        await super().close()

    async def is_bot_owner(self, user: discord.abc.User) -> bool:
        """OWNER_IDS, else the application owner / team members from Discord."""
        if user.id in self.cfg.owner_ids:
            return True
        try:
            return await self.is_owner(user)
        except discord.HTTPException:
            return False

    # --------------------------------------------------------------- players

    async def ensure_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = GuildPlayer(self, self.cfg, self.db, guild)
            self.players[guild.id] = player
            await player.start()
            log.info("player + station started for guild %s", guild.name)
        return player

    @property
    def stations(self) -> list:
        """Every guild's station. There is no global one."""
        return [player.station for player in self.players.values()]

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        player = self.players.pop(guild.id, None)
        if player is not None:
            await player.stop()
            log.info("player stopped for guild %s", guild.name)
        # Discord deletes the guild's commands with us; forget the fingerprint
        # so a re-invite registers them again.
        await self.db.set_setting(f"cmdsync:{guild.id}", "")

    async def refresh_presence(self) -> None:
        """One status line for a bot that is now playing several things.

        Discord gives us a single presence, so it shows the station with the
        most listeners and says how many other servers are playing along.
        """
        playing = [s for s in self.stations if s.current is not None]
        if not playing:
            text = None
        else:
            best = max(playing, key=lambda s: (s.listener_count(), s.guild.id))
            text = best.current.track.title
            if len(playing) > 1:
                text += f" (+{len(playing) - 1} more server(s))"

        if text == self._presence:
            return
        self._presence = text
        try:
            await self.change_presence(
                activity=None
                if text is None
                else discord.Activity(
                    type=discord.ActivityType.listening, name=text[:128]
                )
            )
        except discord.HTTPException as exc:
            log.debug("presence update failed: %s", exc)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        player = self.players.get(member.guild.id)
        if player is not None:
            player.handle_voice_state_update(member, before, after)

    # ------------------------------------------------------------ background

    async def _on_new_tracks(self) -> None:
        """Fresh downloads land: fold them into every guild's shuffle bag."""
        for station in self.stations:
            if not station.queue and (
                station.waiting_for_tracks or station.bag_size == 0
            ):
                await station.reshuffle()

    async def _sync_loop(self) -> None:
        await self.wait_until_ready()
        if self.cfg.sync_on_start:
            await self._run_sync()
        if self.cfg.sync_interval <= 0:
            return
        while not self.is_closed():
            await asyncio.sleep(self.cfg.sync_interval)
            await self._run_sync()

    async def _run_sync(self) -> None:
        try:
            report = await self.downloader.sync()
            log.info("library sync complete: %s", report.summary())
        except RuntimeError as exc:
            log.info("skipping sync: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("library sync failed")
