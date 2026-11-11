# 041 — Tab and Shift+Tab indent list items

**Status**: shipped
**Requested by**: babs
**Date**: 2026-11-11

## Problem

Nesting a list item means typing spaces by hand. In Teams, Tab indents the list item under the
caret and Shift+Tab brings it back. In the composer, Tab moves focus away instead.

## Solution

In a list, Tab nests the item under the caret one level, and Shift+Tab moves it up one level. Each
item takes its sublists and line breaks with it. Outside a list, Tab behaves as it does today.

## Scope

- Where: the chat page composer and message edit, which share `formatBar.key`.
- Tab on a list item, or on one of its line-break lines:
  - The item's lines, its line breaks and every deeper item below it shift right together, the
    item to the indent given below. The item then nests in its sibling.
  - The first item of a list has no sibling to nest in. Tab does nothing there and keeps focus.
  - The sibling is the item before it at its level, as compose reads the list. Tab aligns the item
    with the sibling's sublist when it has one, else puts it two spaces past the sibling's indent.
  - A move compose would read otherwise (another item changing level, a line break lost) does
    nothing; spaces typed by hand still nest.
- Shift+Tab on a list item:
  - The same block shifts left to the parent item's indent. Siblings that were below it at its old
    level become its children, as compose already reads them.
  - On a top-level item, Shift+Tab does nothing and keeps focus.
- Selection over several items: every item from the first one to the last one shifts together,
  each with its subtree. The first item is checked as above. An end on a plain line keeps the items
  in the range. A selection holding any list line keeps the key, even when the move is refused.
  A selection over items at uneven indents that no shared shift can move does nothing.
- Markers stay as typed. Teams numbers each `ol` from 1, and so does the preview.
- One undo step per key. Mentions stay anchored, as with Enter.
- These keys keep their current behaviour:
  - Tab or Shift+Tab outside a list moves focus.
  - With the completion list open, Tab picks.
  - With Ctrl, Alt or Meta held, Tab is not handled.

## Out of scope

- Renumbering items after a move, since the posted HTML does not carry numbers.
- Shift+Tab on a top-level item turning it back into plain text.

## Acceptance criteria

- [x] **AC1** `- a\n- b|` + Tab gives `- a\n  - b`. `1. a\n2. b|` + Tab gives `1. a\n  2. b`.
- [x] **AC2** `- a\n  - b|` + Shift+Tab gives `- a\n- b`.
- [x] **AC3** The moved item takes its subtree and line breaks along:
  - `- a\n- b|\\\n  b2\n  - c` + Tab gives `- a\n  - b\\\n    b2\n    - c`.
  - Shift+Tab gives back the original text.
- [x] **AC4** In `- a\n  - b|\n  - c`, Shift+Tab gives `- a\n- b\n  - c`.
- [x] **AC5** Tab on the first item of a list, or Shift+Tab on a top-level item:
  - The text does not change.
  - Focus stays in the box.
  - Nothing is sent.
- [x] **AC6** Outside a list, Tab and Shift+Tab move focus as before. With the completion list open,
  Tab picks.
- [x] **AC7** In `- a\n- b\n- c`, a selection over `- b` and `- c` + Tab gives `- a\n  - b\n  - c`, the
  selection still on the same characters.
- [x] **AC8** Ctrl+Z undoes each Tab or Shift+Tab in one step.
- [x] **AC9** A mention in a moved item, or in a line after it, is posted with its anchor.
- [x] **AC10** The same keys work in the message edit composer.
- [x] **AC11** The README composer section mentions Tab and Shift+Tab.

## Phases

1. `listIndent` in `formatBar`, wired to Tab in `key`, browser tests, README update.
   **Covers**: AC1–AC11. **DoD**: the browser tests citing each AC pass under Chromium and Firefox; the full suite is green.

## Decisions

- Tab adds two spaces whatever the marker. compose only needs a deeper indent, and decompose writes
  two spaces per level, so an edited message and a typed one look the same. When the sibling already
  has a sublist, Tab takes that sublist's indent instead, or the item would nest in its last child.
  A sibling at another indent than the item's: two spaces past the sibling's.
- Each press is checked against compose's level stack on the new text: the moved items one level
  deeper or up, every other item and line break unchanged, else no change.
