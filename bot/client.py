"""Bot wiring: intents, cogs, players, periodic sync."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .config import Config
from .db import Database
from .downloader import Downloader, parse_playlist_id, playlist_url
from .player import GuildPlayer

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
        self.players: dict[int, GuildPlayer] = {}
        self._sync_task: asyncio.Task | None = None
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

        await self.add_cog(MusicCog(self))
        await self.add_cog(LibraryCog(self))
        await self.add_cog(CookiesCog(self))

        await self._sync_commands()

        self._sync_task = self.loop.create_task(self._sync_loop(), name="library-sync")

    async def _sync_commands(self) -> None:
        """Register slash commands. Never fatal — a bot that can't register
        commands is still worth having up, and the fix is an invite change."""
        try:
            if self.cfg.guild_ids:
                for guild_id in self.cfg.guild_ids:
                    snowflake = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=snowflake)
                    await self.tree.sync(guild=snowflake)
                log.info(
                    "slash commands synced to %d guild(s)", len(self.cfg.guild_ids)
                )
            else:
                await self.tree.sync()
                log.info("slash commands synced globally (can take up to an hour)")
        except discord.Forbidden:
            app_id = self.application_id or "<your application id>"
            log.error(
                "Could not register slash commands (403 Missing Access).\n"
                "  The bot is either not in the server, or was invited without the\n"
                "  'applications.commands' scope. Re-invite it with BOTH scopes:\n"
                "  https://discord.com/oauth2/authorize"
                "?client_id=%s&scope=bot+applications.commands&permissions=3165184\n"
                "  Also check that GUILD_IDS matches a server the bot is actually in.\n"
                "  Continuing without slash commands.",
                app_id,
            )
        except discord.HTTPException as exc:
            log.error("Slash command sync failed (%s). Continuing.", exc)

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
        for guild in self.guilds:
            await self.ensure_player(guild)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening, name="/play • /queue"
            )
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
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
            log.info("player started for guild %s", guild.name)
        return player

    def get_player(self, guild: discord.Guild | None) -> GuildPlayer | None:
        return self.players.get(guild.id) if guild else None

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
        """Fresh downloads land: fold them into the shuffle bag."""
        for player in self.players.values():
            if not player.queue and (player.waiting_for_tracks or player.bag_size == 0):
                await player.reshuffle()

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
