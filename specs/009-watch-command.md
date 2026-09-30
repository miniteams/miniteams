# 009 — `miniteams watch`: events on declared criteria

**Status**: in progress
**Requested by**: babs
**Date**: 2026-09-30
**Order**: built after 008

## Problem

An agent that waits for something in Teams (an answer to a question, a message in one chat, a
reaction you use as a signal) has to poll, or you have to come back and tell it.

## Solution

`miniteams watch` follows the live stream and prints one line per event that matches the criteria
it was given. Claude Code's Monitor tool runs a command in the background and turns each line of
its output into an event in the session, so an agent arms a watch by starting this command under
Monitor. The same command works in a pipe, outside Claude.

| You want | Command |
|---|---|
| my messages | `miniteams watch --from me` |
| an answer after a given message | `miniteams watch --thread ID --after MSG --from others --once` |
| one special chat | `miniteams watch --thread ID` |
| a reaction from me | `miniteams watch --event reaction --from me [--reaction like]` |

## Scope

- Subcommand `miniteams watch` with these criteria:

| Flag | Values | Default |
|---|---|---|
| `--event` | `message`, `reaction` | `message` |
| `--thread` | a chat id | any chat |
| `--from` | `me`, `others`, `anyone` | `anyone` |
| `--after` | a message id, needs `--thread` | none |
| `--reaction` | an emotion key (`like`, `heart`, ...), needs `--event reaction` | any |
| `--once` | exit 0 after the first match | off |
| `--note` | free text repeated in every line, to say why the watch was armed | none |

- A watch needs `--thread` or `--from me`. Anything wider is refused at startup.
- Criteria combine with AND.
- `message` matches new messages only. Edits, deletes, typing and call events never match.
- A reaction matches when the reaction itself is newer than the start of the watch. The first
  update seen for a message lists every reaction it already had, and those are not news.
- Output: one JSON object per line on stdout, flushed per line. Fields: `event`, `thread`,
  `chat`, `sender`, `message_id`, `time` (UTC), `text`, `reaction`, `note`. `text` is plain text
  cut at 1000 characters. For a reaction it is the text of the message reacted to, when the event
  carries it.
- `--after` also catches up: at startup the command reads the history of the chat newer than
  that message, prints what matches, then follows the stream. Re-arming with the last
  `message_id` received leaves no hole.
- Messages sent through the MCP server of spec 008 never match, so an agent watching
  `--from me` does not wake itself up. The server keeps the ids it sent in a bounded file under
  the cache dir, and `watch` reads it.
- Three service lines, same stream, so that silence never stands for success:

| `event` | When | Then |
|---|---|---|
| `gap` | the stream reconnected or reported a loss | keeps running |
| `suppressed` | more than 10 matches in a minute, with how many were held back | keeps running |
| `stopped` | the credential is dead, with the reason | exits 1 |

- Sign-in is silent only. `watch` never prompts.
- Each `watch` process uses its own Trouter endpoint id, never persisted.
- README section, and in `docs/` the way to arm a watch from Claude Code with Monitor.

## Out of scope

- Claude Code channels, and any push from the MCP server.
- Watch tools in the MCP server. A client that has MCP and no Monitor does not get watches.
- Catch-up for a watch without `--thread`. Events between two arms of such a watch are missed.
- Matching on text (keywords, regex, mentions).
- Presence, calls, meeting starts, read receipts.
- Several criteria sets in one process. One watch, one command.
- Marking anything read.

## Risks

- **Injection.** A watch on a chat brings other people's text into a session that may hold
  `send_message`. The criteria limit who can do it (members of a chat you chose), not what they
  write. The guard is the client's permission prompt on the write tools. A session running with
  permissions bypassed has no guard.
- **Monitor deadline.** A Monitor watch lasts 30 minutes at most, then has to be armed again.
  `--after` closes the hole for a chat, nothing does for a watch across chats.
