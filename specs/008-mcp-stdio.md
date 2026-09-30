# 008 — MCP server over stdio (`miniteams mcp`)

**Status**: shipped
**Requested by**: babs
**Date**: 2026-09-30
**Order**: built after 007

## Problem

An agent that wants Teams content has to follow `docs/agent-archive.md` by hand: open SQLite
files, parse raw JSON, strip HTML, join `recordings.json` to transcript files. Every session
rewrites the same snippets, and posting an answer back means shelling out to `miniteams send`.

## Solution

`miniteams mcp` speaks MCP over stdin/stdout. An MCP client (Claude Code, Claude Desktop) starts
it as a subprocess and gets ten tools: one signs you in, four read the local archive (the Teams API when the archive cannot be reached), five write to Teams as
you, now or at a time Teams holds the message until.

## Scope

- Subcommand `miniteams mcp [--data-dir data] [--read-only]`.
- Protocol layer in stdlib: newline-delimited JSON-RPC 2.0 with `initialize`,
  `notifications/initialized`, `ping`, `tools/list`, `tools/call`. One request at a time.
- Read tools. They read the archive, every database opened `mode=ro`:

| Tool | Arguments | Returns |
|---|---|---|
| `find_chats` | `query?`, `participant?`, `kind?` (`private`/`group`/`meeting`/`channel`/`system`), `since?`, `limit=20` (max 100) | id, label, kind, last activity, participants (name and MRI), `backfill_done`, `history_denied`, `synced_at` |
| `read_messages` | `thread_id`, `since?`, `until?`, `limit=100` (max 500) | oldest first: id, time (UTC), sender, plain text, `edited`, `deleted`, attachment count; `next_since` when cut |
| `search_messages` | `query`, `since` (default 7 days back), `until?`, `thread_id?`, `limit=50` (max 200) | same record plus chat id and label, newest first |
| `read_transcript` | `thread_id`, `message_id?`, `offset=0`, `limit=200` | without `message_id`: the chat's recordings (title, time, duration, has transcript). With it: turns (speaker, start offset, text) |

- API fallback for reads, only when the archive is unavailable: no `index.db` under
  `--data-dir`, an error opening it, or no answer within 3 seconds. Storage that is
  gone can hang a read instead of failing it.
- A stale or partial archive is still the archive. The fallback does not top it up.
- Every read result carries `source`: `archive` or `api`.
- A result from the archive also carries `archiver_seen`, the last heartbeat of the archiver
  (UTC). An archiver that died on an expired credential leaves a readable archive that no
  longer moves, and an old heartbeat is what shows it.
- It carries `archiver_state` too: `syncing` while a pass is running, `following` once
  `archive --live` has caught up and writes events as they come, `idle` after a pass of a
  plain `archive`. After a restart the archiver is alive long before it is caught up, and how
  long the pass takes cannot be known in advance.
- `find_chats` and `read_messages` give, per chat, `synced_at`: the last time a pass went
  through that chat. With `sync_started_at`, the start of the running pass, it tells whether
  the pass has reached the chat yet.
- The heartbeat is written by the archiver into `index.db`: every 30 seconds by
  `archive --live` while its stream is connected, and at the end of every pass, `--live` or
  not. A heartbeat that cannot be written is logged and never stops the archiver.
- What each tool does on the API:

| Tool | On the API | Differences |
|---|---|---|
| `find_chats` | walks the conversation listing, newest activity first | matches label and topic, not participant names. `participant` is checked against the label only, which names the members of a 1:1. Stops after 500 conversations. No `backfill_done`, no `history_denied` |
| `read_messages` | walks the chat history back from `until` | `since` defaults to 7 days back, and the result says so |
| `search_messages` | lists the chats active since `since`, then reads each one down to it | at most 50 chats. A cut result says how many were left out |
| `read_transcript` | not available | `isError`: transcripts need the archive |

- After a failed probe the archive counts as unavailable for 60 seconds, then is probed again.

- Write tools, live API, silent token refresh only:

