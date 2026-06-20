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
auto-reconnect), `dump` (history backfill, `--jsonl`), and `send` are all implemented.
See `HANDOFF.md` for the original milestone plan and protocol notes.

## Setup

```sh
cp .env.example .env        # then set MINITEAMS_TENANT_ID
uv sync
```

## Usage

```sh
uv run miniteams login            # browser auth-code+PKCE, prints masked tokens
uv run miniteams --device-code login   # device-code fallback (often CA-blocked)
uv run miniteams stream           # live: incoming messages, reactions, attachments
uv run miniteams send "hello"     # send to your Notes (write-safe default)
uv run miniteams send --thread 19:xxx@thread.v2 "hi"   # send to a conversation
uv run miniteams dump             # dump your Notes' full history (oldest → newest)
uv run miniteams dump --thread 19:xxx@thread.v2        # dump a conversation
uv run miniteams dump --jsonl > notes.jsonl            # full-detail JSON per line
```

`dump --jsonl` emits one full-detail record per message (raw resource + resolved sender/thread
+ attachment refs + reactions), every message type, no media download — pipe-friendly.

`stream` enriches each line with thread name + participants, downloads images/files to
`~/.cache/miniteams/media/`, and prints reactions (`↳ 👍 Alice reacted`).

Tokens cache under `~/.config/miniteams/` (`0600`); re-runs are silent until the refresh
token expires. Downloaded media lives under `~/.cache/miniteams/media/`.

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
