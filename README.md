# miniteams

Personal CLI that authenticates as the signed-in user and dumps incoming Microsoft Teams chat
messages to stdout in real time — over the same outbound **Trouter** channel the official client
uses. No inbound endpoint, no polling. Reading is the core; it can also **send** (default target:
your own Notes) and **dump** a conversation's history.

Inspired by [`EionRobb/purple-teams`](https://github.com/EionRobb/purple-teams) (transport +
framing) and [`Gerenios/AADInternals`](https://github.com/Gerenios/AADInternals) (token tricks).

> Interop, not a sanctioned API. Uses a Microsoft first-party `client_id` and undocumented
> endpoints — expect breakage when Microsoft rotates flows. **Use only against your own account.**

## Status

Working. Auth (browser/device-code), live `stream` (messages, reactions, attachments,
auto-reconnect), `chats` (recent conversations, date-window filter), `dump` (history backfill,
`--jsonl`), and `send` are all implemented.

## Setup

```sh
cp .env.example .env        # then set MINITEAMS_TENANT_ID
uv sync
```

## Usage

```sh
uv run miniteams login            # browser (FOCI seed), falls back to device-code; masked tokens
uv run miniteams --device-code login   # force device-code flow (no browser)
uv run miniteams stream           # live: incoming messages, reactions, attachments
uv run miniteams stream --media images,transcripts   # download only these attachment types
uv run miniteams chats            # 20 most recent private chats (id + last activity + names)
uv run miniteams chats --since 2026-07-01 --until 2026-07-10   # activity window; --all, --jsonl
uv run miniteams send "hello"     # send to your Notes (write-safe default)
uv run miniteams send --thread 19:xxx@thread.v2 "hi"   # send to a conversation
uv run miniteams send --file msg.txt              # body from a file (escaped)
uv run miniteams send --file test.html --html     # raw RichText/Html (formatted)
uv run miniteams send "look" --thread 19:xxx@thread.v2 --image a.png --image b.jpg  # inline images after the text
uv run miniteams update <msg-id-or-deep-link> "fixed text"   # edit a sent message
uv run miniteams react <msg-id-or-deep-link> like    # react (heart, laugh, 1f525_fire…)
uv run miniteams react <msg-id-or-deep-link> like --remove   # take it back
uv run miniteams typing --thread 19:xxx@thread.v2 --for 30   # show "is typing…" there for ~30 s
uv run miniteams emojis           # org custom emojis: creation date, name, creator
uv run miniteams emojis --jsonl --download   # + reaction key, image saved under ~/.cache/miniteams/emojis/
uv run miniteams archive          # resumable local archive under ./data: private chats, meetings,
                                  # and your own `48:` conversations (Notes, feeds)
uv run miniteams archive --thread 19:xxx@thread.v2     # archive one conversation
uv run miniteams archive --no-media                    # messages only, skip attachments
uv run miniteams archive --verify-media                # re-check every asset on disk, fetch missing
                                                       # (recovers old transcripts from SharePoint too)
uv run miniteams archive --videos --verify-media       # also grab meeting recordings + shared videos
                                                       # (large; --verify-media sweeps existing chats)
uv run miniteams archive --media transcripts --verify-media   # only these attachment types (below)
uv run miniteams archive --loop 600                    # re-run forever, sleeping 600s between runs
                                                       # (bare --loop = 300s); stops on auth expiry
uv run miniteams archive --live                        # catch up, then store each message as it
                                                       # arrives, plus a pass every 1800s (--live N);
                                                       # stops on auth expiry (exit 1)
uv run miniteams archive --recheck-since 2026-09-22    # one-shot: re-walk chats active since the
                                                       # date down to it, filling any hole
uv run miniteams web              # local live page: conversations newest-first (see below)
uv run miniteams web --limit 0 --bind 127.0.0.5:8765   # load every chat (with an archive that is
                                                       # every archived chat: a heavier page)
uv run miniteams web --data-dir /srv/teams-archive     # seed rows from an archive kept elsewhere
uv run miniteams mcp              # MCP server over stdio for an agent (spec 008; see below)
uv run miniteams watch --thread 19:xxx@thread.v2 --once   # one JSON line per matching live event
                                                       # (spec 009; docs/watch.md)
uv run miniteams dump             # dump your Notes' full history (oldest → newest)
uv run miniteams dump --thread 19:xxx@thread.v2        # dump a conversation
uv run miniteams dump --jsonl > notes.jsonl            # full-detail JSON per line
```

`mcp` serves your Teams to an MCP client over stdin/stdout (spec 008). Reads come from the
archive: `find_chats` (label, topic, participant, kind), `read_messages` (plain text, date window,
`next_since` cursor), `search_messages` (substring, case and accents ignored, 7 days back by
default, feeds left out) and `read_transcript` (recordings, then turns). Results say where they come
from (`source`) and carry the archiver's heartbeat (`archiver_seen`, `archiver_state`,
`sync_started_at`, written by `archive` into `index.db`), so an agent can tell a stopped archiver
from a quiet one. When the archive cannot be reached (no `index.db`, or no answer in 3 s) the
first three read from the API instead, with `source: "api"`. `send_message` and `update_message`
write to Teams as you; with `send_at` (ISO 8601 with an offset, 5 seconds to 125 days ahead)
`send_message` schedules instead, and `list_scheduled`, `update_scheduled` and `cancel_scheduled`
manage what Teams is holding (`docs/scheduled-send-wire.md`). `--read-only` keeps every writing tool
out of the list. Nothing needs a token until the
API is called; a dead credential makes that call return a device-code URL, and `login` reports the
same flow. The server needs the project's `.env` and `data/`, so register it as
`uv run --directory /path/to/miniteams miniteams mcp` (a gitignored `.mcp.json` at the repo root
does it for Claude Code).

