"""Playback commands.

Every command here acts on the caller's own server: each guild has its own
station, so its queue, current track and volume are its own. What is shared
is the library underneath — the downloaded files and the playlists in
rotation — which is why /search and /play offer the same songs everywhere.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..player import LEAVE_GRACE_SECONDS, GuildPlayer, QueueItem, Track
from ..utils import (
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

    # ---------------------------------------------------------------- helpers

    async def _player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        if interaction.guild is None:
            return None
        return await self.bot.ensure_player(interaction.guild)

    async def _scope(self, interaction: discord.Interaction) -> list[str] | None:
        """Playlists this server may request from — None means the whole library.

        Rotation is per server, so the scope is too: a song offered in one
        server may not be offered in another.
        """
        if not self.cfg.restrict_requests_to_active or interaction.guild is None:
            return None
        return await self.db.resolve_active_playlist_ids(interaction.guild.id)

    async def song_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        rows = await self.db.search_tracks(
            current, limit=25, playlist_ids=await self._scope(interaction)
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

    async def _resolve(
        self, interaction: discord.Interaction, query: str
    ) -> Track | None:
        """Only ever returns a downloaded track from a playlist in rotation.

        search_tracks matches an exact video id as well as free text, so an id
        pasted by hand is scoped the same way an autocompleted one is.
        """
        rows = await self.db.search_tracks(
            query, limit=1, playlist_ids=await self._scope(interaction)
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

        track = await self._resolve(interaction, query)
        if track is None:
            await interaction.followup.send(
                embed=err_embed(
                    f"No **downloaded** track matches `{truncate(query, 60)}`.\n"
                    "Only songs already downloaded from the playlist(s) in "
                    "this server's rotation can be played — try `/search`, or "
                    "`/active show` to see what's in rotation here."
                ),
                ephemeral=True,
            )
            return

        item = QueueItem(
            track=track,
            requester_id=interaction.user.id,
            requester_name=interaction.user.display_name,
        )
        station = player.station
        try:
            position = station.enqueue(item, front=front)
        except ValueError as exc:
            await interaction.followup.send(embed=err_embed(str(exc)), ephemeral=True)
            return

        if play_now:
            station.skip()
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
        embed.set_footer(
            text=f"{fmt_duration(track.duration)} • requested by "
            f"{interaction.user.display_name} • {station.listener_count()} "
            f"listener(s) in this server"
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

    queue_group = app_commands.Group(name="queue", description="Manage this server's request queue")

    @queue_group.command(name="add", description="Add a downloaded song to the queue")
    @app_commands.describe(song="Start typing to search the downloaded library")
    @app_commands.autocomplete(song=song_autocomplete)
    async def queue_add(self, interaction: discord.Interaction, song: str) -> None:
        await self._add(interaction, song, front=False, play_now=False)

    @queue_group.command(name="list", description="Show this server's request queue")
    async def queue_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        station = player.station
        lines: list[str] = []
        for index, item in enumerate(station.queue, start=1):
            who = f" — {item.requester_name}" if item.requester_name else ""
            lines.append(
                f"`{index:>2}.` **{truncate(item.track.title, 70)}** "
                f"`{fmt_duration(item.track.duration)}`{who}"
            )

        header = f"Request queue • {interaction.guild.name}"
        if station.current is not None:
            header += f" • now: {truncate(station.current.track.title, 40)}"
        pages = build_pages(header, lines, per_page=10, color=INFO)
        if not lines:
            pages[0].description = (
                "_Queue is empty — this server is shuffling the library._\n"
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
        count = player.station.clear_queue()
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
        item = player.station.remove_at(position)
        if item is None:
            await interaction.response.send_message(
                embed=err_embed(f"Nothing at position **{position}**."), ephemeral=True
            )
            return
        await interaction.response.send_message(
            embed=ok_embed(f"Removed **{truncate(item.track.title, 100)}** from the queue.")
        )

    # ------------------------------------------------------------- transport

    @app_commands.command(name="skip", description="Skip the current track in this server")
    async def skip(self, interaction: discord.Interaction) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return
        if not player.station.skip():
            await interaction.response.send_message(
                embed=err_embed("Nothing is playing here."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=ok_embed("Skipped ⏭"))

    @app_commands.command(name="nowplaying", description="What this server is playing right now")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = await self._player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=err_embed("Use this in a server."), ephemeral=True
            )
            return
        station = player.station
        item = station.current
        if item is None:
            await interaction.response.send_message(
                embed=err_embed(
                    "Nothing is playing in this server."
                    + (
                        "\nPlayback is paused because no one is listening."
                        if station.listener_count() == 0
                        else ""
                    )
                ),
                ephemeral=True,
            )
            return

        track = item.track
        elapsed = station.elapsed
        total = track.duration or 0
        bar = progress_bar(elapsed / total if total else 0.0)
        embed = discord.Embed(
            title="Playing in this server",
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
            text=f"{source} • {station.listener_count()} listener(s) here • "
            f"{len(station.queue)} queued • volume {int(station.volume * 100)}%"
        )
        embed.url = f"https://youtu.be/{track.video_id}"
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="search", description="Search the downloaded library")
    @app_commands.describe(query="Title or channel name")
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        rows = await self.db.search_tracks(
            query, limit=100, playlist_ids=await self._scope(interaction)
        )
        lines = [
            f"**{truncate(row['title'], 70)}** `{fmt_duration(row['duration'])}`"
            + (f"\n　{row['uploader']}" if row["uploader"] else "")
            for row in rows
        ]
        pages = build_pages(f"Search: {truncate(query, 40)}", lines, per_page=10)
        await Paginator(pages).send(interaction)

    @app_commands.command(name="shuffle", description="Reshuffle this server's auto-play rotation")
    async def shuffle(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return
        count = await player.station.reshuffle()
        await interaction.followup.send(
            embed=ok_embed(f"Reshuffled **{count}** downloaded track(s) 🔀")
        )

    @app_commands.command(name="volume", description="Show or set this server's playback volume")
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
                embed=ok_embed(
                    f"Volume is **{int(player.station.volume * 100)}%** in this server"
                )
            )
            return
        if not self.cfg.is_dj(interaction.user):
            await interaction.response.send_message(
                embed=err_embed("Only DJs / server managers can change the volume."),
                ephemeral=True,
            )
            return
        value = await player.station.set_volume(percent / 100)
        await interaction.response.send_message(
            embed=ok_embed(f"Volume set to **{int(value * 100)}%** 🔊")
        )

    # ------------------------------------------------------------ connection

    @app_commands.command(
        name="join", description="Bring the bot into your voice channel"
    )
    async def join(self, interaction: discord.Interaction) -> None:
        await self._join_caller(interaction, "join")

    @app_commands.command(name="summon", description="Alias for /join")
    async def summon(self, interaction: discord.Interaction) -> None:
        await self._join_caller(interaction, "summon")

    async def _join_caller(self, interaction: discord.Interaction, name: str) -> None:
        """Pull the bot into the caller's channel.

        Outside the 24/7 channels this is the only way in — the bot never picks
        a channel by itself — so it stays here until the channel empties out.
        """
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        voice_state = getattr(interaction.user, "voice", None)
        channel = voice_state.channel if voice_state else None
        if channel is None:
            await interaction.followup.send(
                embed=err_embed(f"Join a voice channel first, then run `/{name}` again.")
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

        player.rejoin_blocked_until = 0.0  # an explicit ask overrides /leave
        vc = await player.connect(channel)
        if vc is None:
            await interaction.followup.send(
                embed=err_embed(
                    f"Couldn't join {channel.mention}: {player.last_connect_error}"
                )
            )
            return
        if player.is_home(vc.channel.id):
            note = "my 24/7 channel"
        else:
            note = (
                f"I'll stay until it's been empty for "
                f"{self.cfg.idle_stop_after // 60} min, then head back to 24/7 duty"
            )
        await interaction.followup.send(
            embed=ok_embed(f"Joined {vc.channel.mention} — {note}.")
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
        player.pinned_channel_id = None  # back to 24/7 duty, wherever we were
        player.rejoin_blocked_until = 0.0
        vc = await player.connect()
        if vc is None:
            await interaction.followup.send(
                embed=err_embed(
                    "No 24/7 channel is available here "
                    f"({player.last_connect_error or 'none configured'}).\n"
                    "Use `/join` from inside a channel I can access."
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
        player.pinned_channel_id = None
        player.active_channel_id = None
        player.block_rejoin()  # otherwise the auto-rejoin undoes this at once
        await vc.disconnect(force=True)
        minutes = LEAVE_GRACE_SECONDS // 60 or 1
        note = (
            f"I'll rejoin my 24/7 channel in ~{minutes} min "
            "(or right away with `/rejoin`)"
            if player.home_channels()
            else "I'll stay out until someone runs `/join`"
        )
        await interaction.response.send_message(
            embed=ok_embed(f"Disconnected. {note}.")
        )

    # ---------------------------------------------------------------- status

    @app_commands.command(name="status", description="This server's playback, plus library and sync status")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        player = await self._player(interaction)
        if player is None:
            await interaction.followup.send(embed=err_embed("Use this in a server."))
            return

        stats = await self.db.stats()
        dl = self.bot.downloader
        station = player.station

        if player.voice_client and player.voice_client.is_connected():
            channel = player.channel
            listeners = player.human_count()
            tag = "24/7" if player.is_home(player.active_channel_id) else "guest"
            conn = (
                f"🔊 {channel.mention if channel else '?'} "
                f"({tag}) • {listeners} listener(s)"
            )
            if listeners == 0:
                conn += "\n⏸ silent — channel empty, holding the connection"
            elif not player.is_playing_current():
                conn += "\n📡 starting the next track…"
        else:
            conn = "❌ not connected"
            waiting = player.rejoin_blocked_until - time.monotonic()
            if waiting > 0:
                conn += f"\n⏳ `/leave` — rejoining in {int(waiting)}s"
            elif player.last_connect_error:
                conn += f"\n`{truncate(player.last_connect_error, 150)}`"
            conn += "\nUse `/join` to pull me into your channel."

        embed = discord.Embed(
            title=f"Status — {interaction.guild.name}",
            description=(
                "Playback is **this server's own**; the library, rotation and "
                "downloads below are shared with every server I'm in."
            ),
            color=OK if player.voice_client else WARN,
        )
        embed.add_field(name="Voice", value=conn, inline=False)
        embed.add_field(
            name="Playing here",
            value=(
                f"**{truncate(station.current.track.title, 55)}**\n"
                f"`{fmt_duration(station.elapsed)} / "
                f"{fmt_duration(station.current.track.duration)}`"
                if station.current
                else (
                    "waiting for downloads"
                    if station.waiting_for_tracks
                    else "paused — nobody here is listening"
                )
            ),
            inline=False,
        )
        embed.add_field(name="Queue", value=str(len(station.queue)), inline=True)
        embed.add_field(name="Shuffle bag", value=str(station.bag_size), inline=True)
        embed.add_field(
            name="Volume", value=f"{int(station.volume * 100)}%", inline=True
        )

        others = [p for p in self.bot.players.values() if p.guild.id != player.guild.id]
        if others:
            playing = sum(1 for p in others if p.station.current is not None)
            audience = sum(p.human_count() for p in others)
            embed.add_field(
                name="Other servers",
                value=(
                    f"{playing} of {len(others)} playing • {audience} listener(s) "
                    "— separate queues, nothing you do here reaches them"
                ),
                inline=False,
            )

        active_ids = await self.db.resolve_active_playlist_ids(player.guild.id)
        playlists = {row["id"]: row for row in await self.db.get_playlists()}
        if active_ids:
            names = []
            for pid in active_ids[:4]:
                row = playlists.get(pid)
                names.append(truncate((row["title"] if row else None) or pid, 30))
            rotation = ", ".join(names)
            if len(active_ids) > 4:
                rotation += f" +{len(active_ids) - 4} more"
            if await self.db.is_following_all(player.guild.id):
                rotation += "\n_(following all enabled playlists)_"
        else:
            rotation = "⚠️ nothing selected here — a DJ can run `/active all`"
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
