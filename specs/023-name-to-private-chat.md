# 023 — A sender's name leads to the private chat with them

**Status**: phase 1 shipped
**Requested by**: babs
**Date**: 2026-10-16

## Problem

In a group chat or a meeting chat, answering someone privately means finding your 1:1 with them in
the list by hand.

## Solution

Clicking a message's sender name in a chat with several people opens your private chat with that
person in the same page; when you have never written to each other, it opens an empty private chat
whose first message creates it.

## Scope

- **Target.** The sender name at the head of a message, in a chat that is not already a 1:1; not my
  own name. Pointer cursor, focusable, Enter does the same as a click. Middle click and Ctrl/⌘ click
  open it in a new tab, as rows of the list do.
- **Existing 1:1.** The server finds the private chat that holds both of us (Teams' `unq.gbl.spaces`
  id, either order of the two ids: not always sorted), from the board rows and the archive index;
  the page switches to it like a click in the list (Back returns to the group chat).
- **No 1:1 yet.** After the research of phase 2: the page opens an empty private chat with the
  person, the composer ready, and the first message creates it; if no clean path exists without
  Graph, the name instead offers "Open in Teams" on a new chat with that person.

## Out of scope

- Mentions, read-receipt avatars and the members popover as targets (later, once the action exists).
- The widget.

## Acceptance criteria

- [x] **AC1** In a chat with several people, someone else's sender name is a link-like target (pointer,
  Tab, Enter); mine is not, and a 1:1 shows none.
- [x] **AC2** Clicking it, when a 1:1 with that person exists in either id order, switches the page to
  that 1:1; Back returns to the group chat.
- [x] **AC3** Middle click or Ctrl/⌘ click opens that 1:1 in a new tab.
- [ ] **AC4** With no 1:1 yet, the page opens an empty private chat with that person whose first
  message, once sent, creates the chat in Teams (or, if phase 2 finds no clean path, the name offers
  "Open in Teams" on a new chat with them).
- [x] **AC5** The lookup is answered by the server from what it holds (no Teams call for an existing
  1:1); an unknown person or a failed lookup says so on the error line.

## Phases

### Phase 1 — Existing private chats
- Work: a page verb that answers the 1:1 holding me and a given MRI (board rows, then the archive
  index, both id orders); the clickable sender name; same-page switch and new-tab variants.
- **Covers**: AC1, AC2, AC3, AC5
- **Data model impact**: none
- **DoD**: unit tests for the lookup (both orders, none, unknown); browser tests in Chrome and Firefox;
  full suite and pre-commit green.

### Phase 2 — A first private chat
- Work: read-only research of how a 1:1 comes to exist (archive shapes, purple-teams, a capture if
  needed), one live trial on the test chat's other member only if needed and agreed; then the empty
  private chat whose first message creates it, or the "Open in Teams" fallback.
- **Covers**: AC4
- **Data model impact**: none
- **DoD**: research report; the chosen path working end to end on a real account; browser tests for
  the empty chat; full suite and pre-commit green.

## Data model impact (summary)

None.

## Open questions

None for phase 1. Phase 2's path depends on its research.

## Decisions

- Sender names only, same page, research then creation (user's calls, 2026-10-16).
- Middle click and Ctrl/⌘ click open a new tab — because rows of the list already behave so.
- The lookup searches both id orders — because some private chats are not in sorted
  order, so the id cannot be computed.
- A double-click on a name opens the 1:1 like a click, as on any link; drag-selecting a name does
  not (phase 1 review).