`dump --jsonl` emits one full-detail record per message (raw resource + resolved sender/thread
+ attachment refs + reactions), every message type, no media download — pipe-friendly.

`web` serves a single page listing private, group and meeting chats newest-message-first
(label, last author — `You` for your own — snippet), updated live from the Trouter stream: new messages move a row
up, edits/deletes of the last message rewrite it, `✍ Name` shows who is typing (cleared after
10s if Teams never says so), and with `--reactions` a reaction becomes the row's last event.
Rows are seeded from the archive under `--data-dir` (default `./data`, read-only): the name, snippet
and read state of a chat `archive` already knows cost no API call, which is what makes a deep
`--limit` bearable — it defaults to 400 chats with an archive, 250 without. The page is served as
soon as the seed is read and fills in from the live listing afterwards; a small spinner at the
bottom right means that walk is still running, and the rows may reshuffle when it lands. Mentions
are scanned at startup on the newest rows only (50); older chats surface theirs when the next
message arrives. It binds a random `127.0.0.X:PORT` drawn once and kept in `~/.config/miniteams/web.json` so the
URL stays bookmarkable; `--bind` overrides it and refuses anything but loopback (IPv6 as `[::1]:PORT`). Media files, emoji and the WebSocket
need the auth cookie, see *First launch*. Clicking a row opens the chat at its last message through `--opener` (default `xdg-open`
on an `msteams://` deep link, i.e. the desktop client; `--open-scheme https` for Teams web,
`--opener none` to let the page follow its plain https link); opening does not mark the row
seen. Ctrl/middle click opens the https link, through `--browser firefox` when set, else in the
page's own browser (middle click can open the chat page instead, see the ⚙ setting below). `xdg-open` only works when the `msteams` handler's `.desktop` entry passes the
URL along (`Exec=… %u`); otherwise point `--opener` at the client binary, e.g.
`--opener "/path/to/teams-for-linux --no-sandbox"`. Hovering a preview cut by the row width shows the whole message (up to 1000 characters).
Hovering (or tabbing into) a row shows `✎`, `👁` (mark read in Teams) and `⋯`, a menu with **✓ Mark seen** and
**🔕 Mute** (same rows in the chat page's split list, without `✎`). Seen: the row leaves the Inbox until something
newer lands on that chat (state in `~/.config/miniteams/seen.json`); the **Seen** tab lists those rows dimmed,
where ⋯ → ↺ Unseen undoes it. A message that @mentions you gets a blue stripe and an `@ you` tag (`@ all` for an
@everyone), also when the mention was added by editing an older message; it clears once you mark the row seen
or read the chat in Teams after the mention. The `@` tab lists mentioned rows, **Unread** follows Teams' own
read marker. Mute takes a chat out of Inbox whatever lands on it (listed under **Muted** until ⋯ → 🔔 Unmute; mentions still
surface; state in `muted.json`). `✎` (or `r` on the row selected with the arrow keys, `Tab` from the filter) opens
a reply box under the row: Enter sends it to the chat as you, Shift+Enter adds a line, Escape closes it and keeps
the draft until the page reloads; a failed send keeps the text and shows why. The ⚙ menu picks the theme (system by default), hides avatars for a denser list, and sets
**Middle click** and **Chat navigation** (see *Chat page*). The
Teams session expired (refresh token dead): `web` logs `auth_expired` and waits; run `miniteams login` in another
terminal and it reconnects within seconds (it watches the token cache file).

**First launch**
- Launch with `chromium --app=file://$HOME/.config/miniteams/open.html` (or the `MINITEAMS_LAUNCH_FILE` path). `web` writes that launch file (`0600`) and `web.json` (`0600`, the bind) with a single-use token; the page trades the token at `/auth` for the `mt` cookie (`HttpOnly; SameSite=Strict`, one year, refreshed on each page load) and drops it from the address bar. `web` writes a new token after each use, so the same command always works, and once a browser holds the cookie the plain `http://host:port/` URL works too.
- Snap Chromium: its confinement (the snap `home` interface) denies hidden files and directories under `~`, so it cannot open `~/.config/miniteams/open.html`. Set `MINITEAMS_LAUNCH_FILE=~/snap/chromium/common/miniteams/open.html` in `.env` (the snap's own data directory, kept across snap refreshes) and launch `chromium --app=file://$HOME/snap/chromium/common/miniteams/open.html`. Non-snap browsers (`google-chrome` or Firefox from a `.deb`) read the default path.
- Alternative: `chromium --app="http://$(jq -r '"\(.host):\(.port)/#\(.token)"' ~/.config/miniteams/web.json)"`. It puts the single-use token in argv, where a process reading `/proc` could spend it first; the page then shows `Not signed in`.
- The secret never appears on a command line, in the terminal or in the browser history.
- Pages load without the cookie, but show `Not signed in` and stay empty until launched with a token: data (the socket, media, emoji) needs the cookie.
- Open the page by the bound address: any other `Host` name (e.g. `localhost`) gets 404 (DNS-rebinding guard).
- The secret is persistent, in `~/.config/miniteams/web-secret` (`0600`); delete it to rotate, then relaunch each browser with a token.
- Chat pages opened through `--browser` get the current token in their URL fragment, so a fresh `--browser` profile signs in on the first open.
- Known limit: the cookie is host-scoped, not port-scoped, so another HTTP server on the same `127.0.0.X` address receives it. `web` draws a dedicated `127.0.0.X` and keeps it across restarts to make that unlikely.
- Archive reads have a deadline: a hung mount answers `archive unavailable` (history, search, follow) or HTTP 503 (pages, media) and logs one `archive_unavailable` warning per 30 s. A chat page that missed live frames reloads its latest page. At exit, threads stuck on a dead mount do not hold the process.

### Chat page

A chat's own view, `/chat/<thread>`: history, live updates, composer. Settings are per browser (`localStorage`).

- **Middle click** (widget ⚙): `Teams web` (default) or `Chat here`, which opens the chat page through `--browser` when set,
  else in a new tab. The same `--browser` serves chat pages opened from the widget.
- **`/?nav=chat`**: the board as a chat picker; the chat page's ◀ goes there. Click opens the chat in the same tab,
  middle click in a new one, Ctrl click opens Teams.
- **Chat navigation** (⚙, on the widget and on the chat page): `List` (default) or `Split`. `Split` shows the list on the
  chat page's left instead of ◀: a click switches chats in place (Back returns), each chat keeps its draft and quotes.
  The widget adds `?chatnav=split` to the chat URLs it opens, so a page in another browser follows the setting.
  - Drag the list's edge to resize (or focus it: ←/→, Home/End, Enter or double click resets); width and fold are kept per browser.
  - ☰ folds the list, or opens it over the chat below 700px.
  - Ctrl+K jumps to the list's filter: ↑/↓ pick a row, Enter opens it. Seen and Muted sit behind ⋯ in a narrow list.
- **Composer** (also the widget's quick reply): completes `:emoji` and `@member` (`@everyone` on group chats, not 1:1;
  picked members are notified; a mention needs a non-word character before `@`, and two members with the same name map
  in the order they were picked). Messages are limited to 28 KB.
  - Backspace after a picked full-name mention shortens it to the first name while keeping its recipient.
  - Paste images (PNG, JPEG, GIF, WebP; up to 8, 10 MiB each, 40 MiB in all): thumbnails per chat, click to preview,
    × to drop; Enter sends them after the text, or alone. They upload to Teams only on send and live in the page:
    a reload drops them, the text draft stays. A failed or unanswered send may have gone out: until the page reloads,
    retrying the unchanged draft looks for it in the chat before posting again; an edited draft goes out as a new
    message.
  - Widget: inline formatting.
  - Chat page: bold, italic, strike, code, lists, quotes through a toolbar or Ctrl+B / Ctrl+I, plus a preview of the message as posted.
- **Messages**: hover for actions: reply with a quote, react (Teams' 6 default reactions plus 🙏 and 🙃), edit and delete your own. Only chips of the 10 keys the page's `react` verb accepts (`messages.EMOJI`, `yes`/`no` included) toggle; others show their count.
  Hover any reaction chip to see all the message's reactions, grouped by emoji with their participants.
  Native skin tones (`yes-tone1` → 👍🏻 through `yes-tone5` → 👍🏿) are preserved on supported emoji.
  Cards from bots and apps are rendered; runs of deleted messages are folded.
- **Browsing**: infinite scroll both ways, "↓ latest" when away from the bottom, header search, Open in Teams.
- **Archive**: history and search read the archive under `--data-dir`. Without one the page shows the latest API page only
  and search is off.

`watch` follows the stream and prints one JSON line per event matching its criteria: `--thread`,
`--from me|others|anyone`, `--event message|reaction`, `--after MSG` (also catches up from the
chat's history), `--reaction KEY`, `--once`, `--note`. Built for Claude Code's Monitor tool, which
turns each line into an event in the session; `docs/watch.md` has the call and the service lines
(`gap`, `suppressed`, `stopped`).

`stream` enriches each line with thread name + participants, resolves MRIs → display names
(cached in `~/.cache/miniteams/names.json`), downloads images/files to `~/.cache/miniteams/media/`
(full-res + optimized, shown as `file://`), and renders reactions (`↳ 👍 Alice reacted`), edits
(`✏ …`) and deletes (`🗑 …`). `--typing` adds `✍ is typing / stopped` indicators.
`stream --jsonl` = per-event JSON; `stream --raw` = full firehose NDJSON of every frame (all
endpoints, presence/calls, named events), decoded.

`archive` keeps private chats, meeting chats and your own `48:` conversations: Notes, scheduled
and parked drafts, and the activity feeds (mentions, notifications, saved, …). `--all` adds
channels. A draft you cancel loses its text on Teams' side; the archive keeps the version it saw
before.

`archive` builds a resumable local archive under `./data/`: `index.db` (chat metadata) plus
one folder per conversation with a `messages.db` (every message as raw API JSON, keyed by id)
and a `media/` folder (attachments at original quality). Interrupt anytime — re-running resumes
from the oldest stored message, tops up new ones, and skips media already on disk. Assets the
archive gave up on are remembered per chat: a 403 (deleted object, lost share permission) is
permanent, a 404 backs off (1h → 6h → 12h → 24h → 48h, then every 48h) because it may just be a
transcript still being generated. `--retry-assets` forces both, and refuses to run under `--loop`
— a one-shot override on a timer is not an override. Likewise a chat whose whole history is
403 (revoked meeting access) is skipped on later runs; `--retry-denied` re-attempts it. Every run
ends with an `archive_recap` log line (chats, failures, new messages, media files, avatars,
duration). `data/` is gitignored (personal chat data).

`--media TYPES` (spec 014, on `archive`, `archive --live` and `stream`) picks the attachment
types downloaded, comma-separated; the rest of the run is unchanged:

| Type | Downloads |
|---|---|
| `images` | inline pictures, full and optimized views; picture `URIObject`s |
| `files` | shared documents (SharePoint) and other attachments (card and recording thumbnails) |
| `transcripts` | meeting transcripts, every format served (AMS by content type, SharePoint JSON + VTT) |
| `videos` | meeting recordings and shared video files (large) |

The default is `images,files,transcripts`. `--no-media` is an empty selection and `--videos` the
default plus `videos`; neither combines with `--media`. `--verify-media` and `--assets-only` only
fetch the selected types, and a deselected type is neither fetched nor annotated. Files already on
disk are never deleted, whatever the selection.

`archive --live` (spec 005) replaces the `--loop` timer with the event stream. It subscribes first,
buffers events while a normal pass catches the archive up, then applies them and writes each new
one as it arrives. Edits, reactions and deletes update `messages.raw`; the version they replace is
kept in `message_versions`. Attachments download in the background as each message arrives
(`--no-media`, `--videos` and `--media` apply). Read markers land in `index.db`. A reconnect, a real
`trouter.message_loss`, a failed write or the reconcile timer (`--live SECONDS`, default 1800)
triggers a new catch-up. A live write stays pending until a pass tops the chat up past it, so a
message the stream dropped without saying so is still fetched by the next pass. It uses its own Trouter endpoint,
so it runs beside `web` and `stream`. It cannot be combined with `--loop`, `--thread`,
`--assets-only`, `--retry-assets` or `--recheck-since`.

The top-up stops at the newest message a complete walk reached. `--recheck-since WHEN` lowers
that bound to WHEN for every chat active since then: a one-shot repair for an archive that holds
a hole under a newer message, e.g. rows `--live` wrote before that bound existed. It only adds
missing messages; an edit missed meanwhile stays at the stored version.

Tokens cache under `~/.config/miniteams/` (`0600`); re-runs are silent until the refresh
token expires. Downloaded media (from `stream`) lives under `~/.cache/miniteams/media/`.

> **send writes to real chats as you.** Default target is `48:notes` (your own Notes), but
> `--thread` can post anywhere. `typing` shows your indicator the same way. The rest of the tool is
> read-only.

## Config

All via `MINITEAMS_*` env vars / `.env` (see `.env.example`). `MINITEAMS_ENDPOINT_SUFFIX=-test` gives a
second `web`, `stream` or `archive --live` its own Trouter endpoint ids, so it does not take the
deliveries of the one already running; a second `web` also needs its own `--bind` (sharing the config
dir, it leaves the first one's `web.json` and launch file alone, and prints its own launch URL on
stderr at start and after each use: enter it through that URL, or with a cookie a browser already holds for that host).
`MINITEAMS_LAUNCH_FILE` moves the launch file (default `~/.config/miniteams/open.html`). Defaults are the purple-teams
**work/TFW** constants — override only if auth starts returning 4xx, after re-capturing from a
live browser session. `MINITEAMS_ASYNCGW_HOST` sets the custom emoji host (default `fr-prod.asyncgw.teams.microsoft.com`); set it when your tenant lives in another region.

## Dev

```sh
uv run ruff check . && uv run mypy src && uv run pytest
uv run --with playwright pytest tests/test_browser*.py   # opt-in headless Chrome; skipped without playwright, fails under CI=1
pre-commit run --all-files
```
