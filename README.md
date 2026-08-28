# ytvc-bot

A Discord bot that keeps YouTube playlists downloaded as local mp3s and plays them into
voice channels 24/7 on shuffle — **one shared library, a separate station per server**.

Every server it's in runs its own station: its own queue, its own current track, its own
skips and volume. Nothing one server does is heard in another. What they share is the
library underneath — one set of playlists, one copy of the mp3s on disk — so every server
can play everything that has been downloaded.

- Tracks any number of playlists; **each server** picks which of them it plays
- The bot's Discord status shows what the busiest server is playing
- Downloads everything with `yt-dlp` and converts to mp3 with `ffmpeg`
- Streams the **local files** — no per-play YouTube requests, no buffering
- Shuffles forever, with a user request queue on top
- **Pauses when the voice channel empties, but never leaves it**, and resumes from the
  exact position when someone comes back
- Joins **only** the configured 24/7 channels on its own; anywhere else needs `/join`
- Anyone can queue — but only songs already downloaded
- One `docker compose up -d` to deploy

---

## Quick start

```bash
cp .env.example .env
```

Fill in `DISCORD_TOKEN`, `VOICE_CHANNEL_IDS`, `OWNER_IDS`.

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

Slash commands are registered **per server, as the bot joins it**, so they appear within
seconds — including in a server invited later. They're re-registered only when the command
list actually changes, since Discord caps command writes per server per day. `GUILD_IDS`
is optional and only pre-registers servers the bot hasn't joined yet.

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

The container runs as uid **1000**. If your host `data/` is owned by someone else (very
easy if you ran anything with `sudo`), SQLite fails with *"attempt to write a readonly
database"* and the container restart-loops. The bot detects this at startup and prints the
exact `chown` to run:

```bash
docker compose down && sudo chown -R 1000:1000 data && docker compose up -d
```

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
| `/skip` · `/nowplaying` · `/shuffle` | Transport — this server only |
| `/search <query>` | Search the shared library |
| `/join` (alias `/summon`) | **Pull the bot into your voice channel** |
| `/rejoin` · `/status` · `/volume` | Connection and state |
| `/active show` | Which playlists this server plays from |
| `/playlist list` · `/playlist view <playlist>` · `/stats` | Browse the library |

The `song` field autocompletes and **only ever offers downloaded tracks** — a pasted
video id is scoped identically, so there's no way to request something that isn't on disk.

### DJ — Manage Server, or a role in `DJ_ROLE_IDS`

| Command | What it does |
|---|---|
| `/volume <percent>` | Set this server's volume 0–200 (persists) |
| `/leave` | Disconnect for a minute; then it rejoins its 24/7 channel |
| `/active set <playlist>` | **Play from one playlist only, in this server** |
| `/active add` · `/active remove` | Multi-playlist rotation for this server |
| `/active all` | Follow every enabled playlist |

### Bot owner only

| Command | What it does |
|---|---|
| `/cookies guide\|status\|test\|upload\|paste\|clear` | YouTube authentication |
| `/playlist add <url>` | Track a playlist |
| `/playlist remove <playlist> [delete_files]` | Stop tracking it |
| `/playlist toggle <playlist> <enabled>` | Keep the files, pause syncing |
| `/sync [retry_failed]` | Re-read playlists and download |
| `/prune` | Delete mp3s no longer in any playlist |

---

## How it works

