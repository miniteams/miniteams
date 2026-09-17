# 002 — Web widget (live conversation list)

**Status**: shipped
**Requested by**: babs
**Date**: 2026-09-15

## Problem

Knowing "who wrote to me lately, and what have I not looked at yet" needs the full Teams client.
`chats` gives the ordered list once; `stream` gives the live firehose. Nothing combines the two
into a glanceable, persistent view with a personal "seen" marker.

## Solution

`miniteams web` serves one local page: private conversations, newest activity first, updated
live from the Trouter stream. Each row has a **seen** button. A seen conversation is soft-hidden
until a new message arrives; a toggle shows hidden rows anyway.

## Scope

- New `web` CLI command, one process: bootstrap list + Trouter subscription + tiny web server.
- Bootstrap: first page(s) of `fetch_conversations`, private 1:1/group **and meeting chats**
  (`is_private or is_meeting`, same default set as `archive`), cap `--limit` (default 50).
- Per row: `id`, `label` (`Directory.label`), `last_activity`, **last message: `sender` +
  `text`** (stripped, truncated), `seen_at`, `typing` (list of names currently typing).
- Live: `TrouterClient` gets an optional `on_event` hook; `NewMessage` / `MessageUpdate` (edit)
  on an in-scope thread updates that row and re-broadcasts. Threads not in the bootstrap list get
  added on first event.
- Typing: `Control/Typing` / `ClearTyping` set/clear the author in the row's `typing` list
  (auto-clear after 10s as a safety net — Teams does not always send ClearTyping).
- Reactions: `MessageUpdate` with `emotions` becomes the row's last event (`👍 Alice reacted`)
  **when `--reactions` is on** (off by default); the row moves up with it. `seen` semantics are
  unchanged: a reaction bumps `last_activity` only when shown.
- Transport: one websocket `/ws`. Server pushes the full row list on connect and on every
  change; client sends `{"seen": "<thread_id>"}`. `GET /` serves the page. Both handled by
  `websockets.serve(process_request=…)` — no new dependency.
- Seen state: `<config_dir>/seen.json` = `{thread_id: seen_at_iso}`. Hidden ⇔
  `seen_at >= last_activity`. New message ⇒ `last_activity` moves ⇒ row reappears. No extra state.
- Page: `src/miniteams/widget.html`, inline CSS/JS, no framework, no build. Sort + hide filter +
  "show hidden" toggle computed client-side from the pushed rows.
- Bind: `127.0.0.X:PORT`, X random in 2–254 and PORT random in the ephemeral range, drawn on
  first run and persisted in `<config_dir>/web.json` so the URL is stable; `--bind ADDR:PORT`
  overrides. URL logged at start.

## Out of scope

- Message history / thread view in the page — each row shows only the **last message and its
  author**; `dump`/`archive` cover history.
- Sending from the page.
- Auth on the page (loopback bind only, personal machine).
- Syncing "seen" with Teams' own read marker (`consumptionhorizon`) — noted as a later option.
- Presence in the widget. (Edits, typing and — opt-in — reactions are in scope, see above.)
- Teams / channels (V1 = private 1:1, group and meeting chats only; no `--all`).

## Acceptance criteria

