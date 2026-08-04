"""Small formatting/UI helpers shared by the cogs."""

from __future__ import annotations

import discord

OK = 0x57F287
WARN = 0xFEE75C
ERR = 0xED4245
INFO = 0x5865F2


# What the bot actually needs: read/post in a text channel to answer commands,
# plus join and stream in a voice channel. Nothing more.
REQUIRED_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    connect=True,
    speak=True,
)

# Both scopes are required: "bot" alone registers no slash commands.
INVITE_SCOPES = ("bot", "applications.commands")


def invite_url(application_id: int | str | None) -> str:
    if not application_id:
        return ""
    return discord.utils.oauth_url(
        application_id, permissions=REQUIRED_PERMISSIONS, scopes=INVITE_SCOPES
    )


def fmt_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def truncate(text: str, limit: int = 90) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def embed(title: str, description: str = "", color: int = INFO) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def ok_embed(description: str, title: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=OK)


def err_embed(description: str, title: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=ERR)


def progress_bar(fraction: float, width: int = 18) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "▬" * filled + "🔘" + "▬" * max(0, width - filled - 1)


class Paginator(discord.ui.View):
    """Button-paged embed. Anyone can flip pages."""

    def __init__(self, pages: list[discord.Embed], *, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages or [discord.Embed(description="Nothing to show.")]
        self.index = 0
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        pass

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def send(self, interaction: discord.Interaction, ephemeral: bool = False) -> None:
        self._sync()
        if len(self.pages) == 1:
            await interaction.followup.send(embed=self.pages[0], ephemeral=ephemeral)
            return
        await interaction.followup.send(
            embed=self.pages[0], view=self, ephemeral=ephemeral
        )
        if not ephemeral:
            self.message = await interaction.original_response()


def build_pages(
    title: str, lines: list[str], per_page: int = 15, color: int = INFO, footer: str = ""
) -> list[discord.Embed]:
    if not lines:
        return [discord.Embed(title=title, description="_Nothing here yet._", color=color)]
    pages: list[discord.Embed] = []
    for start in range(0, len(lines), per_page):
        chunk = lines[start : start + per_page]
        page = discord.Embed(title=title, description="\n".join(chunk), color=color)
        tail = f"{len(lines)} entries"
        page.set_footer(text=f"{footer} • {tail}" if footer else tail)
        pages.append(page)
    return pages