**Rotation.** The library can hold many playlists, and **each server picks which of them
it plays** — that selection is per server, set by its DJs, and stored under its guild id.
`/active all` follows every enabled playlist and picks up new ones automatically.
Removing or disabling a playlist (owner-level, since it's the shared library) drops it
from every server's rotation with no dangling reference. With
`RESTRICT_REQUESTS_TO_ACTIVE=true` (default), users can only request from what's in
*their* server's rotation; set it false to let them reach anything downloaded.

A server that has never chosen follows every enabled playlist. On upgrade from a version
with one global rotation, that old selection is what servers start from.

**A station per server.** Each guild gets a *station* — the clock, queue, shuffle bag and
volume — paired with a *receiver* that owns the voice connection. The station advances on
the track's duration (or a skip) and tells only its own receiver what to play, so servers
never interfere with each other. A listener arriving mid-track hears it from the station's
current offset (`ffmpeg -ss`), and the same seek repairs a reconnect or a drift.

What is shared is everything below playback: the downloaded files, which playlists are
tracked and synced, and the disk they live on. Which of those playlists a server actually
plays is its own choice, so `/search` and `/play` can offer different songs in different
servers.

**Discord status.** A bot has only one presence, so it shows the track from the server
with the most listeners, plus `(+N more server(s))` when others are playing too. It
clears when nothing is playing anywhere.

**Shuffle.** Each station draws from its own shuffled bag — every song plays once before
any repeats, then it reshuffles with the last 25 pushed toward the back. Requests jump
ahead of the bag. Two servers running at once are shuffling the same library
independently, so they will normally be on different songs.

**Empty channel.** When the last human leaves a server's channel, that station goes quiet
after `IDLE_STOP_AFTER` seconds (default 5 min) — the ffmpeg process is torn down but the
bot **stays connected**, and the station holds its place instead of burning through the
library to an empty room. Other servers are unaffected. When someone returns, playback
resumes with the next track.

**Where it joins.** By itself, the bot only ever enters a channel listed in
`VOICE_CHANNEL_IDS` — the 24/7 rooms. Several can be configured, in different servers;
since it can hold only one channel per server, it picks the 24/7 room with listeners in
it, config order breaking ties, and moves between them as people come and go. **If a
server has none of them, it stays out of voice entirely** rather than picking a room
itself.

Every other channel needs a human: `/join` (or `/summon`) from inside the channel, or a
moderator dragging the bot in — both are adopted the same way. That channel is held
until it has been empty for `IDLE_STOP_AFTER`, then the bot returns to its 24/7 room, or
disconnects if there isn't one. `/rejoin` sends it back to 24/7 duty immediately.

**Staying there.** It joins its 24/7 channel as soon as it starts, and goes straight back
if it's disconnected or kicked out of voice — it doesn't wait for the next watchdog tick,
and it returns to the 24/7 room rather than to a guest channel it was kicked from. The
one exception is `/leave`, which keeps it out for a minute so the command actually means
something; `/join` or `/rejoin` cancels the wait. Every `WATCHDOG_INTERVAL` seconds it
re-checks the connection as a backstop. If a 24/7 channel exists but can't be joined it
warns once in `TEXT_CHANNEL_ID` and retries every `RECONNECT_INTERVAL`.

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
| `OWNER_IDS` | — | Extra owners; the app owner always counts. Owners manage the shared library |
| `DJ_ROLE_IDS` | — | Manage Server also counts. DJs set their own server's rotation and volume |
| `VOICE_CHANNEL_IDS` | — | The 24/7 channels — the only ones joined automatically |
| `TEXT_CHANNEL_ID` | — | Where connection warnings go |
| `RESTRICT_REQUESTS_TO_ACTIVE` | `true` | Limit requests to the server's own rotation |
| `PLAYLISTS` | — | Seeds the library on first boot |
| `COOKIES_FILE` | `/data/cookies.txt` | Ignored if the file doesn't exist |
| `AUDIO_QUALITY` | `192` | mp3 kbps |
| `DOWNLOAD_CONCURRENCY` | `2` | Higher gets you throttled |
| `SYNC_INTERVAL` | `3600` | `0` disables periodic syncing |
| `IDLE_PAUSE` / `IDLE_STOP_AFTER` | `true` / `300` | Empty-channel behaviour |
| `VOLUME` | `0.5` | Starting level; `/volume` then persists per server |

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
| "This application has no commands" | The 403 above, or the bot hasn't finished starting. Kicking and re-inviting also re-registers. |
| Joins but silence | `libopus` missing, or no **Speak** permission. |
| `/status` shows 0 downloaded | Sync still running; check `/playlist list` for a per-playlist error. |
| `WebSocket closed with 4006` / `Already connected to a voice channel` | A stale voice session. The bot force-drops the client, clears the session via the gateway, and reconnects fresh. **If it persists, check nothing else is running the same token** — two instances (an old container, or a bare-metal run) invalidate each other's voice session and produce 4006 forever. `docker ps -a` and `pgrep -af "python -m bot"`. |
| `attempt to write a readonly database` | `data/` isn't writable by the bot's user. In Docker: `sudo chown -R 1000:1000 data`. Startup logs the exact command. |
| `Requested format is not available` on every video | YouTube's "n" challenge needs a JS runtime. Install **deno** and `pip install yt-dlp-ejs` (the Docker image includes both). Startup logs which runtime it found. |
| Playlist reads fine, downloads all fail | Usually an outdated `yt-dlp`: `docker compose build --no-cache`, or `pip install -U yt-dlp`. |
| `Sign in to confirm you're not a bot` | YouTube wants cookies — run `/cookies guide`. Failed tracks auto-retry once cookies land. |
| Private playlist won't read | Needs `data/cookies.txt` — see `/cookies guide`. |
| Bot sits in an empty channel doing nothing | Working as intended — it holds the channel and resumes when you join. |

`LOG_LEVEL=DEBUG` for verbose yt-dlp and voice logging.
