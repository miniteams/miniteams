# 040 — Nested lists in the composer

**Status**: in progress
**Requested by**: babs
**Date**: 2026-11-10

## Problem

The composer writes flat `- ` / `1. ` lists only: an indented item is sent as a plain line. Editing a
message that holds a nested list, often written in Teams, is refused with "can't edit here: a nested
list" (spec 019 AC9). The chat already displays nested lists.

## Solution

An indented list item nests under the item above it, in bullets and numbers alike, mixed at will.
The message posts as Teams writes it, so Teams shows the same tree. A message holding a nested list
opens in the editor, its sublists indented by two spaces per level, and saves back unchanged when
nothing was touched.

## Scope

- Compose (chat page composer, message edit, MCP and CLI `format: full` sends; the widget's quick reply
  sends inline text and has no lists):
  - A list line indented further than the list item above it starts a sublist inside that item.
  - A list line indented less goes back to the level whose item it lines up with, or the nearest
    shallower one.
  - Each sublist is `ul` or `ol` from its own marker; a different marker at the same level starts a
    new list, as it does today at the top.
  - Output shape, as Teams writes it (375 of 3021 archived list messages):
    `<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>`.
- Edit (decompose): a sublist inside a `<li>`, after the item's text, reads back as indented lines.
  Written back with two spaces per level.
- List continuation (chat page composer and message edit), as in Teams:
  - Enter on a list item opens the next item at the same level, `- ` again or the next number; on an
    item holding only its marker it goes up one level, or at the top empties the line and ends the list.
  - Enter sends (or saves) only outside a list: on a list item or one of its continuation lines it
    never sends.
  - Shift+Enter on a list item breaks the line inside the item: `\` (CommonMark hard break), a new
    line, and the indent of the item's text.
- Line breaks inside an item (compose and edit): an item line ending with `\`, followed by a line
  indented at least to the item's text and not itself a list item, continues the item: `<br>` inside
  the `<li>`. The edit flow reads `<li>a<br>b</li>` back that way (Teams writes such items).
- README composer section mentions indented lists, continuation and line breaks in items.

## Out of scope

- Tab / Shift+Tab to indent in the composer: Tab moves focus and picks completions today.
- Text after a sublist inside the same item, a list directly inside a list (`<ul><ul>`), quotes or
  code inside list items: never seen in the archive (0 of 3021), still refused.
- Card markdown (`card_markdown_html`): Teams card text has no nesting to read.

## Acceptance criteria

- [ ] **AC1** `- a\n  - b\n- c` posts `<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>`.
- [ ] **AC2** Mixed kinds: `1. a\n   - b\n2. c` posts an `ol` whose first item holds a `ul`.
- [ ] **AC3** Three levels deep, then back to the top in one step, nest and close correctly.
- [ ] **AC4** An indented list line with no list item above it (first line, after text, after a
  quote) starts a top-level list.
- [ ] **AC5** An indented line that is not a list item keeps today's behaviour (plain line).
- [ ] **AC6** The edit flow opens the message in AC1's shape as `- a\n  - b\n- c` and saving it
  unchanged posts the same HTML.
- [ ] **AC7** The edit flow opens Teams' archived shape, with its `\r\n` layout whitespace and
  `&nbsp;` text, and round-trips it.
- [ ] **AC8** A `<li>` with text after its sublist, or a list directly inside a list, is still
  refused with "a nested list".
- [ ] **AC9** The chat page preview shows the nested tree before sending.
- [ ] **AC10** Live: a nested list sent to the test chat "miniteams" shows nested in Teams web.
- [ ] **AC11** Enter at the end of `  - b` gives `  - b\n  - `; at the end of `2. c`, `2. c\n3. `.
- [ ] **AC12** Enter on an empty `  - ` turns it into `- ` (one level up); on an empty top-level
  `- `, the line is emptied and the list ends. Undo (Ctrl+Z) takes back each step.
- [ ] **AC13** Enter mid-item splits it: the text after the caret moves to the new item; a selection
  within one line's text is replaced by the split; a caret in the marker or indent counts as at the
  item's text (an empty item opens above).
- [ ] **AC14** Enter on a list item or a continuation line never sends nor saves; outside a list Enter
  sends (saves) as before; after an empty top-level item ends the list, the next Enter sends. A selection
  reaching another item or plain text, or a next number past 9 digits, gives a plain newline or
  nothing, never a send; one within the item (break lines included) is removed and the item split.
- [ ] **AC15** Shift+Enter at the end of `  - b` gives `  - b\\\n    `; on a continuation line, another
  break at that line's indent; at a break (right after its `\`), an empty break line between the two.
- [ ] **AC16** `- a\\\n  b\n- c` posts `<ul><li>a<br>b</li><li>c</li></ul>`, nested items alike; a
  trailing `\` not followed by a continuation line stays as today.
- [ ] **AC17** The edit flow opens `<li>a<br>b</li>` as `- a\\\n  b` (nested: the item's text column)
  and saves it unchanged; Teams' archived list items holding a `<br>` open.
- [ ] **AC18** Enter on a continuation line opens the next item at its item's level; on an empty one,
  the empty line and its `\` give way to that next item.

## Phases

### Phase 1 — Compose nested lists
- Work: `compose._blocks` builds the list tree from indentation; tests for AC1–AC5.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC9
- **Data model impact**: none
- **DoD**: `uv run pytest tests/test_compose.py` green; preview checked in a test `miniteams web`.

### Phase 2 — Edit nested lists
- Work: `decompose` keeps sublists in list items and writes them back indented; spec 019 AC9 text and
  README updated.
- **Covers**: AC6, AC7, AC8
- **Data model impact**: none
- **DoD**: `uv run pytest tests/test_decompose.py` green; the archived message from the user's
  screenshot opens in the editor of a test `miniteams web`.

### Phase 3 — List continuation
- Work: one `ui.js` helper, wired in the chat composer and the message editor.
- **Covers**: AC11, AC12, AC13
- **Data model impact**: none
- **DoD**: each AC exercised in a headless browser on a test `miniteams web`; both themes screenshot.

### Phase 4 — Enter in lists, line breaks in items
- Work: Enter continues inside a list and sends only outside one; Shift+Enter breaks the line inside an
  item; compose writes `<br>` for a `\` continuation; decompose reads it back.
- **Covers**: AC11, AC12, AC13, AC14, AC15, AC16, AC17, AC18
- **Data model impact**: none
- **DoD**: compose/decompose tests and browser tests (Chrome and Firefox) green; archived list items
  with a `<br>` counted before/after.

### Phase 5 — Live check
- Work: send AC1's and AC3's lists to the "miniteams" test chat, edit one, compare in Teams web.
- **Covers**: AC10
- **Data model impact**: none
- **DoD**: Teams web shows the same tree as miniteams, before and after the edit.

## Open questions

- (none)

## Decisions

- Relative indentation, not a fixed width: any deeper indent nests one level, so 2, 3 or 4 spaces
  all work. The edit flow writes 2 spaces per level.
- Indent = number of leading spaces and tabs, a tab counting as one: no column arithmetic.
- An indent jump of several levels nests one level only, as Markdown does.
- AC4 changes today's behaviour: `  - x` alone is a plain line now, a list item after this spec.
- A backslash after the indent keeps an indented list marker literal (`  \- x`); AC5's plain indented
  line excepts it.
- Enter continues a list and Shift+Enter breaks the line inside an item, as Teams does (user's call,
  2026-11-11); continuation lines use CommonMark's `\` hard break.
