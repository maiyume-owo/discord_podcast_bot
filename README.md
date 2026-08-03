# ytvc-bot

A Discord bot that keeps YouTube playlists downloaded as local mp3s and streams them into
a voice channel 24/7 on shuffle.

- Tracks any number of playlists; the **bot owner** picks which are in rotation
- Downloads everything with `yt-dlp` and converts to mp3 with `ffmpeg`
- Streams the **local files** — no per-play YouTube requests, no buffering
- Shuffles forever, with a user request queue on top
- **Pauses when the voice channel empties, but never leaves it**, and resumes from the
  exact position when someone comes back
- Falls back through configured channels → any joinable channel; `/summon` overrides
- Anyone can queue — but only songs already downloaded
- One `docker compose up -d` to deploy

---

## Quick start

```bash
cp .env.example .env
```

Fill in `DISCORD_TOKEN`, `GUILD_IDS`, `VOICE_CHANNEL_ID`, `OWNER_IDS`.

```bash
mkdir -p data && docker compose up -d --build
```

Then in Discord, as the owner:

```
/playlist add <playlist url>
/sync
```

Playback starts as soon as the first mp3 lands. `/status` shows progress throughout.

---

## Discord setup

1. **Developer Portal → New Application → Bot.** Token goes in `DISCORD_TOKEN`.
2. No privileged intents required. *Server Members* is optional — enable it and set
   `MEMBER_INTENT=true` only if you want other bots excluded when deciding whether a
   voice channel is empty.
3. **OAuth2 → URL Generator**: scopes **`bot` *and* `applications.commands`** — both, or
   slash commands can't register and the bot is useless. Permissions: **Connect**,
   **Speak**, **View Channel**, **Send Messages**, **Embed Links**.
4. Put your own user ID in `OWNER_IDS`. The application owner (or team members) counts
   automatically, so `OWNER_IDS` is only for adding *more* owners.

With `GUILD_IDS` set, slash commands appear immediately; otherwise a global sync can take
up to an hour.

---

## Cookies (optional)

Only needed for playlists or videos that aren't publicly viewable — private/unlisted
playlists, Watch Later (`WL`), Liked videos (`LL`), and age-restricted or region-locked
videos. Public playlists need nothing.

Export with a "Get cookies.txt LOCALLY" browser extension in **Netscape** format and save
to `data/cookies.txt`. It's picked up automatically if present, ignored if not.

> Keep that file private — it's a live YouTube session. Download only what you have
> access to, and stay within YouTube's Terms of Service.

---

## Commands

### Everyone

| Command | What it does |
|---|---|
| `/play <song>` | Play a downloaded song **right now** |
| `/playnext <song>` | Put it at the **top** of the queue |
| `/queue add <song>` | Append to the queue |
| `/queue list` · `/queue remove <n>` · `/queue clear` | Manage the queue |
| `/skip` · `/nowplaying` · `/search <query>` · `/shuffle` | Transport |
| `/summon` | **Pull the bot into your voice channel** |
| `/rejoin` · `/status` · `/volume` | Connection and state |
| `/active show` | Which playlists are in rotation |
| `/playlist list` · `/playlist view <playlist>` · `/stats` | Browse the library |

The `song` field autocompletes and **only ever offers downloaded tracks** — a pasted
video id is scoped identically, so there's no way to request something that isn't on disk.

### DJ — Manage Server, or a role in `DJ_ROLE_IDS`

| Command | What it does |
|---|---|
| `/volume <percent>` | Set volume 0–200 (persists) |
| `/leave` | Disconnect; it auto-rejoins shortly |

### Bot owner only

| Command | What it does |
|---|---|
| `/playlist add <url>` | Track a playlist |
| `/playlist remove <playlist> [delete_files]` | Stop tracking it |
| `/playlist toggle <playlist> <enabled>` | Keep the files, pause syncing |
| `/active set <playlist>` | **Play from one playlist only** |
| `/active add` · `/active remove` | Multi-playlist rotation |
| `/active all` | Follow every enabled playlist |
| `/sync [retry_failed]` | Re-read playlists and download |
| `/prune` | Delete mp3s no longer in any playlist |

---

## How it works

