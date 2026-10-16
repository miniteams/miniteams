# 020 — Mark a chat read in Teams when it is shown

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-16

## Problem

Opening a chat in the chat page leaves it unread in Teams: the official client, the phone and the
widget's Unread tab still count it until 👁 "mark read in Teams" is clicked by hand on its row.

## Solution

An option in the chat page's ⚙ menu, on by default: a chat shown in the chat page is marked read in
Teams when it opens and again whenever a message lands while it is in view, the way the Teams client
does.

## Scope

- **Setting.** ⚙ menu, "Mark read in Teams when shown", a checkbox stored with the other ⚙
  preferences; on by default.
- **On open.** Opening a chat (first load, a click or Enter in the list, Back/Forward) marks it read
  once its latest messages are on screen.
- **While shown.** A message from someone else arriving in the shown chat marks it read when the page
  is visible, the window has the focus and the newest message is in view; otherwise as soon as all
  three hold again (back to the window, back to the latest message).
- **One request per second.** At most one `read` per chat per second; further ones collapse into one
  trailing send, which checks the conditions again when it fires.
- **Same verb.** The existing `read` page verb (Teams' read marker up to the row's last message); no
  server change.

## Out of scope

- The widget (its click opens Teams, which marks read itself).
- Marking read without the chat being shown (e.g. on a mention notification).
- Read receipts' privacy: Teams shows others that you read, as with any client.

## Acceptance criteria

- [x] **AC1** The ⚙ menu has "Mark read in Teams when shown", checked by default; unchecking it is
  kept across a reload, and with it unchecked no automatic `read` is sent (👁 still marks by hand).
- [x] **AC2** Opening a chat, by first load, a click, Enter in the list or Back/Forward, sends one
  `read` for it once its history is shown and the archive has caught up (no "loading latest
  messages…").
- [x] **AC3** A message from someone else arriving in the shown chat sends `read` when the page is
  visible, focused and the newest message is in view; a message of mine never triggers one.
- [x] **AC4** With the page hidden or unfocused, or the reader detached in older history, an arriving
  message sends nothing; the `read` goes out as soon as the page is back in view, focused, at the
  latest message.
- [x] **AC5** A burst of messages sends at most one `read` per chat per second, the rest collapsed
  into one trailing send that goes out only if the chat is still shown, visible, focused and at its
  latest message.

## Phases

### Phase 1 — Auto read
- Work: the ⚙ checkbox and its preference; the triggers on open and on arrival; the visibility,
  focus and at-latest conditions; the coalescing.
- **Covers**: AC1, AC2, AC3, AC4, AC5
- **Data model impact**: none
- **DoD**: browser tests per AC in Chrome and Firefox (focus and visibility driven by the test);
  full suite and pre-commit green.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- No read while the archive lags — because the server marks read up to the row's last message, which
  the page has not shown yet.
- Coalescing by time, one per second with a trailing send — because the `read` verb gets no answer to
  wait for.
- On by default (user's call, 2026-10-16).
- Triggers on open and on each arrival while in view (user's call): the Teams client's behaviour.
- Only when the newest message is in view — because a reader scrolled back in history has not seen
  what just arrived.
