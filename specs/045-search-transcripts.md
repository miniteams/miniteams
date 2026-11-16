# 045 — Search the local transcripts

**Status**: shipped
**Requested by**: babs
**Date**: 2026-11-16

## Problem

The archive holds two kinds of spoken transcripts: the follower's own (spec 044, `data/follow/`) and the
Teams recording transcripts already downloaded (`<chat>/media/*.transcript.json`). An agent can page one
transcript it already knows, but cannot find which call said something.

## Solution

A new MCP tool, `search_transcripts`, finds the spoken lines that contain a word or phrase, across both
kinds, newest first, with a little context around each hit. The README and the agent archive doc describe
it.

## Scope

- MCP tool `search_transcripts(query, since?, until?, thread_id?, context?, limit?)`.
- Sources: followed calls (NDJSON lines) and recording transcripts (`.transcript.json`, the rendition that
  names its speakers).
- Docs: README MCP section, `docs/agent-archive.md`, `specs/008-mcp-stdio.md`.

## Out of scope

- An index or full-text engine: a linear scan, like `search_messages`.
- VTT renditions (no speaker names, finer cues).
- Searching chat messages and transcripts in one call (`search_messages` stays as is).

## Acceptance criteria

- [x] **AC1** A query matches a line's text case- and accent-insensitively (the same folding as
      `search_messages`), across followed calls and recording transcripts; hits come newest first.
- [x] **AC2** `since` / `until` filter on the time of the line (a followed line's `t`; a recording turn's
      recording start plus its start offset); `since` defaults to 7 days back, like `search_messages`.
- [x] **AC3** `thread_id` restricts the search to one chat; a 1:1 chat id matches in either spelling.
- [x] **AC4** Each hit carries `source` (`follow` or `recording`), `thread_id`, `chat` (label when the
      archive knows it), `call` or `message_id`, `speaker`, `time` (UTC ISO), `text`, and `context`: up to
      `context` lines before and after (default 1, max 5), each with speaker, time and text.
- [x] **AC5** `limit` (default 50, max 200) caps the hits; `more` says whether others match.
- [x] **AC6** A transcript still being written is searched up to its last complete line; an unreadable or
      malformed file is skipped (logged), never fatal; followed calls are searched without an archive
      index.
- [x] **AC7** The README, `docs/agent-archive.md` and `specs/008-mcp-stdio.md` document the tool.

## Phases

### Phase 1 — Tool, tests, docs
- Work: `Archive.search_transcripts` in `mcp.py` reusing the folding of `search_chat`, `transcript_files`,
  `recordings()` and `same_chat`; the tool registration; tests on synthetic transcripts of both kinds;
  the three docs.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7
- **Data model impact**: none
- **DoD**: tests citing each AC pass; coverage does not drop; a live run of the tool against the real
  archive finds a word from this morning's followed calls (counts only reported).

## Data model impact (summary)

None.

## Open questions

- (none)

## Decisions

- Both sources, one new tool (user call, 2026-11-16).
- A recording turn's time is the recording's start plus the turn's `startOffset`. The recording message is
  posted at the recording's end, so the start is its compose time minus `duration` (compose time alone
  when `duration` is missing).