| Tool | Arguments | Effect |
|---|---|---|
| `send_message` | `thread_id`, `text`, `send_at?` | posts plain text to the chat as you. With `send_at`, Teams holds it until then |
| `update_message` | `thread_id`, `message_id`, `text` | replaces the text of a message you sent |
| `list_scheduled` | `thread_id?` | pending scheduled messages: id, chat, send time, text |
| `cancel_scheduled` | `thread_id`, `message_id` | drops a pending message |
| `update_scheduled` | `thread_id`, `message_id`, `send_at?`, `text?` | moves or rewrites a pending message |

- Scheduling is Teams' own scheduled send. miniteams stores nothing and runs no timer.
- `send_at` is an ISO 8601 datetime with an offset or `Z`.
- Sign-in by device code, for when the cached credential is dead or absent:

| Tool | Arguments | Returns |
|---|---|---|
| `login` | none | `signed_in` with the account name, or `pending` with the login URL, the code and the seconds left |

- A tool that needs a token and finds none starts the same login and returns it in its error:
  the URL with the code pre-filled, the code, and how long they stay valid. Calling the tool
  again after signing in runs it.
- The tool list never changes with the sign-in state. Every tool stays listed, and the one that
  needs a token says so when called.
- The credential is shared with the CLI and the archiver. When it is dead here it is dead for
  them too, so an archiver has most likely stopped and the archive stopped moving. The login
  error and the result of `login` say that an archiver which stopped has to be started again.
- One login at a time. While one is pending, `login` and every such error return the same URL.
- The server polls Microsoft in the background and keeps answering other calls meanwhile.
- The credential lands in the same cache as the CLI (`~/.config/miniteams`, `0600`), so
  `miniteams login` and this tool are interchangeable.
- `--read-only` leaves the five write tools out of `tools/list` and refuses them in `tools/call`.
- Tool annotations: `readOnlyHint` on the four read tools, `destructiveHint` on `update_message`,
  `cancel_scheduled` and
  `update_scheduled`, so a client can ask before a write.
- README section with the client registration command, and a pointer from
  `docs/agent-archive.md`.

## Out of scope

- Mixing sources in one answer, or using the API to fill what the archive lacks.
- Transcripts from the API.
- Writing what the fallback fetched into the archive.
- Events pushed to the client. Spec 009 covers them with a command, outside the server.
- MCP resources, prompts, sampling, and the HTTP transport.
- HTML bodies, mentions, attachments and mark-read on the write side.
- A local queue or an OS timer as a fallback scheduler. If Teams' scheduled send cannot be
  reproduced, phase 6 stops and comes back to you.
- Recurring messages.
- Browser login with a loopback redirect. The server signs in by device code only.
- VTT transcripts. Only `*.transcript.json` is read.
- Media bytes. Tools return file names, never content.
- Full-text index. `search_messages` scans.

## Acceptance criteria

Protocol

- [ ] Given `initialize` with a protocol version the server supports, the response echoes that
      version and declares the `tools` capability. An unsupported version gets the server's newest.
- [ ] `tools/list` returns ten tools, each with a JSON Schema `inputSchema`. With `--read-only`
      it returns five: the four read tools and `login`.
- [ ] A notification (no `id`) produces no output line.
- [ ] An unknown method answers `-32601`. A line that is not JSON answers `-32700` and the
      server keeps reading. An unknown tool or a bad argument answers `-32602`.
- [ ] A tool that fails returns `isError: true` with a one-line reason. The process stays up.
- [ ] stdout carries only JSON-RPC lines. Logs go to stderr.
- [ ] EOF on stdin exits 0.

Read tools

- [ ] Given no `index.db` under `--data-dir`, the directory is not created.
- [ ] `find_chats` with `query` matches label, topic and participant names, case-insensitive,
      newest activity first.
- [ ] `find_chats` with `participant` returns the chats where someone whose name contains it,
      case and accents ignored, or whose MRI equals it, is a member. `participant: "zoe"`
      with `kind: "private"` finds the 1:1 with that person. A group chat whose label is
      its topic is found through its roster.
