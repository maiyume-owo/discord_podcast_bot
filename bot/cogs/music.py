"""Playback commands.

Queue, skip and now-playing act on the Station — one shared broadcast — so a
request made in any server changes what every server hears. Only /summon,
/rejoin and /leave are per-guild, since those are about the connection.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..player import GuildPlayer, QueueItem, Track
from ..utils import (
    ERR,
    INFO,
    OK,
    WARN,
    Paginator,
    build_pages,
    err_embed,
    fmt_duration,
    ok_embed,
    progress_bar,
    truncate,
)

log = logging.getLogger(__name__)


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    @property
    def station(self):
        return self.bot.station

    # ---------------------------------------------------------------- helpers

    async def _player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        if interaction.guild is None:
            return None
        return await self.bot.ensure_player(interaction.guild)

    async def _scope(self) -> list[str] | None:
        """Playlists users may request from — None means the whole library."""
        if not self.cfg.restrict_requests_to_active:
            return None
        return await self.db.resolve_active_playlist_ids()

    async def song_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await self.db.search_tracks(
            current, limit=25, playlist_ids=await self._scope()
        )
        return [
            app_commands.Choice(
                name=truncate(
                    f"{row['title']}"
                    + (f" — {row['uploader']}" if row["uploader"] else "")
                    + (f" [{fmt_duration(row['duration'])}]" if row["duration"] else ""),
                    100,
                ),
                value=row["video_id"],
            )
            for row in rows
        ]

    async def _resolve(self, query: str) -> Track | None:
        """Only ever returns a downloaded track from a playlist in rotation.

        search_tracks matches an exact video id as well as free text, so an id
        pasted by hand is scoped the same way an autocompleted one is.
        """
        rows = await self.db.search_tracks(
            query, limit=1, playlist_ids=await self._scope()
        )
        if not rows:
            return None
        return Track.from_row(rows[0], self.cfg.audio_dir)

    async def _add(
        self, interaction: discord.Interaction, query: str, front: bool, play_now: bool
    ) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return

        track = await self._resolve(query)
        if track is None:
            await interaction.followup.send(
                embed=err_embed(
                    f"No **downloaded** track matches `{truncate(query, 60)}`.\n"
                    "Only songs already downloaded from the playlist(s) in "
                    "rotation can be played — try `/search`, or `/active show` "
                    "to see what's in rotation."
                ),
                ephemeral=True,
            )
            return

        item = QueueItem(
            track=track,
            requester_id=interaction.user.id,
            requester_name=interaction.user.display_name,
        )
        try:
            position = self.station.enqueue(item, front=front)
        except ValueError as exc:
            await interaction.followup.send(embed=err_embed(str(exc)), ephemeral=True)
            return

        if play_now:
            self.station.skip()
            verb = "Now playing"
        elif front:
            verb = "Up next"
        else:
            verb = f"Queued (#{position})"

        embed = ok_embed(
            f"**{truncate(track.title, 120)}**"
            + (f"\n{track.uploader}" if track.uploader else ""),
            verb,
        )
        listeners = self.station.listener_count()
        embed.set_footer(
            text=f"{fmt_duration(track.duration)} • requested by "
            f"{interaction.user.display_name} • airing to {listeners} listener(s)"
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------- play/queue

    @app_commands.command(name="play", description="Play a downloaded song right now")
    @app_commands.describe(song="Start typing to search the downloaded library")
    @app_commands.autocomplete(song=song_autocomplete)
    async def play(self, interaction: discord.Interaction, song: str) -> None:
        await self._add(interaction, song, front=True, play_now=True)

    @app_commands.command(
        name="playnext", description="Put a song at the top of the queue (plays next)"
    )
    @app_commands.describe(song="Start typing to search the downloaded library")
    @app_commands.autocomplete(song=song_autocomplete)
    async def playnext(self, interaction: discord.Interaction, song: str) -> None:
        await self._add(interaction, song, front=True, play_now=False)

    queue_group = app_commands.Group(name="queue", description="Manage the request queue")

    @queue_group.command(name="add", description="Add a downloaded song to the queue")
    @app_commands.describe(song="Start typing to search the downloaded library")
    @app_commands.autocomplete(song=song_autocomplete)
    async def queue_add(self, interaction: discord.Interaction, song: str) -> None:
        await self._add(interaction, song, front=False, play_now=False)

    @queue_group.command(name="list", description="Show the request queue")
    async def queue_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        lines: list[str] = []
        for index, item in enumerate(self.station.queue, start=1):
            who = f" — {item.requester_name}" if item.requester_name else ""
            lines.append(
                f"`{index:>2}.` **{truncate(item.track.title, 70)}** "
                f"`{fmt_duration(item.track.duration)}`{who}"
            )

        header = "Request queue"
        if self.station.current is not None:
            header += f" • now: {truncate(self.station.current.track.title, 40)}"
        pages = build_pages(header, lines, per_page=10, color=INFO)
        if not lines:
            pages[0].description = (
                "_Queue is empty — the station is shuffling the library._\n"
                "Add something with `/queue add` or `/playnext`."
            )
        view = Paginator(pages)
        await view.send(interaction)

    @queue_group.command(name="clear", description="Clear the request queue")
    async def queue_clear(self, interaction: discord.Interaction) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return
        count = self.station.clear_queue()
        await interaction.response.send_message(
            embed=ok_embed(
                f"Cleared **{count}** queued track(s). Shuffle play continues."
                if count
                else "The queue was already empty."
            )
        )

    @queue_group.command(name="remove", description="Remove one entry from the queue")
    @app_commands.describe(position="Position shown in /queue list")
    async def queue_remove(self, interaction: discord.Interaction, position: int) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return
        item = self.station.remove_at(position)
        if item is None:
            await interaction.response.send_message(
                embed=err_embed(f"Nothing at position **{position}**."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=ok_embed(f"Removed **{truncate(item.track.title, 100)}** from the queue.")
        )

    # ------------------------------------------------------------- transport

    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction) -> None:
        if not self.station.skip():
            await interaction.response.send_message(
                embed=err_embed("Nothing is playing."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=ok_embed("Skipped ⏭ — for every server tuned in.")
        )

    @app_commands.command(name="nowplaying", description="What's playing right now")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        item = self.station.current
        if item is None:
            await interaction.response.send_message(
                embed=err_embed(
                    "Nothing is on air right now."
                    + (
                        "\nThe broadcast is paused because no one is listening."
                        if self.station.listener_count() == 0
                        else ""
                    )
                ),
                ephemeral=True,
            )
            return

        track = item.track
        elapsed = self.station.elapsed
        total = track.duration or 0
        bar = progress_bar(elapsed / total if total else 0.0)
        embed = discord.Embed(
            title="On air",
            description=f"**{truncate(track.title, 120)}**"
            + (f"\n{track.uploader}" if track.uploader else ""),
            color=OK,
        )
        embed.add_field(
            name="\u200b",
            value=f"{bar}\n`{fmt_duration(elapsed)} / {fmt_duration(total)}`",
            inline=False,
        )
        source = (
            f"requested by {item.requester_name}"
            if item.requester_name
            else "shuffle"
        )
        embed.set_footer(
            text=f"{source} • {self.station.listener_count()} listener(s) across "
            f"{len(self.bot.players)} server(s) • {len(self.station.queue)} queued "
            f"• volume {int(self.station.volume * 100)}%"
        )
        embed.url = f"https://youtu.be/{track.video_id}"
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="search", description="Search the downloaded library")
    @app_commands.describe(query="Title or channel name")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        rows = await self.db.search_tracks(
            query, limit=100, playlist_ids=await self._scope()
        )
        lines = [
            f"**{truncate(row['title'], 70)}** `{fmt_duration(row['duration'])}`"
            + (f"\n　{row['uploader']}" if row["uploader"] else "")
            for row in rows
        ]
        pages = build_pages(f"Search: {truncate(query, 40)}", lines, per_page=10)
        await Paginator(pages).send(interaction)

    @app_commands.command(name="shuffle", description="Reshuffle the auto-play rotation")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return
        count = await self.station.reshuffle()
        await interaction.followup.send(
            embed=ok_embed(f"Reshuffled **{count}** downloaded track(s) 🔀")
        )

    @app_commands.command(name="volume", description="Show or set playback volume")
    @app_commands.describe(percent="0-200; omit to just show the current level")
    async def volume(
        self, interaction: discord.Interaction, percent: int | None = None
    ) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return
        if percent is None:
            await interaction.response.send_message(
                embed=ok_embed(f"Volume is **{int(self.station.volume * 100)}%**")
            )
            return
        if not self.cfg.is_dj(interaction.user):
            await interaction.response.send_message(
                embed=err_embed("Only DJs / server managers can change the volume."),
                ephemeral=True,
            )
            return
        value = await self.station.set_volume(percent / 100)
        await interaction.response.send_message(
            embed=ok_embed(f"Volume set to **{int(value * 100)}%** 🔊")
        )

    # ------------------------------------------------------------ connection

    @app_commands.command(name="summon", description="Bring the bot into your voice channel")
    async def summon(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        voice_state = getattr(interaction.user, "voice", None)
        channel = voice_state.channel if voice_state else None
        if channel is None:
            await interaction.followup.send(
                embed=err_embed("Join a voice channel first, then run `/summon` again.")
            )
            return

        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.followup.send(
                embed=err_embed(
                    f"I don't have **Connect**/**Speak** in {channel.mention}."
                )
            )
            return

        vc = await player.connect(channel)
        if vc is None:
            await interaction.followup.send(
                embed=err_embed(
                    f"Couldn't join {channel.mention}: {player.last_connect_error}"
                )
            )
            return
        await interaction.followup.send(
            embed=ok_embed(f"Joined {vc.channel.mention} — this is my home channel now.")
        )

    @app_commands.command(
        name="rejoin", description="Reconnect to the configured 24/7 channel"
    )
    async def rejoin(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return
        vc = player.voice_client
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
        vc = await player.connect()
        if vc is None:
            await interaction.followup.send(
                embed=err_embed(
                    "Still can't join anywhere: "
                    f"{player.last_connect_error or 'no joinable channel'}.\n"
                    "Use `/summon` from inside a channel I can access."
                )
            )
            return
        await interaction.followup.send(
            embed=ok_embed(f"Reconnected to {vc.channel.mention}.")
        )

    @app_commands.command(name="leave", description="Disconnect (DJ only)")
    async def leave(self, interaction: discord.Interaction) -> None:
        player = await self._player(interaction)
        if player is None:
            return
        if not self.cfg.is_dj(interaction.user):
            await interaction.response.send_message(
                embed=err_embed("Only DJs / server managers can do that."), ephemeral=True
            )
            return
        vc = player.voice_client
        if vc is None:
            await interaction.response.send_message(
                embed=err_embed("I'm not connected."), ephemeral=True
            )
            return
        await vc.disconnect(force=True)
        await interaction.response.send_message(
            embed=ok_embed("Disconnected. I'll auto-rejoin shortly — use `/summon` to steer me.")
        )

    # ---------------------------------------------------------------- status

    @app_commands.command(name="status", description="Connection, library and sync status")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        stats = await self.db.stats()
        dl = self.bot.downloader

        if player.voice_client and player.voice_client.is_connected():
            channel = player.channel
            listeners = player.human_count()
            conn = f"🔊 {channel.mention if channel else '?'} • {listeners} listener(s)"
            if player.paused_for_idle:
                conn += "\n⏸ paused (channel empty, staying connected)"
            elif player.idle_since is not None:
                conn += "\n💤 idle — stream torn down, holding the channel"
        else:
            conn = "❌ not connected"
            if player.last_connect_error:
                conn += f"\n`{truncate(player.last_connect_error, 150)}`"
            conn += "\nUse `/summon` to pull me into your channel."

        embed = discord.Embed(
            title="Status",
            color=OK if player.voice_client else WARN,
        )
        embed.add_field(name="Voice", value=conn, inline=False)
        embed.add_field(
            name="On air",
            value=(
                f"**{truncate(self.station.current.track.title, 55)}**\n"
                f"`{fmt_duration(self.station.elapsed)} / "
                f"{fmt_duration(self.station.current.track.duration)}`"
                if self.station.current
                else (
                    "waiting for downloads"
                    if self.station.waiting_for_tracks
                    else "paused — no listeners"
                )
            ),
            inline=False,
        )
        embed.add_field(name="Queue", value=str(len(self.station.queue)), inline=True)
        embed.add_field(name="Shuffle bag", value=str(self.station.bag_size), inline=True)
        embed.add_field(
            name="Audience",
            value=f"{self.station.listener_count()} across {len(self.bot.players)} server(s)",
            inline=True,
        )

        active_ids = await self.db.resolve_active_playlist_ids()
        playlists = {row["id"]: row for row in await self.db.get_playlists()}
        if active_ids:
            names = []
            for pid in active_ids[:4]:
                row = playlists.get(pid)
                names.append(truncate((row["title"] if row else None) or pid, 30))
            rotation = ", ".join(names)
            if len(active_ids) > 4:
                rotation += f" +{len(active_ids) - 4} more"
            if await self.db.is_following_all():
                rotation += "\n_(following all enabled playlists)_"
        else:
            rotation = "⚠️ nothing selected — owner: `/active all`"
        embed.add_field(name="Rotation", value=rotation, inline=False)

        embed.add_field(
            name="Library",
            value=(
                f"✅ {stats.get('downloaded', 0)} downloaded\n"
                f"⏳ {stats.get('pending', 0)} pending\n"
                f"⚠️ {stats.get('failed', 0)} failed • "
                f"⛔ {stats.get('skipped', 0)} unavailable"
            ),
            inline=False,
        )
        sync_state = dl.state
        if dl.state != "idle":
            sync_state += f" ({dl.remaining} left"
            sync_state += f", {truncate(dl.current, 40)})" if dl.current else ")"
        elif dl.last_report is not None:
            ago = int(time.time() - (dl.last_run or time.time()))
            sync_state += f" • last run {ago // 60}m ago: {dl.last_report.summary()}"
        if dl.bot_check_blocked:
            sync_state += (
                "\n🚫 **blocked by YouTube** — sign-in required. "
                "Owner: run `/cookies guide`"
            )
        embed.add_field(name="Downloader", value=sync_state, inline=False)
        await interaction.followup.send(embed=embed)
