# 010 — Outgoing typing indicator (`miniteams typing`)

**Status**: implemented
**Requested by**: babs
**Date**: 2026-10-01

## Problem

A demo or a screenshot of a chat sometimes needs the "… is typing" line on the other side. Today
that means typing for real in Teams while someone else takes the capture.

## Solution

`miniteams typing --thread <id> --for 30` shows your typing indicator in that chat for about
30 seconds. Without `--for` it sends the indicator once.

## Scope

- `send_typing()` in `send.py`: `POST …/conversations/<thread>/messages` with
  `{"messagetype": "Control/Typing", "contenttype": "Application/Message", "content": ""}`.
- `miniteams typing [--thread ID] [--for SECONDS]`. Default thread is `48:notes`, like `send`.
- `--for` re-sends every 5 s until the duration is covered. Ctrl-C stops it.

## Out of scope

- Typing indicator from the widget's composer or from the MCP server.
- Clearing the indicator with `Control/ClearTyping`: it expires by itself, and purple-teams stopped
  sending the clear.
- Refreshing the skype token during a hold (a hold is limited to the token's life, about 45 min).

## Acceptance criteria

- [x] `send_typing` posts the body above to the url-encoded thread and returns the HTTP status.
- [x] `typing` with no option sends once to `48:notes`.
- [x] `typing --for N` sends at 0, 5, 10… s and stops once the next send would start at or after N.
- [x] Ctrl-C during a hold exits 0 without a traceback.
- [x] `--for -1` is refused by the parser, before any Teams call.
- [x] On a real 1:1, the other member sees the indicator, without a gap over a 30 s hold.

## Phases

### Phase 1 — function, command, docs
- Work: `send_typing`, `cmd_typing` and its parser, tests, README usage line.
- **Data model impact**: none
- **DoD**: `pytest` green with tests for the request shape, the resend schedule, Ctrl-C and the
  parser; `pre-commit run --all-files` rc=0; one live hold on a real chat seen by its other member.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- CLI only (babs, 2026-10-01). The MCP tool was judged useless, the widget hook is not needed for
  captures.
- Wire format taken from purple-teams `teams_conv_send_typing_to_channel`; Teams answered `201`.
- 5 s resend interval: a 30 s hold at that rate showed without a gap in the Teams client
  (2026-10-01). purple-teams re-sends every 20 s, not tried here.