**Rotation.** The library can hold many playlists; only those the owner marks active are
in the shuffle pool. `/active all` follows every enabled playlist and picks up new ones
automatically. Removing or disabling a playlist drops it from rotation with no dangling
reference. With `RESTRICT_REQUESTS_TO_ACTIVE=true` (default), users can only request from
what's in rotation; set it false to let them reach anything downloaded.

**Shuffle.** Tracks are drawn from a shuffled bag — every song plays once before any
repeats, then it reshuffles with the last 25 pushed toward the back. Requests jump ahead
of the bag.

**Empty channel.** When the last human leaves, playback pauses but the bot **stays
connected**. After `IDLE_STOP_AFTER` seconds (default 5 min) it tears the ffmpeg process
down entirely so idling is free — still without leaving. Position is remembered and
resumed with `ffmpeg -ss` from ~2 seconds before the cut.

**Connection fallback.** On startup and every `WATCHDOG_INTERVAL` seconds it ensures it's
connected, trying: the `/summon` channel → `VOICE_CHANNEL_ID` →
`FALLBACK_VOICE_CHANNEL_IDS` → any channel with Connect+Speak that isn't full, busiest
first. If all fail it warns once in `TEXT_CHANNEL_ID`, retries every
`RECONNECT_INTERVAL`, and anyone can `/summon` it. Being dragged between channels by a
moderator is handled — it adopts the new channel.

**Sync.** At startup and every `SYNC_INTERVAL`: re-read each enabled playlist, reconcile
the local index, then download whatever's missing, `DOWNLOAD_CONCURRENCY` at a time.
Private/deleted entries are recorded as unavailable — they show in
`/playlist view … problems` and are never retried. Failed downloads retry across syncs 3
times, then wait for `/sync retry_failed:true`. Playback is never blocked by downloading,
and new tracks fold into the rotation as they land.

---

## Configuration

Annotated in full in `.env.example`; `.env.example.filled` shows what each value looks
like when filled in.

| Variable | Default | Notes |
|---|---|---|
| `OWNER_IDS` | — | Extra owners; the app owner always counts |
| `DJ_ROLE_IDS` | — | Manage Server also counts |
| `VOICE_CHANNEL_ID` | — | The 24/7 home channel |
| `FALLBACK_VOICE_CHANNEL_IDS` | — | Tried in order before auto-discovery |
| `TEXT_CHANNEL_ID` | — | Where connection warnings go |
| `RESTRICT_REQUESTS_TO_ACTIVE` | `true` | Limit requests to playlists in rotation |
| `PLAYLISTS` | — | Seeds the library on first boot |
| `COOKIES_FILE` | `/data/cookies.txt` | Ignored if the file doesn't exist |
| `AUDIO_QUALITY` | `192` | mp3 kbps |
| `DOWNLOAD_CONCURRENCY` | `2` | Higher gets you throttled |
| `SYNC_INTERVAL` | `3600` | `0` disables periodic syncing |
| `IDLE_PAUSE` / `IDLE_STOP_AFTER` | `true` / `300` | Empty-channel behaviour |

`/data` holds everything stateful: `audio/` (mp3s), `library.db`, `cache/`,
`cookies.txt`. Back that up, or delete `audio/` + `library.db` to force a clean
re-download.

---

## Running without Docker

Needs Python 3.11+, `ffmpeg`, and `libopus`:

```bash
sudo apt install ffmpeg libopus0 python3-venv
```

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Set `DATA_DIR=./data` in `.env` (the Docker default `/data` isn't writable), then:

```bash
.venv/bin/python -m bot
```

Run it as your normal user, not root — otherwise `data/` ends up root-owned. Bare-metal
runs can skip the cookie file with `COOKIES_FROM_BROWSER=firefox`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403 Missing Access` at startup | Invited without the `applications.commands` scope. Re-invite with both scopes. |
| Slash commands missing | `GUILD_IDS` unset → slow global sync, or the 403 above. |
| Joins but silence | `libopus` missing, or no **Speak** permission. |
| `/status` shows 0 downloaded | Sync still running; check `/playlist list` for a per-playlist error. |
| Playlist reads fine, downloads all fail | Usually an outdated `yt-dlp`: `docker compose build --no-cache`, or `pip install -U yt-dlp`. |
| Private playlist won't read | Needs `data/cookies.txt`. |
| Bot sits in an empty channel doing nothing | Working as intended — it holds the channel and resumes when you join. |

`LOG_LEVEL=DEBUG` for verbose yt-dlp and voice logging.