- [ ] `query` and `participant` together narrow each other.
- [ ] `read_messages` returns plain text (no tags, entities decoded), oldest first, inside
      `[since, until)`. A cut result carries a `next_since` that resumes with no gap and no repeat.
- [ ] `read_messages` on a thread id that is not in the index returns `isError`. It never builds
      a path from an id the index does not know.
- [ ] `search_messages` without `thread_id` skips the `48:` conversations other than
      `48:notes`. They hold copies of messages from other chats (spec 007). With `thread_id`
      it reads the one asked for.
- [ ] `find_chats` with `kind: "system"` returns the `48:` conversations.
- [ ] `search_messages` opens only chats whose last activity is at or after `since`, and matches
      on stripped text, so a word that appears only in markup or a URL attribute does not match.
- [ ] `read_transcript` without `message_id` lists the recordings from `recordings.json`. With
      one, it returns the turns of its `.transcript.json`, and falls back to `speakerId` when
      `speakerDisplayName` is empty.
- [ ] A recording with no transcript on disk returns `isError`, not an empty list.
- [ ] While the archive answers, a read tool call makes no network request and mints no token.
- [ ] A result from the archive carries `archiver_seen`, equal to the heartbeat stored in
      `index.db`. An archive that holds no heartbeat gives no `archiver_seen`, not an error.

Heartbeat

- [ ] `archive --live` with a connected stream writes a heartbeat every 30 seconds, messages
      or not.
- [ ] The state is `syncing` from the start of a pass to its end, then `following` under
      `--live` and `idle` otherwise. A pass that fails or stops on a dead credential does not
      leave `following` behind.
- [ ] `sync_started_at` is the start of the pass that is running, or of the last one.
- [ ] Given a pass in progress, a chat it already went through has a `synced_at` at or after
      `sync_started_at`, and a chat it has not reached yet has an older one.
- [ ] While the stream is down or reconnecting, no heartbeat is written. The first one comes
      back with the connection.
- [ ] Every pass writes one at its end: `archive`, `archive --loop`, and the passes of
      `--live`. A pass that stopped on a dead credential writes none.
- [ ] A heartbeat write that fails (database locked, disk) is logged, the archiver keeps
      running and the next beat is tried.
- [ ] An `index.db` made before the heartbeat existed opens, gets its table, and keeps every
      row it had.

API fallback

- [ ] Given no `index.db`, `find_chats`, `read_messages` and `search_messages` answer from the
      API with `source: "api"`, and `read_transcript` returns `isError` naming the archive path.
- [ ] Given an archive whose probe never returns, the tool answers from the API within 3 seconds
      plus the API time. The server does not hang.
- [ ] Given a failed probe, the next calls within 60 seconds do not probe again. The first call
      after that does, and an archive that is back is used, with `source: "archive"`.
- [ ] Given a readable archive that lacks the chat or the period asked for, the answer comes from
      the archive. No API call is made.
- [ ] A record has the same fields from both sources. A field the API cannot fill is absent,
      not empty.
- [ ] `read_messages` on the API without `since` returns the last 7 days and states the window.
- [ ] `search_messages` on the API over more than 50 active chats reads 50 and reports the rest.
- [ ] Given an unavailable archive and a dead refresh token, a read returns `isError` that names
      both causes and carries the login URL.

Write tools

- [ ] `send_message` calls `send.send_message` once with the thread id and the exact text.
- [ ] `update_message` calls `send.edit_message` once with thread id, message id and text.
- [ ] Empty or whitespace-only text, a non-string text, or a body over 28 KB once escaped is
      refused with no API call.
- [ ] A thread id that is neither in `index.db` nor `48:notes` is refused with no API call.
      With the archive unavailable, the id is checked by a thread lookup on the API, and an
      unknown one is refused with no message sent.
- [ ] With a dead refresh token, a write returns `isError` that carries the login URL and code,
      and sends nothing. Nothing is read from stdin or written to stdout outside the protocol.

Sign-in

- [ ] `tools/list` returns the same tools before a login, while one is pending, and after the
      credential died. The server never writes `notifications/tools/list_changed`.
