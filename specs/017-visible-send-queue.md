# 017 — Visible send queue, burst sends from the widget

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-15

## Problem

The chat page queues burst sends (PR #43) but shows nothing of the queue: a message typed and sent
vanishes until Teams echoes it, and the user cannot tell what is still waiting. The widget's quick
reply has no queue at all: it locks while a message is in flight and closes once it has gone, so a
second message means reopening the reply.

## Solution

Both pages queue sends the same way, one at a time per chat, in order. The chat page shows each
waiting message as a greyed bubble at the bottom of the thread with its state; the bubble turns into
the real message when Teams echoes it. The widget's reply stays open after each send and lists the
waiting messages under it, one line each.

## Scope

- **One queue.** The chat page's outbox moves to `ui.js` and both pages use it: per chat, one
  message in flight, the rest in send order.
- **Failures stay in the queue.** A failed send keeps its place, labelled `failed` with the reason;
  the composer is never touched; the messages behind it wait as `paused`. Three actions on the failed
  item: ↻ retry (same id, checked), ✎ edit (into the composer when it is empty, else in place in the
  item), ✕ discard. Retry, an edit sent again, or discard resumes the queue. This replaces PR #43's
  "text back in the box" in the chat page.
- **Widget burst sends.** Enter empties the box at once and keeps the reply open and focused; an
  acknowledged send no longer closes it.
- **Chat page bubbles.** One greyed bubble per queued or in-flight message, after the last message,
  in send order, labelled `queued`, `sending…`, `failed` or `paused` (queued behind a failure). Teams' echo
  (same `clientmessageid`) replaces the bubble in place.
- **Widget lines.** Under the open chat's reply, one truncated line per queued or in-flight message
  with the same labels. A line leaves when its send is acknowledged.
- **Reload.** A page reloaded with messages still queued keeps them queued (both pages): they show
  again and go out in order once the socket is back; the one that was in flight goes out with its
  own id and the check, never twice. The unsent draft stays the draft.

## Out of scope

- A server-side queue, or one order shared by the widget and a chat page writing to the same chat.
- A queue indicator on widget rows other than the open reply.
- Editing or cancelling a queued message other than by the failure path.

## Acceptance criteria

- [ ] **AC1** Widget: with a message in flight, Enter on a new message empties the box, keeps the
  reply open and focused, and the messages reach the server one at a time, in order.
- [ ] **AC2** Widget: when the last queued send is acknowledged, the reply stays open and empty.
- [ ] **AC3** Both pages: a failed send stays in the queue at its place, labelled `failed` with its
  reason; the composer's text is untouched; the messages behind it read `paused` and do not go out.
- [ ] **AC4** Chat page: each queued or in-flight message shows as a greyed bubble after the last
  message, in send order, labelled `queued` or `sending…`.
- [ ] **AC5** Chat page: Teams' echo of a sent message replaces its bubble: one message, never both,
  never neither in between.
- [ ] **AC6** Both pages: on a failed item, ↻ retries it with the same id and the check, ✕ discards
  it, ✎ moves its text into an empty composer or edits it in place when the composer holds a draft;
  sending it again or discarding it lets the paused messages go, in order. The actions are buttons
  reachable by Tab.
- [ ] **AC7** Widget: under the open chat's reply, one truncated line per queued or in-flight
  message, labelled like the bubbles; a line leaves when its send is acknowledged.
- [ ] ~~**AC8** Both pages: a reload with messages still queued keeps their text as that chat's draft.~~
  Superseded by AC12 (user's call, 2026-10-15).
- [ ] **AC9** One queue implementation, in `ui.js`; neither page keeps its own outbox.
- [ ] **AC10** Bubble and line text is the message's plain text, escaped: markup typed in a message
  never renders.
- [ ] **AC11** A reader at the bottom of the chat stays there when bubbles appear, change or leave.
- [ ] **AC12** Both pages: a reload (a newer page, or after a server restart) with messages queued or
  in flight keeps them queued in order; the in-flight one goes out again with the same id and the
  check, so Teams never gets it twice; the composer keeps only the unsent draft.
- [ ] **AC13** Both pages: when the socket drops with a message in flight, that message goes back to
  `queued` (no error, no manual retry); once the socket is back the queue sends it again with the
  same id and the check, then the rest, in order.

## Phases

### Phase 1 — Shared queue, widget burst sends
- Work: `ui.js` send queue (list, pump, ack, failed item kept at its place, retry / edit / discard)
  built from chat.html's outbox; chat page and widget switch to it; the widget keeps its reply open;
  the queue persists in `sessionStorage` across a reload (AC12) and survives a dropped socket (AC13).
- **Covers**: AC1, AC2, AC9, AC12, AC13
- **Data model impact**: none
- **DoD**: `uv run --with playwright pytest tests/test_browser*.py` green, including widget burst,
  failure and reload tests; no `outbox` left in `chat.html` or `widget.html`.

### Phase 2 — Showing the queue
- Work: chat page bubbles with states, replaced by the echo (the chat record carries its
  `clientmessageid`); widget lines under the reply.
- **Covers**: AC3, AC4, AC5, AC6, AC7, AC10, AC11
- **Data model impact**: none
- **DoD**: browser tests for each state, the echo hand-over, the failure, markup in a message, and
  the reader kept at the bottom, in Chrome and Firefox for the chat page; full suite and pre-commit
  green.

## Data model impact (summary)

None. The chat record sent to the page gains a `cmid` field (the message's `clientmessageid`).

## Open questions

None.

## Decisions

- The queue survives a reload in `sessionStorage` (user's call, 2026-10-15) — because folding it into
  the draft merged the messages and could post the in-flight one twice.
- A dropped socket requeues the in-flight message and resends it checked on reconnect (user's call,
  2026-10-15) — because the connection is often intermittent and the check rules out a duplicate;
  it replaces the "connection lost: Enter checks the chat" error and its manual retry (012-AC23).
- A queued message's pasted images do not survive a reload; its text does, with a note to paste
  them again — because images are too large for `sessionStorage`.

- A bubble stays until Teams' echo, not until the server's acknowledgement — because dropping it on
  the acknowledgement leaves a gap until the echo lands, the flicker this feature removes.
- A failure stays in the visible queue with retry / edit / discard instead of PR #43's "text back in
  the box" — because putting it back merged it with the draft being typed and hid the pause (user's
  call, 2026-10-15).
- ~~The widget's queued text survives a reload as a draft.~~ Superseded: the queue itself survives
  (AC12).
- A checked resend whose id is still in flight on the server waits for that attempt's outcome instead
  of being refused — because a socket dropped mid-send leaves the server's send running, and the
  automatic resend (AC13) would otherwise fail on it.
