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
uv run miniteams chats            # 20 most recent private chats (id + last activity + names)
uv run miniteams chats --since 2026-07-01 --until 2026-07-10   # activity window; --all, --jsonl
uv run miniteams send "hello"     # send to your Notes (write-safe default)
uv run miniteams send --thread 19:xxx@thread.v2 "hi"   # send to a conversation
uv run miniteams send --file msg.txt              # body from a file (escaped)
uv run miniteams send --file test.html --html     # raw RichText/Html (formatted)
uv run miniteams update <msg-id-or-deep-link> "fixed text"   # edit a sent message
uv run miniteams archive          # resumable local archive of private chats + meetings under ./data
uv run miniteams archive --thread 19:xxx@thread.v2     # archive one conversation
uv run miniteams archive --no-media                    # messages only, skip attachments
uv run miniteams archive --verify-media                # re-check every asset on disk, fetch missing
                                                       # (recovers old transcripts from SharePoint too)
uv run miniteams archive --videos --verify-media       # also grab meeting recordings + shared videos
                                                       # (large; --verify-media sweeps existing chats)
uv run miniteams archive --loop 600                    # re-run forever, sleeping 600s between runs
                                                       # (bare --loop = 300s); stops on auth expiry
uv run miniteams web              # local live page: conversations newest-first (see below)
uv run miniteams web --limit 0 --bind 127.0.0.5:8765   # load every chat (with an archive that is
                                                       # every archived chat: a heavier page)
uv run miniteams web --data-dir /srv/teams-archive     # seed rows from an archive kept elsewhere
uv run miniteams dump             # dump your Notes' full history (oldest → newest)
uv run miniteams dump --thread 19:xxx@thread.v2        # dump a conversation
uv run miniteams dump --jsonl > notes.jsonl            # full-detail JSON per line
```

`dump --jsonl` emits one full-detail record per message (raw resource + resolved sender/thread
+ attachment refs + reactions), every message type, no media download — pipe-friendly.

`web` serves a single page listing private, group and meeting chats newest-message-first
(label, last author, snippet), updated live from the Trouter stream: new messages move a row
up, edits/deletes of the last message rewrite it, `✍ Name` shows who is typing (cleared after
10s if Teams never says so), and with `--reactions` a reaction becomes the row's last event.
Rows are seeded from the archive under `--data-dir` (default `./data`, read-only): the name, snippet
and read state of a chat `archive` already knows cost no API call, which is what makes a deep
`--limit` bearable — it defaults to 400 chats with an archive, 250 without. The page is served as
soon as the seed is read and fills in from the live listing afterwards; a small spinner at the
bottom right means that walk is still running, and the rows may reshuffle when it lands. Mentions
are scanned at startup on the newest rows only (50); older chats surface theirs when the next
message arrives. It binds a random `127.0.0.X:PORT` drawn once and kept in `~/.config/miniteams/web.json` so the
URL stays bookmarkable; `--bind` overrides it and refuses anything but loopback (no auth on the
page). Clicking a row opens the chat at its last message through `--opener` (default `xdg-open`
on an `msteams://` deep link, i.e. the desktop client; `--open-scheme https` for Teams web,
`--opener none` to let the page follow its plain https link); opening does not mark the row
seen. Ctrl/middle click opens the https link, through `--browser firefox` when set, else in the
page's own browser. `xdg-open` only works when the `msteams` handler's `.desktop` entry passes the
URL along (`Exec=… %u`); otherwise point `--opener` at the client binary, e.g.
`--opener "/path/to/teams-for-linux --no-sandbox"`. Hovering a row shows its **seen** button (✓): the row leaves the Inbox until something
newer lands on that chat (state in `~/.config/miniteams/seen.json`); the **Seen** tab lists those rows dimmed,
where ↺ undoes it. A message that @mentions you gets a blue stripe and an `@ you` tag (`@ all` for an
@everyone), also when the mention was added by editing an older message; it clears once you mark the row seen
or read the chat in Teams after the mention. The `@` tab lists mentioned rows, **Unread** follows Teams' own
read marker. `🔕` mutes a chat (out of Inbox whatever lands on it, listed under **Muted** until `🔔`; mentions still
surface; state in `muted.json`). The ⚙ menu picks the theme (system by default) and hides avatars for a denser list. The
process exits non-zero when the stream dies (auth expired) — re-run it.

`stream` enriches each line with thread name + participants, resolves MRIs → display names
(cached in `~/.cache/miniteams/names.json`), downloads images/files to `~/.cache/miniteams/media/`
(full-res + optimized, shown as `file://`), and renders reactions (`↳ 👍 Alice reacted`), edits
(`✏ …`) and deletes (`🗑 …`). `--typing` adds `✍ is typing / stopped` indicators.
`stream --jsonl` = per-event JSON; `stream --raw` = full firehose NDJSON of every frame (all
endpoints, presence/calls, named events), decoded.

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

Tokens cache under `~/.config/miniteams/` (`0600`); re-runs are silent until the refresh
token expires. Downloaded media (from `stream`) lives under `~/.cache/miniteams/media/`.

> **send writes to real chats as you.** Default target is `48:notes` (your own Notes), but
> `--thread` can post anywhere. The rest of the tool is read-only.

## Config

All via `MINITEAMS_*` env vars / `.env` (see `.env.example`). Defaults are the purple-teams
**work/TFW** constants — override only if auth starts returning 4xx, after re-capturing from a
live browser session.

## Dev

```sh
uv run ruff check . && uv run mypy src && uv run pytest
pre-commit run --all-files
```