- [ ] A failure on Microsoft's side starts no login and changes nothing else.
- [ ] `login` with a live credential returns `signed_in` and starts no flow.
- [ ] `login` with a dead or absent credential returns `pending` with a URL on a Microsoft login
      host, the user code and the seconds left.
- [ ] A second `login`, or a tool that needs a token, while the flow is pending returns the same
      URL and code. Microsoft is asked for one flow, not two.
- [ ] While a login is pending, a read from a reachable archive answers at once.
- [ ] Once the user has signed in, the tool that was refused succeeds on the next call with no
      restart of the server, and the token cache file is `0600`.
- [ ] A flow that expires unused is dropped. The next call starts a new one with a new code.
- [ ] A sign-in Microsoft refuses returns `isError` with Microsoft's error code and reason.
- [ ] A throttled or unreachable Microsoft returns `isError` saying to retry. It starts no flow,
      since the credential may still be good.
- [ ] No token, refresh token or device code appears in a tool result or a log line. The user
      code does, it is what the user types.
- [ ] An HTTP error, an `errorCode` envelope or a transport error returns `isError` with the
      status or reason. No token appears in the result or the logs.
- [ ] With `--read-only`, calling any of the five write tools answers `-32602` and makes no
      API call.

Scheduling

- [ ] `send_message` with a `send_at` in the future returns the scheduled message id and delivers
      nothing now. The message shows as scheduled in the Teams client.
- [ ] A `send_at` that does not parse or has no offset is refused with no API call.
- [ ] A `send_at` more than 125 days ahead is refused with no API call, and the reason names
      the 125 days. One second under 125 days goes to Teams.
- [ ] A `send_at` less than 5 seconds ahead, the past included, is refused with no API call,
      and the reason names the 5 seconds. Six seconds ahead goes to Teams.
- [ ] A `send_at` between 120 and 125 days ahead is scheduled like any other. The result
      carries no warning.
- [ ] A `send_at` inside both bounds that Teams still refuses returns `isError` with Teams' own
      reason, and nothing is scheduled. A call that was 5 seconds ahead when checked can reach
      Teams too late.
- [ ] The two bounds and the pass-through apply to `update_scheduled` as well.
- [ ] `list_scheduled` returns that message with chat id, label, send time (UTC) and plain text.
      With no pending message it returns an empty list, not an error.
- [ ] `list_scheduled` leaves out drafts already sent (`sendAt` in the past) and cancelled ones
      (empty body). Teams keeps both in its listing.
- [ ] A scheduled message created or edited here shows as scheduled in the Teams client, with
      its send time, not as a message already sent.
- [ ] A new draft never takes a client id that a draft in the listing already uses, cancelled
      ones included.
- [ ] `cancel_scheduled` removes it from `list_scheduled` and nothing is delivered at `send_at`.
- [ ] `update_scheduled` with `send_at` moves the time, with `text` replaces the body, with both
      does both in one call. With neither it is refused with no API call.
- [ ] `cancel_scheduled` or `update_scheduled` on an id that is not pending returns `isError`
      with no write call. Teams answers 200 to a second cancel, so the state is read first.
- [ ] `send_message` with `send_at` and `48:notes` schedules into the Notes thread behind the
      alias.
- [ ] The text limits and the thread check of `send_message` apply to scheduled messages too.

## Phases

### Phase 1 — heartbeat, protocol, `find_chats`, `read_messages`
- Work: the heartbeat in the archiver. `src/miniteams/mcp.py` with the read loop, the tool
  table and the two tools. `mcp` subcommand in `cli.py`. The `meta` table in
  `docs/agent-archive.md`.
- **Data model impact**: new table `meta(key, value)` in `index.db`, created on open, three
  rows: `archiver_seen`, `archiver_state`, `sync_started_at`. No existing table changes.
- **DoD**: tests green for the Heartbeat criteria. Live, on a scratch data dir with its own
  config dir: `archive --live` left idle for two minutes shows a heartbeat less than 30 seconds
  old. `pytest tests/test_mcp.py` green, driving the loop with text streams over a fixture
  archive. `printf` of `initialize` + `tools/list` piped into `uv run miniteams mcp` prints two
  valid JSON lines. `pre-commit run --all-files` rc=0.

