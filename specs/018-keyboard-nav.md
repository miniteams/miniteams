# 018 — Keyboard navigation in the chat page

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-17

## Problem

In the chat page, reaching an older message to edit, quote or react to it takes the mouse: the
keyboard can type, but not move through the thread. Tab walks every control of every message, so
crossing a long chat by Tab is no faster.

## Solution

The border between typing and navigating blurs: ↑ from the composer steps into the thread, the
arrows walk messages, a letter acts on the focused one, and Escape or typing goes back to the
composer.

## Scope

- **Composer → thread.** ↑ in the composer, when it is empty or the caret is on its first *visual*
  line (ui.js's caret mirror), focuses the last message. Not with a selection, an open completion
  list or an IME composition.
- **Thread.** One Tab stop (roving tabindex, like the split list's rows). ↑/↓ move by message,
  PgUp/PgDn by a screen, Home/End to the first/last loaded message; reaching the top loads older
  history as scrolling does. Folded runs (`.cont`) are steps too, their head shown with them. The
  focused message is scrolled into view and outlined.
- **Thread → composer.** ↓ on the last message, or Escape, returns to the composer, caret at the
  end. A printable character other than Space that is not an action key returns to the composer and
  lands there.
- **Space.** On a message or a link in it, Space scrolls the thread one screen down, Shift+Space one
  screen up, like a feed. The focus stays unless its message leaves the view; then it moves to the
  first (down) or last (up) message in view. A focused button keeps its own Space.
- **Actions on the focused message.** `e` edit (own, editable), `r` quote, `+` react (opens the
  picker), `p` private chat with its sender (spec 023; same rules as clicking the name), Enter on a
  quote follows it (spec 021). Each key does nothing where its button would not be offered.
- **Tab stops.** The per-message controls (action bar buttons, sender names) leave the Tab order;
  they stay clickable. The formatting bar (B I S …) becomes one toolbar Tab stop walked by ←/→.
  Links in message bodies (URLs, shared files, card toggles, code blocks) are out of Tab too, except in
  the focused message: Tab walks its links in order, then leaves for the toolbar; Shift+Tab walks back
  to the message.
  On a link, ↑/↓ move to the previous/next message, Escape returns to its message, Enter stays the
  link's own. So Tab hops zone to zone: header, thread, [focused message's links], toolbar, composer.
- **Search.** `/` from the thread focuses the chat's search box (`#find`); Ctrl+Shift+F does from
  anywhere in the page. Ctrl+F stays the browser's.
- **Unchanged.** Ctrl+K (conversation panel and its search, ↓ walks the results), the list's own
  keys, the widget.

## Out of scope

- The widget's quick reply.
- Shortcuts for delete, copy or mark unread.
- A shortcut help overlay.

## Acceptance criteria

- [x] **AC1** ↑ in an empty composer, or with the caret on its first visual line, focuses the last
  message; not with a selection, an open completion list or during IME composition.
- [x] **AC2** The thread is one Tab stop; ↑/↓, PgUp/PgDn, Home/End move the focus; folded runs are
  steps; the focused message is in view and outlined; reaching the top loads older history.
- [x] **AC3** ↓ on the last message or Escape returns to the composer with the caret at the end; a
  printable character other than Space that is not an action key returns there and is typed. Space /
  Shift+Space on a message or its link scroll the thread a screen down / up, nothing typed, the focus
  kept on a message in view.
- [x] **AC4** On the focused message, `e`, `r`, `+`, `p` and Enter (on a quote) do what their
  buttons or clicks do, and nothing where those are not offered.
- [x] **AC5** Per-message controls are out of the Tab order; the formatting bar is one Tab stop
  walked by ←/→; Tab hops header → thread → [focused message's links] → toolbar → composer; on a
  link, ↑/↓ move by message and Escape returns to its message.
- [x] **AC6** `/` from the thread and Ctrl+Shift+F from anywhere focus the chat search; Ctrl+F is
  left to the browser.
- [x] **AC7** Ctrl+K and the list's keys behave as before.

## Phases

### Phase 1 — Keyboard navigation
- Work: roving focus in the thread, composer ↔ thread, action keys, toolbar Tab stop, search keys.
- **Covers**: AC1–AC7
- **Data model impact**: none
- **DoD**: browser tests per AC in Chrome and Firefox; existing Tab-order tests (011-AC29, 023)
  updated to the new zones; full suite and pre-commit green.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- ↑ on the first visual line, not only in an empty composer; F6 dropped; Ctrl+K kept as is (user's
  calls, 2026-10-15).
- `/` and Ctrl+Shift+F for the chat search, Ctrl+F left to the browser — the open point of
  2026-10-15, settled by default; overturnable.
- `p` for the private chat — sender names leave the Tab order (one stop per message would defeat
  the single thread stop), so spec 023's action needs a key.
- Links: out of Tab except in the focused message, walked by Tab (user's call 2026-10-17).
- Space scrolls the thread a screen like a feed, Shift+Space up; the focus moves only to stay in view
  (user's call 2026-10-17).
- Space on a card toggle toggles it (native button behaviour), not scroll.
