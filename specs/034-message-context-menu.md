# 034 — Message context menu

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-27

## Problem

In the chat page, a message's actions (react, reply with quote, edit, delete, open in Teams, copy link)
live in the bar that shows on hover, a small target at the message's top right. A right click on a
message opens the browser's own menu, which knows nothing about messages. Copying a message's text
takes a manual selection.

## Solution

A right click on a message opens a menu at the pointer with that message's actions: a row of the
favorite reactions plus `…` for any emoji, then Reply, Edit, Delete, Copy text, Copy link and Open in
Teams. On a link, an image or selected text, the browser's own menu stays. The hover bar stays as is.

## Scope

- **Trigger.** `contextmenu` on a message in `#msgs`, by mouse or keyboard (the Menu key or Shift+F10
  on a focused message). The menu opens at the pointer, or under the message's top left from the
  keyboard, kept inside the viewport.
- **Native menu kept** (user decision, 2026-10-27) when the target is inside a link (`a[href]`) or an
  image, or when the selection is a non-empty range inside that message.
- **Content.** The same actions, under the same conditions, as the message's hover bar (the
  favorite reactions with mine marked and toggling, `…` opening the emoji search, Reply, Edit, Delete,
  Open in Teams, Copy link), plus **Copy text** (user decision, 2026-10-27): the message's plain text
  without its reply quotes, as a reply quote reads it. Read-only chats show Open in Teams, Copy link
  and Copy text only, like the bar. Copy text and Copy link need a secure context, like Copy link today.
  A deleted message, or one with no text (an image or a card alone), has no Copy text.
- **Behaviour.** Each item runs the bar's action and closes the menu; the menu's copies confirm with a
  short "Copied" note at the bottom of the list. Delete keeps its confirm. One menu at a time; it
  closes on Escape, a click outside, a scroll by the reader, the chat switching, or the message being
  drawn again. When the list follows new messages, the menu moves with its message. Escape and a keyboard pick put the focus back on the message.
- **Keyboard.** The first item takes the focus; ↑/↓ move between items, Home/End to the ends,
  Enter/Space run, Tab leaves and closes.

## Out of scope

- New message actions beyond Copy text (forward, pin, mark unread, save).
- A context menu in the widget's chat list or elsewhere in the chat page (composer, header).
- Touch long-press.
- Changes to the hover bar.

## Acceptance criteria

- [x] **AC1** Given a message I can react to, when I right click its body, then a menu opens at the
  pointer listing the favorite reactions, `…`, and the actions its hover bar shows, plus Copy text; the
  browser menu does not open.
- [x] **AC2** Given the menu is open, when I pick a favorite reaction, then the reaction is sent
  exactly as the bar's picker sends it (toggling mine off) and the menu closes.
- [x] **AC3** `…` opens the emoji search; picking an emoji reacts with it.
- [x] **AC4** Reply adds the message as a quote in the composer; Edit opens its editor; Delete asks
  for confirmation then deletes; Open in Teams opens the message's Teams link; Copy link copies the same
  link as the bar's 🔗. Each closes the menu.
- [x] **AC5** Copy text puts the message's plain text, reply quotes left out, in the clipboard.
- [x] **AC6** A right click on a link, an image, or text selected inside the message opens the
  browser's menu, not ours.
- [x] **AC7** Edit and Delete appear only on my own messages under the bar's conditions; Reply only
  where the bar offers it; a deleted message offers Open in Teams and Copy link only; a message with no
  text (an image or a card alone) has no Copy text.
- [x] **AC8** In a read-only chat the menu offers Open in Teams, Copy link and Copy text (when the
  message has text) only.
- [x] **AC9** Escape, a click outside, a scroll by the reader, a chat switch, or opening another
  message's menu closes the open menu; at most one menu exists in the page. A new message arriving
  while the list is at the bottom keeps the menu open, beside its message.
- [x] **AC10** On a focused message, the Menu key or Shift+F10 opens the menu with its first item
  focused; ↑/↓/Home/End move, Enter runs, Escape closes and refocuses the message.
- [x] **AC11** A menu opened near the right or bottom edge stays fully inside the viewport.

## Phases

### Phase 1 — Context menu
- Work: in `chat.html`, a `contextmenu` handler on `#msgs` that builds the menu from the record the
  message was drawn from, reusing the bar's handlers (`react`, `addQuote`, `startEdit`, the delete
  send, `teamsA`, `copyLink`, `plainOf`), its CSS next to the `.actions` rules, keyboard handling,
  dismissal. README chat-page section mentions the menu.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7, AC8, AC9, AC10, AC11
- **Data model impact**: none
- **DoD**: existing test suite green; every AC checked in a test server (own `--bind`) with a
  throwaway `chromium --app`, reactions sent in the test channel only.

## Data model impact (summary)

None.

## Open questions

- [x] Native menu on links, images, selections: kept (2026-10-27).
- [x] Extra actions: Copy text only (2026-10-27).

## Decisions

- The hover bar stays: the menu is a second way in, not a replacement.
- Item order follows Teams: reactions first, then actions.
- The bar's per-action rules are the menu's rules, so the two never disagree on what a message allows.