### Phase 2 — `search_messages`, `read_transcript`
- Work: the two tools, chat pruning by last activity, transcript paging.
- **Data model impact**: none
- **DoD**: tests green for each criterion above. On the real archive, `search_messages` over
  the last 7 days answers in under 10 s, measured and written in the PR.

### Phase 3 — API fallback for reads
- Work: the availability probe with its deadline, the three tools on `chats.fetch_conversations`
  and `dump.iter_history_pages`, `source` in every result, token via `RefreshingToken`, the
  `login` tool and the login carried by token errors.
- **Data model impact**: none
- **DoD**: tests green for the API fallback and Sign-in criteria, HTTP and MSAL mocked, the hang
  simulated by a probe that sleeps. Live, with an empty config dir: `login` gives a URL, signing
  in makes the next `find_chats` answer. Live with `--data-dir /nonexistent`: `find_chats` then `read_messages` on
  `48:notes` return `source: "api"`.

### Phase 4 — write tools, `--read-only`, docs
- Work: `send_message`, `update_message`, the flag, README and
  `docs/agent-archive.md`.
- **Data model impact**: none
- **DoD**: tests green with `send.send_message` and `send.edit_message` mocked. Registered in
  Claude Code, a `send_message` to `48:notes` shows up in Teams, then `update_message` rewrites
  it. A `find_chats` call from the same client returns rows.

### Phase 5 — Teams' scheduled send on the wire (spike, no product code)
- Done (2026-09-30): list, create, read back, edit, move and cancel, run on Notes through the
  proxy host with an ic3 bearer token that miniteams mints silently. Recorded in
  `scheduled-send-wire.md`, which moves to `docs/` with this phase.
- The drafts store refuses the alias `48:notes` and accepts the Notes thread behind it.
- Left unchecked: how a draft made here is drawn in a group chat. Channels only if you want them
  as a target. Both wait for a chat the check can use.
- **Data model impact**: none
- **DoD**: `docs/scheduled-send-wire.md` states what was verified and what was not, with the
  1:1 case verified: a draft made here shows in the Teams client and can be deleted from it.

### Phase 6 — scheduling tools
- Work: `send_at` on `send_message`, `list_scheduled`, `cancel_scheduled`, `update_scheduled`,
  built on the calls phase 5 recorded. README.
- **Data model impact**: none
- **DoD**: tests green with the HTTP layer mocked against the recorded bodies. From a registered
  client: a message scheduled a few minutes ahead is listed, moved later, and arrives within a
  minute of the new time. A second one is cancelled and never arrives.

## Data model impact (summary)

One new table in `index.db`, written by the archiver:

```sql
meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)
-- rows: archiver_seen, archiver_state, sync_started_at
```

The MCP server reads `index.db`, `messages.db`, `recordings.json` and transcript files. It
writes nothing under `--data-dir`.

## Open questions

None.

## Decisions

- Reads come from the archive, and from the API when the archive is unavailable
  (babs, 2026-09-30).
- Unavailable means unreachable, not stale or incomplete. Otherwise every question about a
  recent message would go to the API and the archive would stop being the reference.
  Overturnable.
- No transcript on the API. The existing fetch resolves SharePoint links and writes files into
  the archive, which is the thing that is missing. Overturnable.
- The probe runs in a thread with a deadline. A thread stuck on a hung read cannot be
  killed, so the 60 second hold keeps them from piling up.
- `send_message` and `update_message` take any thread id (babs, 2026-09-30).
- Hand-rolled protocol in stdlib, no `mcp` SDK (babs, 2026-09-30).
- All four read tools (babs, 2026-09-30).
- `find_chats` takes a `participant` filter (babs, 2026-09-30). `query` already matched names,
  but it also matches topics, so "paris" finds a room named after the city as well as a person.
  `participant` is membership only, and takes an MRI too.
