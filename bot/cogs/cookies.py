"""Cookie management — how the bot gets past YouTube's "confirm you're not a bot".

All owner-only and all ephemeral: a cookie jar is a live YouTube session for
whoever exported it.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils import INFO, OK, WARN, err_embed, ok_embed, truncate

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024

BROWSERS = [
    "firefox",
    "chrome",
    "chromium",
    "brave",
    "edge",
    "opera",
    "vivaldi",
    "safari",
    "whale",
]


class CookiesCog(commands.Cog, name="Cookies"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.dl = bot.downloader

    async def _deny_non_owner(self, interaction: discord.Interaction) -> bool:
        if await self.bot.is_bot_owner(interaction.user):
            return False
        await interaction.response.send_message(
            embed=err_embed(
                "Only the **bot owner** can manage cookies — they're a live "
                "YouTube login."
            ),
            ephemeral=True,
        )
        return True

    group = app_commands.Group(
        name="cookies", description="YouTube authentication (owner only)"
    )

    # ------------------------------------------------------------------ guide

    @group.command(name="guide", description="How to get YouTube cookies")
    async def guide(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return

        embed = discord.Embed(
            title="Getting YouTube cookies",
            description=(
                "YouTube is asking the bot to *“sign in to confirm you're not a "
                "bot”*. It needs a cookie jar from a logged-in session.\n\n"
                "**Use a throwaway Google account.** Bulk downloading can get an "
                "account rate-limited or banned — don't use your main one."
            ),
            color=INFO,
        )
        embed.add_field(
            name="1 · Install a cookie exporter",
            value=(
                "Add a **“Get cookies.txt LOCALLY”** extension to your browser "
                "([Chrome](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) "
                "· [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/))."
            ),
            inline=False,
        )
        embed.add_field(
            name="2 · Export from a private window",
            value=(
                "This exact order matters — YouTube rotates cookies on any open "
                "tab, which silently invalidates a normal export:\n"
                "① Open a **private / incognito** window\n"
                "② Log in to YouTube\n"
                "③ In that same tab go to `youtube.com/robots.txt`\n"
                "④ Export cookies with the extension → `cookies.txt`\n"
                "⑤ **Close the private window** before doing anything else"
            ),
            inline=False,
        )
        embed.add_field(
            name="3 · Give the file to the bot",
            value=(
                "**Best (self-hosted):** copy it straight onto the host — nothing "
                f"leaves your machine:\n```\n{self.cfg.cookies_file}\n```\n"
                "**Or:** `/cookies upload` and attach the file. ⚠️ That sends your "
                "session through Discord's servers — prefer doing it in a DM with "
                "the bot, and delete the message afterwards."
            ),
            inline=False,
        )
        embed.add_field(
            name="4 · Verify",
            value="`/cookies status` then `/cookies test`, then `/sync`.",
            inline=False,
        )
        embed.set_footer(
            text="Cookies expire — if downloads start failing again, re-export."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- status

    @group.command(name="status", description="Are cookies installed and healthy?")
    async def status(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return
        info = self.dl.cookie_status()

        if info["exists"]:
            age = info["age_hours"]
            age_text = f"{age:.0f}h old" if age >= 1 else f"{age * 60:.0f}m old"
            body = (
                f"✅ **Installed** — {info['youtube_cookies']} YouTube cookie(s), "
                f"{info['size']} bytes, {age_text}"
            )
            if info["auth_cookies"]:
                body += (
                    f"\n🔑 Login cookies: `{', '.join(info['auth_cookies'][:6])}`"
                )
            else:
                body += "\n⚠️ **No login cookies** — exported while signed out."
            colour = OK if info["auth_cookies"] else WARN
        else:
            body = "❌ **No cookie file.** Run `/cookies guide`."
            colour = WARN

        if info["blocked"]:
            body += (
                "\n\n🚫 YouTube is currently **blocking downloads** "
                "(sign-in demanded). Fresh cookies should clear it."
            )
            colour = WARN

        embed = discord.Embed(title="Cookie status", description=body, color=colour)
        embed.add_field(name="Path", value=f"`{info['path']}`", inline=False)
        if info["browser"]:
            embed.add_field(
                name="Browser fallback", value=f"`{info['browser']}`", inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- test

    @group.command(name="test", description="Ask YouTube for a video to check cookies")
    async def test(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.dl.test_cookies()
        await interaction.followup.send(
            embed=(ok_embed if ok else err_embed)(message, "Cookie test"),
            ephemeral=True,
        )

    # ----------------------------------------------------------------- upload

    @group.command(name="upload", description="Attach a cookies.txt file")
    @app_commands.describe(file="cookies.txt exported in Netscape format")
    async def upload(
        self, interaction: discord.Interaction, file: discord.Attachment
    ) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if file.size > MAX_UPLOAD_BYTES:
            await interaction.followup.send(
                embed=err_embed(
                    f"That file is {file.size // 1024} KB — cookie jars are a few "
                    "KB. Are you sure it's the right file?"
                ),
                ephemeral=True,
            )
            return

        try:
            raw = await file.read()
            text = raw.decode("utf-8", errors="replace")
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=err_embed(f"Couldn't download the attachment: {exc}"),
                ephemeral=True,
            )
            return

        ok, message = await self.dl.install_cookie_text(text)
        if not ok:
            await interaction.followup.send(
                embed=err_embed(message, "Rejected"), ephemeral=True
            )
            return

        # Anything that failed the bot-check should get another go.
        requeued = await self.bot.db.reset_failures()
        ok_test, test_message = await self.dl.test_cookies()

        embed = ok_embed(
            f"Installed — {message}\n"
            f"Re-queued **{requeued}** previously failed track(s).\n\n"
            f"{'✅' if ok_test else '⚠️'} {test_message}",
            "Cookies updated",
        )
        if ok_test:
            embed.set_footer(text="Run /sync to start downloading.")
        else:
            embed.color = WARN
        await interaction.followup.send(embed=embed, ephemeral=True)

        if interaction.guild is not None:
            log.info("cookies replaced by %s", interaction.user)

    # ---------------------------------------------------------------- browser

    @group.command(
        name="browser", description="Try importing cookies from a local browser"
    )
    @app_commands.describe(
        browser="Browser installed on the machine running the bot",
        profile="Optional profile name",
    )
    @app_commands.choices(
        browser=[app_commands.Choice(name=b, value=b) for b in BROWSERS]
    )
    async def browser(
        self,
        interaction: discord.Interaction,
        browser: app_commands.Choice[str],
        profile: str | None = None,
    ) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        ok, message = await self.dl.import_browser_cookies(browser.value, profile)
        if not ok:
            await interaction.followup.send(
                embed=err_embed(
                    f"{truncate(message, 400)}\n\n"
                    "This only works if that browser is installed **on the same "
                    "machine as the bot** and logged in. In Docker or WSL it "
                    "usually can't see your browser at all — use "
                    "`/cookies upload` instead (`/cookies guide`).",
                    "Import failed",
                ),
                ephemeral=True,
            )
            return

        requeued = await self.bot.db.reset_failures()
        ok_test, test_message = await self.dl.test_cookies()
        embed = ok_embed(
            f"{message}\nRe-queued **{requeued}** failed track(s).\n\n"
            f"{'✅' if ok_test else '⚠️'} {test_message}",
            "Cookies imported",
        )
        if not ok_test:
            embed.color = WARN
            embed.description += (
                "\n\n_YouTube rotates cookies on open tabs, so browser imports "
                "often fail. The private-window export in `/cookies guide` is "
                "more reliable._"
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ clear

    @group.command(name="clear", description="Delete the stored cookie file")
    async def clear(self, interaction: discord.Interaction) -> None:
        if await self._deny_non_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        removed = await self.dl.clear_cookies()
        await interaction.followup.send(
            embed=ok_embed(
                "Cookie file deleted. Public videos still download; anything "
                "needing sign-in will fail."
                if removed
                else "There was no cookie file to delete."
            ),
            ephemeral=True,
        )