- **Monitor cut-off.** Claude Code stops a monitor that emits too much. The 10 per minute limit
  is there to stay under it, and the real threshold is not documented.
- **Endpoint ids.** The Trouter registrar keeps one socket per endpoint id. A shared id would
  make two watches steal each other's events.

## Acceptance criteria

Matching (pure function, no network)

- [ ] Each row of the table in Solution matches its event and rejects the three others.
- [ ] `--from me` matches on my MRI, `others` on any other, whatever the display name says.
- [ ] `--after` matches messages newer than that id in that chat only. The message itself and
      older ones do not match.
- [ ] A reaction watch matches when my MRI is added with a reaction time after the start of the
      watch. A removed reaction, someone else's, or one of mine older than the start do not match
      a `--from me` watch.
- [ ] An edit or a delete of a matching message does not match.
- [ ] A message whose client id is in the sent file of the MCP server does not match. A missing
      or unreadable file excludes nothing and does not stop the watch.

Command

- [ ] Neither `--thread` nor `--from me`: exit 2 with the reason on stderr, no socket opened. Same
      for `--after` without `--thread`, and `--reaction` without `--event reaction`.
- [ ] Every stdout line parses as one JSON object, text with newlines included. Logs go to
      stderr.
- [ ] `--once` prints one line and exits 0, including when two matching events arrive together.
- [ ] `--after` prints the matching messages already in the chat, oldest first, before any live
      one, and prints none of them twice when the stream repeats one.
- [ ] A dead credential prints one `stopped` line and exits 1. Nothing prompts.
- [ ] A reconnect prints one `gap` line and the watch goes on.
- [ ] The 11th match within a minute is not printed. One `suppressed` line says how many were
      held back.
- [ ] Two `watch` processes running at once both receive their events.
- [ ] A closed stdout ends the process quietly, as `stream` does.

## Phases

### Phase 1 — matcher and live follow
- Work: the matcher as a pure function, the `watch` subcommand, the JSON line, a per-process
  endpoint id, the silent start of the stream, `--once`.
- **Data model impact**: none
- **DoD**: `pytest tests/test_watch.py` green for the Matching criteria and the first three
  Command criteria. Live: `miniteams watch --thread 48:notes`, a message typed in Teams, one
  line out. `pre-commit run --all-files` rc=0.

### Phase 2 — catch-up, exclusion, service lines
- Work: `--after` catch-up from the chat history, the sent file written by the MCP server and
  read by `watch`, `gap`, `suppressed`, `stopped`.
- **Data model impact**: none. One bounded file under the cache dir.
- **DoD**: tests green for the remaining criteria. Live: a watch stopped, two messages typed,
  the watch started again with `--after`, both come out once.

### Phase 3 — Monitor and docs
- Work: README, `docs/` page with the Monitor call.
- **Data model impact**: none
- **DoD**: in a Claude Code session, each of the four commands of the Solution table runs under
  Monitor and its event reaches the session. A `--once` watch ends its monitor.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- Notifications are armed on demand, on declared criteria (babs, 2026-09-30).
- Delivery through a command and Claude Code's Monitor tool, not through channels
  (babs, 2026-09-30). Channels are a research preview behind a development flag and drop events
  without an error.
- The reaction that serves as a signal is chosen by whoever arms the watch. No fixed key
  (babs, 2026-09-30).
- No watch tools in the MCP server. They would need the stream inside the server and a poll
  tool, for a client nobody uses here. Overturnable.
- One JSON object per line. Monitor takes a line as an event, and a message with line breaks
  would otherwise become several.
- The exclusion covers what the MCP server sent, not the CLI or the widget: those are typed by
  you, and a `--from me` watch is meant to see them.
- `run_forever` calls `TokenSource.acquire()`, which prints its prompt on stderr and blocks.
  `watch` needs a switch that makes it silent only.
- Every in-memory set keyed per message is an LRU with a cap, as the stream runs for the life of
  the process.
- No text matching. The criteria stay on who, where and what kind, which a test can enumerate.