- Chat content reaching the model behind the client is accepted. An agent reading the SQLite
  files by hand sends the same content to the same place (babs, 2026-09-30).
- Notifications are spec 009 (babs, 2026-09-30).
- Scheduling goes through Teams' own scheduled send, with list, cancel, move and edit
  (babs, 2026-09-30).
- The wire format of scheduled send was unknown: no code in miniteams, nothing published. The
  archive held no trace because scheduled messages live in a pseudo conversation `48:drafts`
  that `archive` does not walk. Hence the spike, placed after the tools that do not depend on it.
- `send_at` without an offset is refused. The archive is UTC, you are Europe/Paris, and an agent
  guessing which one applies sends a message two hours off.
- Two bounds on `send_at` in the code, both the service's own and both as named constants with
  a comment saying where they come from: 5 seconds and 125 days. The comment on the second
  says that the Teams client's date picker stops at 120 (babs, 2026-09-30).
- No warning between 120 and 125 days: it would be noise (babs, 2026-09-30). Such a draft can
  still be deleted from the client; editing it there pulls its date back to day 120.
- The bounds are checked against the clock of this machine. A refusal from Teams is still
  handled, for a clock that is off or a call that took too long.
- `cancel_scheduled` leaves a tombstone in Teams' draft list. No call removes it.
- The ic3 token gets a public method on `TokenSource`, next to `graph_token()`.
- `list_scheduled` reads Teams, not the archive copy of `48:drafts`, which is as old as the
  last pass.
- `update_scheduled` is its own tool. `update_message` edits a delivered message, and mixing both
  behind one name hides which state the message is in.
- Write targets must exist in `index.db`, or be `48:notes`. An agent gets ids from
  `find_chats`, so this only blocks an invented id. Cost: a chat the archive has never seen
  cannot be written to. Overturnable.
- `--read-only` exists although writes are on by default. It is one filter on the tool table,
  and it lets a client config for an untrusted agent drop the write path.
- Write tools send plain text only, escaped by `send.message_html`, same as the widget.
- 28 KB cap reused from spec 006.
- Sign-in by device code from the server, offered by the tool that lacks a token
  (babs, 2026-09-30). This replaces the earlier rule of no login from the server.
- The user code is shown to the agent and the user. It gives nothing without the sign-in, and
  the device code that redeems the token never leaves the server.
- The tools stay listed whatever the sign-in state (babs, 2026-09-30). An earlier draft took
  the write tools out of the list on expiry. Dropped: tools that come and go confuse the agent,
  and the error of the tool already carries the login URL.
- A login starts only on a dead or absent credential. A failure on Microsoft's side does not
  start one.
- Heartbeat of the archiver every 30 seconds, read back as `archiver_seen`
  (babs, 2026-09-30). `last_fetch_at` was tried first and dropped: it moves with passes only,
  so a healthy `archive --live` shows it up to 30 minutes old.
- The heartbeat means the stream is connected, not that the process exists. An archiver stuck
  reconnecting receives nothing, and saying it is alive would be wrong.
- `archiver_seen` is returned as a time. The server adds no verdict and no warning.
- Reads stay on the archive whenever it can be reached, stale or not (babs, 2026-09-30: the
  length of a catch-up after a restart cannot be estimated, so no age threshold can decide
  when to switch to the API). The archiver's state and the per-chat `synced_at` are given
  instead, and the agent judges.
- `synced_at` is the existing `last_fetch_at` of the chat. No new column.
- Tokens are minted on the first call that needs the API, not at startup. A session that only
  reads a reachable archive never touches the token cache, so it cannot rotate the refresh
  token under `web` or `archive --live`.
- `search_messages` is a case-insensitive substring match with a mandatory window. No regex.
  A scan over the chats active in the window is enough for a few thousand chats. SQLite FTS5 if the
  10 s DoD fails.
- Requests are handled one at a time. A slow search blocks the next call. Threads if that bites.
- Tool results are one `text` content block holding JSON.
- The server needs the project's `.env` and `data/`, both relative to the working directory, so
  the documented registration is `uv run --directory <repo> miniteams mcp`.