- [x] `uv run miniteams web` logs a `http://127.0.0.X:PORT/` URL; the page lists the same set
      as `uv run miniteams chats --all --limit 50` minus channels, ordered by last message time
      (`chats` itself prints the server's *version* order, which membership bumps also move),
      each with label + last author + snippet.
- [x] Restarting the process yields the same URL; `--bind` changes it.
- [x] `--bind` refuses a non-loopback address; a websocket opened from another origin gets 403.
- [x] A message received on any in-scope thread moves its row to the top within a second, no
      page reload; a thread absent from the list appears.
- [x] Someone typing shows `✍ Name` on the row while it lasts; it clears on ClearTyping or 10s.
- [x] With `--reactions`, a reaction becomes the row's last event and bumps it; without, it is
      ignored.
- [x] Clicking **seen** hides the row; it stays hidden across page reload and process restart.
- [x] A new message on a hidden thread un-hides it.
- [x] "Show hidden" toggle reveals hidden rows (visually dimmed); un-toggling hides them again.
- [x] `stream` / `chats` behaviour and output unchanged (`uv run pytest` green).

## Phases

### Phase 1 — Snapshot list
- Work: `web.py` — bind persistence (`web.json`, `--bind`), `rows_from_conversations()` (pure:
  conversation dict → row), `websockets.serve` with `process_request` serving `widget.html`
  on `/` and pushing the bootstrap rows on `/ws`; `widget.html` renders rows sorted by
  `last_activity`; `web` subcommand in `cli.py`.
- **Data model impact**: new `<config_dir>/web.json`.
- **DoD**: `uv run pytest tests/test_web.py` green — row mapping (private+meeting filter,
  channels dropped, snippet strip/truncate, sender fallback), bind persistence round-trip,
  `--bind` override; manual: page order matches `chats --all` minus channels.

### Phase 2 — Live updates
- Work: `TrouterClient(on_event=…)` hook called with the decoded `EventMessage` (falls back to
  today's `handle_delivery` when unset); `web.py` folds `NewMessage`/edit/typing (and reactions
  under `--reactions`) into its row table and broadcasts to every open `/ws`; the page applies
  the pushed list and renders the typing marker.
- **Data model impact**: none.
- **DoD**: `uv run pytest tests/test_web.py tests/test_messages.py` green — event folds into
  an existing row (moves to top), creates a missing row, ignores channel threads, typing
  sets/clears/expires, reaction folded only with `--reactions`; stream tests unchanged.
- **Fin de phase**: pause — validation user on a real session before phase 3.

### Phase 3 — Seen / hide / toggle
- Work: `seen.json` load/save; `{"seen": id}` over `/ws` stamps `seen_at = last_activity`;
  rows carry `seen_at`; page hides `seen_at >= last_activity` rows unless "show hidden".
- **Data model impact**: new `<config_dir>/seen.json`.
- **DoD**: `uv run pytest tests/test_web.py -k seen` green — seen persists, newer message
  un-hides, seen on unknown id is a no-op.

## Addenda (shipped after the initial scope, 2026-09-16)

- Unread rows in bold from Teams' own read marker (`consumptionhorizon` at bootstrap,
  `ConversationUpdate` live); "unread only" quick filter; `unseen` undoes a seen marker.
- Row click opens the chat through `--opener` (default `xdg-open` on an `msteams://` deep link to
  the last message, host `teams.cloud.microsoft`); Ctrl/middle click → https link through
  `--browser` when set. Opening does not mark seen. The Linux client reloads its SPA on every
  deep link (no in-page route is consumed) — inherent, not fixable from here.
- Open tabs reload when `widget.html` changes (version in every frame, 2s file poll).
- `web` registers its own Trouter endpoint id (`endpoint_id-web`): sharing `stream`'s made the
  last registrant the only receiver.
- File/card posts render from `properties` (`📎 name`); listing stubs that look deleted are
  resolved with one history call. Topic/roster changes refresh the label; a failed lookup keeps
  the old name; thread lookups retry on 429, and a transient failure is not refetched for 60s
  (new token or rename lifts it). A row still on its bare id retries on its next message.
- The directory's skype token is renewed at 80% of its lifetime while the websocket stays up
  (it outlives the ~1h token); re-registers use the renewed pair.

## Data model impact (summary)

Two small JSON files under `<config_dir>` (`web.json`, `seen.json`). No change to existing
caches, token files or the archive.

## Open questions

- (none)

## Decisions

- Scope = private + group + meeting chats (user: V1), channels later — same default set as
  `archive`.
- Reactions opt-in (`--reactions`): they bump rows and would defeat "seen" for chatty threads;
  typing is always on (transient, no effect on order or seen).

- Loopback bind on a random `127.0.0.X` (user request); persisted so the bookmark survives.
- One websocket for push and for the `seen` action — no REST layer for a single verb.
- `websockets` serves the static page too (`process_request`) rather than adding aiohttp /
  starlette: one dependency already present, one port, one process.
- Seen = timestamp comparison, not a boolean: the "reset on new message" rule falls out of the
  data with no extra bookkeeping.
- `--limit 0` walks every page like `chats` (approved 2026-09-15); pages are iterated anyway,
  the cap is just an early return.
- No JS framework / build step: one HTML file shipped in the package, readable with `cat`.
