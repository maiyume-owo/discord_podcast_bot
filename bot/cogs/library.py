"""Playlist tracking, rotation, and library maintenance.

Permission model:
  * bot owner  — which playlists are tracked, synced and downloaded (shared:
                 one library on disk for every server)
  * DJ         — which of those playlists this server plays from (per server)
  * everyone   — read-only views
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..downloader import parse_playlist_id, playlist_url
from ..utils import (
    INFO,
    WARN,
    Paginator,
    build_pages,
    err_embed,
    fmt_duration,
    ok_embed,
    truncate,
)

log = logging.getLogger(__name__)

STATUS_ICON = {
    "downloaded": "✅",
    "pending": "⏳",
    "failed": "⚠️",
    "skipped": "⛔",
}


class LibraryCog(commands.Cog, name="Library"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # --------------------------------------------------------------- helpers

    async def _deny_non_owner(self, interaction: discord.Interaction) -> bool:
        """True (and replies) if the caller is not a bot owner."""
        if await self.bot.is_bot_owner(interaction.user):
            return False
        await self._reply(
            interaction,
            err_embed("Only the **bot owner** can do that."),
            ephemeral=True,
        )
        return True

    async def _deny_non_dj(self, interaction: discord.Interaction) -> bool:
        """True (and replies) if the caller can't change this server's rotation.

        Rotation is per server now, so it's a server-level decision: Manage
        Server or a DJ role. Which playlists *exist* is still the bot owner's
        call, since those cost disk and downloads.
        """
        if interaction.guild is None:
            await self._reply(
                interaction, err_embed("Use this in a server."), ephemeral=True
            )
            return True
        if self.cfg.is_dj(interaction.user) or await self.bot.is_bot_owner(
            interaction.user
        ):
            return False
        await self._reply(
            interaction,
            err_embed(
                "Only **DJs** (Manage Server, or a role in `DJ_ROLE_IDS`) can "
                "change what this server plays."
            ),
            ephemeral=True,
        )
        return True

    @staticmethod
    async def _reply(
        interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = False
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def playlist_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await self.db.get_playlists()
        needle = current.lower()
        out: list[app_commands.Choice[str]] = []
        for row in rows:
            label = row["title"] or row["id"]
            if needle and needle not in label.lower() and needle not in row["id"].lower():
                continue
            out.append(
                app_commands.Choice(
                    name=truncate(f"{label} ({row['tracked']} tracks)", 100),
                    value=row["id"],
                )
            )
        return out[:25]

    # -------------------------------------------------------------- playlist

    group = app_commands.Group(
        name="playlist", description="View and manage tracked playlists"
    )

    @group.command(name="list", description="List the playlists the bot tracks")
    async def playlist_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        rows = await self.db.get_playlists()
        if not rows:
            await interaction.followup.send(
                embed=err_embed(
                    "No playlists tracked yet. The owner can add one with "
                    "`/playlist add`."
                )
            )
            return

        active = set(
            await self.db.resolve_active_playlist_ids(interaction.guild.id)
            if interaction.guild
            else []
        )
        lines: list[str] = []
        for row in rows:
            total, done = await self.db.playlist_counts(row["id"])
            marks = "🟢" if row["enabled"] else "⚪"
            if row["id"] in active:
                marks += "▶️"
            title = row["title"] or row["id"]
            line = (
                f"{marks} **{truncate(title, 55)}**\n"
                f"　`{row['id']}` • {done}/{total} downloaded"
            )
            if row["last_synced"]:
                line += f" • synced {row['last_synced'][:16].replace('T', ' ')}"
            if row["last_error"]:
                line += f"\n　⚠️ {truncate(row['last_error'], 90)}"
            lines.append(line)

        pages = build_pages(
            "Tracked playlists", lines, per_page=6, footer="▶️ = in rotation"
        )
        await Paginator(pages).send(interaction)

    @group.command(name="view", description="Show the tracks in a playlist")
    @app_commands.describe(playlist="Which playlist", only="Filter by download status")
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    @app_commands.choices(
        only=[
            app_commands.Choice(name="all", value="all"),
            app_commands.Choice(name="downloaded", value="downloaded"),
            app_commands.Choice(name="pending", value="pending"),
            app_commands.Choice(name="problems", value="problems"),
        ]
    )
    async def playlist_view(
        self,
        interaction: discord.Interaction,
        playlist: str,
        only: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.followup.send(embed=err_embed(f"No playlist `{playlist}`."))
            return

        tracks = await self.db.playlist_tracks(row["id"], limit=5000)
        mode = only.value if only else "all"
        if mode == "downloaded":
            tracks = [t for t in tracks if t["status"] == "downloaded"]
        elif mode == "pending":
            tracks = [t for t in tracks if t["status"] == "pending"]
        elif mode == "problems":
            tracks = [t for t in tracks if t["status"] in ("failed", "skipped")]

        lines = []
        for index, track in enumerate(tracks, start=1):
            icon = STATUS_ICON.get(track["status"], "❔")
            line = (
                f"`{index:>3}.` {icon} **{truncate(track['title'], 60)}** "
                f"`{fmt_duration(track['duration'])}`"
            )
            if track["status"] == "failed" and track["error"]:
                line += f"\n　　{truncate(track['error'], 80)}"
            lines.append(line)

        total, done = await self.db.playlist_counts(row["id"])
        pages = build_pages(
            f"{truncate(row['title'] or row['id'], 60)} — {mode}",
            lines,
            per_page=10,
            footer=f"{done}/{total} downloaded",
        )
        await Paginator(pages).send(interaction)

    @group.command(name="add", description="Track a YouTube playlist (owner)")
    @app_commands.describe(url="Playlist URL or ID")
    async def playlist_add(self, interaction: discord.Interaction, url: str) -> None:
        if await self._deny_non_owner(interaction):
            return
        playlist_id = parse_playlist_id(url)
        if not playlist_id:
            await interaction.response.send_message(
                embed=err_embed(
                    "That doesn't look like a playlist. Paste a link containing "
                    "`list=...` or the bare playlist ID."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        try:
            title, count = await self.bot.downloader.describe_playlist(playlist_id)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=err_embed(f"Couldn't read that playlist:\n```{truncate(str(exc), 300)}```")
            )
            return

        await self.db.add_playlist(
            playlist_id, playlist_url(playlist_id), title, interaction.user.id
        )
        await interaction.followup.send(
            embed=ok_embed(
                f"**{truncate(title, 100)}**\n`{playlist_id}` • {count} item(s)\n\n"
                "Run `/sync` to index and download it.",
                "Playlist added",
            )
        )

    @group.command(name="remove", description="Stop tracking a playlist (owner)")
    @app_commands.describe(playlist="Which playlist", delete_files="Also delete its mp3s")
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def playlist_remove(
        self, interaction: discord.Interaction, playlist: str, delete_files: bool = False
    ) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer()
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.followup.send(embed=err_embed(f"No playlist `{playlist}`."))
            return
        await self.db.remove_playlist(row["id"])
        message = (
            f"Stopped tracking **{truncate(row['title'] or row['id'], 80)}**.\n"
            "_Your YouTube playlist itself is untouched._"
        )
        if delete_files:
            removed = await self.bot.downloader.prune(delete_files=True)
            message += f"\nDeleted **{removed}** orphaned file(s)."
        await self._reshuffle_all()
        await interaction.followup.send(embed=ok_embed(message))

    @group.command(name="toggle", description="Enable/disable syncing a playlist (owner)")
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def playlist_toggle(
        self, interaction: discord.Interaction, playlist: str, enabled: bool
    ) -> None:
        if await self._deny_non_owner(interaction):
            return
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.response.send_message(
                embed=err_embed(f"No playlist `{playlist}`."), ephemeral=True
            )
            return
        await self.db.set_playlist_enabled(row["id"], enabled)
        await self._reshuffle_all()
        await interaction.response.send_message(
            embed=ok_embed(
                f"**{truncate(row['title'] or row['id'], 80)}** is now "
                f"{'enabled 🟢' if enabled else 'disabled ⚪'}."
            )
        )

    # ------------------------------------------------------- active rotation

    active = app_commands.Group(
        name="active", description="Which playlists the bot plays from"
    )

    async def _reshuffle_all(self) -> None:
        """The tracked playlists changed, which affects every server."""
        for station in self.bot.stations:
            await station.reshuffle()

    async def _reshuffle_guild(self, interaction: discord.Interaction) -> None:
        """This server picked a different rotation; nobody else is affected."""
        player = await self.bot.ensure_player(interaction.guild)
        await player.station.reshuffle()

    @active.command(
        name="show", description="Which playlists this server plays from"
    )
    async def active_show(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if interaction.guild is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return
        ids = await self.db.resolve_active_playlist_ids(interaction.guild.id)
        follow_all = await self.db.is_following_all(interaction.guild.id)
        rows = {row["id"]: row for row in await self.db.get_playlists()}

        if not ids:
            await interaction.followup.send(
                embed=err_embed(
                    "**Nothing is in rotation here** — the selected playlists "
                    "were removed or disabled. A DJ can fix this with "
                    "`/active all` or `/active set`."
                )
            )
            return

        lines = []
        for pid in ids:
            row = rows.get(pid)
            title = (row["title"] if row else None) or pid
            total, done = await self.db.playlist_counts(pid)
            lines.append(f"▶️ **{truncate(title, 60)}** — {done} playable")
        playable = len(await self.db.downloaded_ids(ids))
        embed = ok_embed(
            "\n".join(lines),
            f"In rotation in {interaction.guild.name}"
            + (" (following all playlists)" if follow_all else ""),
        )
        embed.set_footer(text=f"{playable} downloaded track(s) in the shuffle pool")
        await interaction.followup.send(embed=embed)

    @active.command(
        name="set", description="Play from one playlist only, here (DJ)"
    )
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def active_set(self, interaction: discord.Interaction, playlist: str) -> None:
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.followup.send(embed=err_embed(f"No playlist `{playlist}`."))
            return
        if not row["enabled"]:
            await interaction.followup.send(
                embed=err_embed(
                    "That playlist is disabled — enable it first with "
                    "`/playlist toggle`."
                )
            )
            return

        await self.db.set_active_playlists(interaction.guild.id, [row["id"]])
        await self._reshuffle_guild(interaction)
        playable = len(await self.db.downloaded_ids([row["id"]]))
        embed = ok_embed(
            f"**{interaction.guild.name}** now plays from "
            f"**{truncate(row['title'] or row['id'], 80)}** only.\n"
            f"{playable} downloaded track(s) in rotation here.",
            "Rotation changed",
        )
        if playable == 0:
            embed.color = WARN
            embed.description += (
                "\n\n⚠️ Nothing from it is downloaded yet — run `/sync`."
            )
        await interaction.followup.send(embed=embed)

    @active.command(
        name="add", description="Add a playlist to this server's rotation (DJ)"
    )
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def active_add(self, interaction: discord.Interaction, playlist: str) -> None:
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.followup.send(embed=err_embed(f"No playlist `{playlist}`."))
            return
        current = await self.db.resolve_active_playlist_ids(interaction.guild.id)
        if row["id"] in current:
            await interaction.followup.send(
                embed=err_embed("That playlist is already in rotation here.")
            )
            return
        current.append(row["id"])
        await self.db.set_active_playlists(interaction.guild.id, current)
        await self._reshuffle_guild(interaction)
        await interaction.followup.send(
            embed=ok_embed(
                f"Added **{truncate(row['title'] or row['id'], 70)}** in "
                f"**{interaction.guild.name}**.\n"
                f"{len(current)} playlist(s) in rotation here, "
                f"{len(await self.db.downloaded_ids(current))} track(s) playable."
            )
        )

    @active.command(
        name="remove", description="Drop a playlist from this server's rotation (DJ)"
    )
    @app_commands.autocomplete(playlist=playlist_autocomplete)
    async def active_remove(
        self, interaction: discord.Interaction, playlist: str
    ) -> None:
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()
        row = await self.db.find_playlist(playlist)
        if row is None:
            await interaction.followup.send(embed=err_embed(f"No playlist `{playlist}`."))
            return
        current = await self.db.resolve_active_playlist_ids(interaction.guild.id)
        if row["id"] not in current:
            await interaction.followup.send(
                embed=err_embed("That playlist isn't in rotation here.")
            )
            return
        current.remove(row["id"])
        if not current:
            await interaction.followup.send(
                embed=err_embed(
                    "That's the last one — this server would have nothing to "
                    "play. Use `/active set` to switch instead."
                )
            )
            return
        await self.db.set_active_playlists(interaction.guild.id, current)
        await self._reshuffle_guild(interaction)
        await interaction.followup.send(
            embed=ok_embed(
                f"Removed **{truncate(row['title'] or row['id'], 70)}** from "
                f"**{interaction.guild.name}**'s rotation.\n"
                f"{len(current)} playlist(s) left here."
            )
        )

    @active.command(
        name="all", description="Play from every enabled playlist, here (DJ)"
    )
    async def active_all(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_dj(interaction):
            return
        await interaction.response.defer()
        await self.db.set_active_playlists(interaction.guild.id, None)
        await self._reshuffle_guild(interaction)
        ids = await self.db.resolve_active_playlist_ids(interaction.guild.id)
        await interaction.followup.send(
            embed=ok_embed(
                f"**{interaction.guild.name}** now follows **all {len(ids)} "
                f"enabled playlist(s)** — "
                f"{len(await self.db.downloaded_ids(ids))} track(s) in rotation.\n"
                "Newly added playlists join automatically."
            )
        )

    # ------------------------------------------------------------ maintenance

    @app_commands.command(name="stats", description="Library totals")
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        stats = await self.db.stats()
        playlists = await self.db.get_playlists()
        rows = await self.db.fetchall(
            "SELECT COALESCE(SUM(duration), 0) AS secs FROM tracks "
            "WHERE status = 'downloaded'"
        )
        total_secs = rows[0]["secs"] if rows else 0
        size = 0
        if self.cfg.audio_dir.exists():
            size = sum(f.stat().st_size for f in self.cfg.audio_dir.glob("*.mp3"))

        embed = discord.Embed(title="Library", color=INFO)
        embed.add_field(name="Playlists", value=str(len(playlists)), inline=True)
        embed.add_field(name="Tracks", value=str(stats.get("total", 0)), inline=True)
        embed.add_field(
            name="Downloaded", value=str(stats.get("downloaded", 0)), inline=True
        )
        embed.add_field(name="Pending", value=str(stats.get("pending", 0)), inline=True)
        embed.add_field(name="Failed", value=str(stats.get("failed", 0)), inline=True)
        embed.add_field(
            name="Unavailable", value=str(stats.get("skipped", 0)), inline=True
        )
        embed.add_field(name="Playtime", value=fmt_duration(total_secs), inline=True)
        embed.add_field(name="On disk", value=f"{size / (1024 ** 3):.2f} GB", inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="sync", description="Re-read playlists and download new songs (owner)"
    )
    @app_commands.describe(retry_failed="Also retry tracks that previously failed")
    async def sync(
        self, interaction: discord.Interaction, retry_failed: bool = False
    ) -> None:
        if await self._deny_non_owner(interaction):
            return
        if self.bot.downloader.busy:
            await interaction.response.send_message(
                embed=err_embed(
                    f"A sync is already running ({self.bot.downloader.state}, "
                    f"{self.bot.downloader.remaining} track(s) left)."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=ok_embed(
                "Reading playlists and downloading anything new. "
                "Watch `/status` for progress.",
                "Sync started",
            )
        )
        try:
            report = await self.bot.downloader.sync(retry_failed=retry_failed)
        except Exception as exc:  # noqa: BLE001
            log.exception("manual sync failed")
            await interaction.followup.send(
                embed=err_embed(f"Sync failed:\n```{truncate(str(exc), 400)}```")
            )
            return

        embed = ok_embed(report.summary(), "Sync complete")
        if report.errors:
            embed.color = WARN
            embed.add_field(
                name="Problems",
                value="\n".join(f"• {truncate(e, 120)}" for e in report.errors[:5]),
                inline=False,
            )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="prune", description="Delete downloads no longer in any playlist (owner)"
    )
    async def prune(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer()
        removed = await self.bot.downloader.prune(delete_files=True)
        await self._reshuffle_all()
        await interaction.followup.send(
            embed=ok_embed(f"Removed **{removed}** orphaned track(s).")
        )
