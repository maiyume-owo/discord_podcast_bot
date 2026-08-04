"""/help and /invite.

The command list is built from the live command tree rather than hand-written,
so a new command shows up in /help automatically. Only the permission tier is
declared here; anything unclassified falls through to "everyone" and is still
listed, so nothing can silently go missing.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import INFO, OK, REQUIRED_PERMISSIONS, err_embed, invite_url

log = logging.getLogger(__name__)

# Full names ("playlist add") or whole groups ("cookies").
OWNER_COMMANDS = {
    "cookies",
    "sync",
    "prune",
    "playlist add",
    "playlist remove",
    "playlist toggle",
    "active set",
    "active add",
    "active remove",
    "active all",
}
DJ_COMMANDS = {"volume", "leave"}

BLURBS = {
    "everyone": "Anyone can use these. Requests are shared — see the note above.",
    "dj": "Needs **Manage Server**, or a role listed in `DJ_ROLE_IDS`.",
    "owner": "Bot owner only.",
}


class MetaCog(commands.Cog, name="Meta"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg

    # ------------------------------------------------------------------ tiers

    @staticmethod
    def _tier(full_name: str) -> str:
        root = full_name.split(" ")[0]
        if full_name in OWNER_COMMANDS or root in OWNER_COMMANDS:
            return "owner"
        if full_name in DJ_COMMANDS or root in DJ_COMMANDS:
            return "dj"
        return "everyone"

    def _walk(self) -> dict[str, list[tuple[str, str]]]:
        """Every command in the tree, bucketed by who may run it."""
        buckets: dict[str, list[tuple[str, str]]] = {
            "everyone": [],
            "dj": [],
            "owner": [],
        }
        for command in self.bot.tree.get_commands():
            children = getattr(command, "commands", None)
            if children:
                for sub in children:
                    full = f"{command.name} {sub.name}"
                    buckets[self._tier(full)].append((full, sub.description))
            else:
                buckets[self._tier(command.name)].append(
                    (command.name, command.description)
                )
        for rows in buckets.values():
            rows.sort()
        return buckets

    @staticmethod
    def _render(rows: list[tuple[str, str]], limit: int = 1024) -> str:
        """Group subcommands onto one line so the whole tier fits in a field."""
        grouped: dict[str, list[str]] = {}
        plain: list[str] = []
        for name, desc in rows:
            if " " in name:
                root, sub = name.split(" ", 1)
                grouped.setdefault(root, []).append(sub)
            else:
                plain.append(f"`/{name}` — {desc}")
        lines = plain + [
            f"`/{root} {'|'.join(subs)}`" for root, subs in sorted(grouped.items())
        ]
        out = "\n".join(lines) or "_none_"
        return out if len(out) <= limit else out[: limit - 1] + "…"

    # ------------------------------------------------------------------- help

    @app_commands.command(name="help", description="What this bot does and how to use it")
    async def help(self, interaction: discord.Interaction) -> None:
        buckets = self._walk()
        total = sum(len(v) for v in buckets.values())

        embed = discord.Embed(
            title="📻 How this bot works",
            description=(
                "It runs a **24/7 radio station**, not a per-server jukebox.\n\n"
                "Every server hears **the same song at the same moment**, from one "
                "shared queue. A skip or a request in any server changes what "
                "everyone hears — and joining mid-song drops you in where the "
                "broadcast already is.\n\n"
                "Only songs already **downloaded** can be requested; `/play` "
                "autocompletes from the library."
            ),
            color=INFO,
        )
        embed.add_field(
            name="🎧 Everyone",
            value=self._render(buckets["everyone"]),
            inline=False,
        )
        embed.add_field(
            name=f"🎚️ DJ — {BLURBS['dj']}",
            value=self._render(buckets["dj"]),
            inline=False,
        )
        embed.add_field(
            name=f"🔧 Owner — {BLURBS['owner']}",
            value=self._render(buckets["owner"]),
            inline=False,
        )
        embed.add_field(
            name="Start here",
            value=(
                "`/status` — what's on air and how the library is doing\n"
                "`/play <song>` — hear something now\n"
                "`/summon` — pull the bot into your voice channel"
            ),
            inline=False,
        )
        embed.set_footer(text=f"{total} commands • /invite to add me to a server")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- invite

    @app_commands.command(name="invite", description="Get a link to add me to a server")
    async def invite(self, interaction: discord.Interaction) -> None:
        link = invite_url(self.bot.application_id)
        if not link:
            await interaction.response.send_message(
                embed=err_embed(
                    "I don't know my own application id yet — try again in a "
                    "moment, once I've finished connecting."
                ),
                ephemeral=True,
            )
            return

        granted = ", ".join(
            name.replace("_", " ").title()
            for name, value in REQUIRED_PERMISSIONS
            if value
        )
        embed = discord.Embed(
            title="Add me to a server",
            description=f"**[Click here to invite me]({link})**",
            color=OK,
        )
        embed.add_field(name="Permissions requested", value=granted, inline=False)
        embed.add_field(
            name="Why both scopes",
            value=(
                "The link includes `bot` **and** `applications.commands`. Without "
                "the second one my slash commands never register and I'd be "
                "unusable."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚠️ One station, all servers",
            value=(
                "I broadcast the *same* stream everywhere I'm added, from one "
                "shared queue — so anyone in a new server can skip and queue for "
                "**your** listeners too. Only add me where you trust the members."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
