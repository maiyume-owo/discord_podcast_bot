# ytvc-bot

A Discord bot that keeps YouTube playlists downloaded as local mp3s and broadcasts them
into voice channels 24/7 on shuffle — **one station, every server in sync**.

It behaves like a radio, not a jukebox: there is a single shared queue and a single
playing track, so everyone tuned in hears the same song at the same moment. A skip in one
server skips for everyone; a server joining mid-track seeks to the current position.

- Tracks any number of playlists; the **bot owner** picks which are in rotation
- The bot's Discord status shows the song currently on air
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

Your `.env` can keep relative paths for bare-metal runs — compose overrides `DATA_DIR`
and friends to `/data` so the mounted volume is always what's used.

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
   **Speak**, **View Channel**, **Send Messages**, **Embed Links**. Once it's running,
   `/invite` builds this link for you.
4. Put your own user ID in `OWNER_IDS`. The application owner (or team members) counts
   automatically, so `OWNER_IDS` is only for adding *more* owners.

With `GUILD_IDS` set, slash commands appear immediately; otherwise a global sync can take
up to an hour.

---

## Cookies

Needed when YouTube says **"Sign in to confirm you're not a bot"** (it throttles
datacenter and high-volume IPs), and for anything not publicly viewable: private/unlisted
playlists, Watch Later (`WL`), Liked videos (`LL`), age-restricted videos.

Run **`/cookies guide`** in Discord for the walkthrough. The short version:

1. Install a **"Get cookies.txt LOCALLY"** browser extension.
2. Open a **private/incognito** window → log in to YouTube → in that same tab visit
   `youtube.com/robots.txt` → export cookies → **close the window**.
3. Save the file to `data/cookies.txt`, or use `/cookies upload`.
4. `/cookies test`, then `/sync`.

Step 2's order matters: YouTube rotates cookies on any open tab, which silently
invalidates a normal export. Closing the private window stops the session being rotated
out from under the bot. (For the same reason yt-dlp's `--cookies-from-browser` is
unreliable for YouTube, so this bot doesn't offer it.)

If you'd rather not install an extension, `/cookies paste` accepts the `cookie:` **request
header** copied from `F12` → Network → a `www.youtube.com` request. Don't use
`document.cookie` from the Console — the login cookies are HttpOnly and it can't read
them.

| Command | |
|---|---|
| `/cookies guide` | Step-by-step instructions |
| `/cookies status` | Installed? how old? logged in? |
| `/cookies test` | Ask YouTube for a video and see if it lets us through |
| `/cookies upload <file>` | Attach a `cookies.txt` |
| `/cookies clear` | Delete the stored jar |

> **Use a throwaway Google account.** Bulk downloading risks the account being
> rate-limited or banned. The file is a live login session — it's stored `0600`, kept out
> of git, and `/cookies` replies are ephemeral. Prefer copying it onto the host directly
> over uploading it through Discord.

---

## Docker

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

Everything persists in `./data` on the host (mp3s, the SQLite library, the yt-dlp cache,
`cookies.txt`), so rebuilds are free. The image bundles ffmpeg, libopus and deno.

The container runs as uid **1000**; if your host `data/` is owned by someone else the bot
can't write to it — `sudo chown -R 1000:1000 data` fixes that.

| | |
|---|---|
| Update to latest code | `git pull && docker compose up -d --build` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Shell inside | `docker compose exec music-bot bash` |
| Install cookies | copy to `./data/cookies.txt` on the **host** — no rebuild needed |

`.dockerignore` keeps `data/` and `.venv/` out of the build context, so builds stay fast
even with a 1.5 GB library.

---

## Commands

### Everyone

| Command | What it does |
|---|---|
| `/help` | What the bot does and every command you can run |
| `/invite` | Generate an invite link with the right scopes and permissions |
| `/play <song>` | Play a downloaded song **right now** |
| `/playnext <song>` | Put it at the **top** of the queue |
| `/queue add <song>` | Append to the queue |
| `/queue list` · `/queue remove <n>` · `/queue clear` | Manage the queue |
| `/skip` · `/nowplaying` · `/search <query>` · `/shuffle` | Transport (global — affects every server) |
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
| `/cookies guide\|status\|test\|upload\|paste\|clear` | YouTube authentication |
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

**One broadcast.** A single *station* owns the clock, the queue and the shuffle bag; each
guild is a *receiver* that plays whatever the station is airing. The station advances on
the track's duration (or a skip), so servers stay aligned without talking to each other.
A receiver that joins, reconnects, or drifts re-syncs by seeking to the station's current
offset with `ffmpeg -ss`. The bot's presence is updated to the song title on every change.

**Shuffle.** Tracks are drawn from a shuffled bag — every song plays once before any
repeats, then it reshuffles with the last 25 pushed toward the back. Requests jump ahead
of the bag, and the queue is global: a request in any server is heard in all of them.

**Empty channel.** When the last human leaves a server's channel, that receiver goes
quiet after `IDLE_STOP_AFTER` seconds (default 5 min) — the ffmpeg process is torn down
but the bot **stays connected**. Other servers keep hearing the broadcast. When someone
returns, it re-joins the stream already in progress. If *every* server empties, the
station holds at the end of the current track rather than burning through the library
overnight.

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

Needs Python 3.11+, `ffmpeg`, `libopus`, and a JavaScript runtime:

```bash
sudo apt install ffmpeg libopus0 python3-venv
```

```bash
curl -fsSL https://deno.land/install.sh | sh
```

Deno solves YouTube's "n" signature challenge. Without it YouTube returns no audio
formats and every download fails — it must be on `PATH`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Set `DATA_DIR=./data` in `.env` (the Docker default `/data` isn't writable), then:

```bash
.venv/bin/python -m bot
```

Run it as your normal user, not root — otherwise `data/` (and any cookie file you drop
there) ends up root-owned, and a non-root bot silently can't read it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `403 Missing Access` at startup | Invited without the `applications.commands` scope. Re-invite with both scopes. |
| Slash commands missing | `GUILD_IDS` unset → slow global sync, or the 403 above. |
| Joins but silence | `libopus` missing, or no **Speak** permission. |
| `/status` shows 0 downloaded | Sync still running; check `/playlist list` for a per-playlist error. |
| `Requested format is not available` on every video | YouTube's "n" challenge needs a JS runtime. Install **deno** and `pip install yt-dlp-ejs` (the Docker image includes both). Startup logs which runtime it found. |
| Playlist reads fine, downloads all fail | Usually an outdated `yt-dlp`: `docker compose build --no-cache`, or `pip install -U yt-dlp`. |
| `Sign in to confirm you're not a bot` | YouTube wants cookies — run `/cookies guide`. Failed tracks auto-retry once cookies land. |
| Private playlist won't read | Needs `data/cookies.txt` — see `/cookies guide`. |
| Bot sits in an empty channel doing nothing | Working as intended — it holds the channel and resumes when you join. |

`LOG_LEVEL=DEBUG` for verbose yt-dlp and voice logging.
