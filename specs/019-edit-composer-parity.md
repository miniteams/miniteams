# 019 — Editing a message works like composing one

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-16

## Problem

Editing one of your messages in the chat page is a bare text box. There is no `:emoji` or `@mention`
completion and no formatting; the box is pre-filled with the message's plain text, so saving drops
its bold, italic, links and mentions, and drops its reply quotes. The edit then goes out as plain
text: markdown typed in it is posted literally.

## Solution

The editor behaves like the composer: completion, formatting keys and a small formatting bar with a
preview, and the saved message is composed exactly like a sent one. It opens with the message's own
formatting, mentions, reply quotes and pasted images, so an untouched part stays as it was. A message
miniteams cannot reproduce is not editable here, and says why.

## Scope

- **Editor.** `:emoji` and `@mention` completion, Ctrl+B / Ctrl+I, and a mini formatting bar under
  the editor (the composer's buttons) with a 👁 preview toggle. The bar and the preview are one
  component in `ui.js`, shared with the composer.
- **Prefill.** The message's HTML converted back to the composer's markdown: bold, italic, strike,
  inline code, links and bare URLs, emojis (`:shortcode:` for custom ones), mentions (restored as
  picked), fenced code, `> ` quotes, flat lists, line breaks. Characters that would otherwise be read
  as markdown are escaped with a backslash.
- **Backslash escapes.** Composition reads `\*`, `\_`, `\~`, `` \` ``, `\[`, `\\` and a leading `\`
  before `- `, `* `, `> `, `1. ` as literal characters, in the composer too.
- **Reply quotes.** Shown above the editor as the composer shows its quotes, kept as they were on
  save, each removable with ×; none can be added.
- **Pasted images.** When every image of the message comes after its text (how miniteams posts
  them), they show as thumbnails under the editor, kept as they were on save, each removable with ×;
  none can be added.
- **Mentions.** A mention already in the message is kept as is, even if that person has left the
  chat (the server checks it against its copy of the message); a new mention is checked against the
  roster like in a send.
- **Not editable here.** A message holding anything else (a table, a heading, a nested list,
  underline, colour, a card, an image inside the text, a tag or team mention) keeps ✎ but opening it
  says what blocks it and offers "Open in Teams".
- **Edited elsewhere.** When the message changes in Teams while it is being edited, a note under the
  editor says so with the time and a "take that version" link that reloads the editor (text, quotes,
  images); saving stays possible and replaces it.
- **No change, no edit.** Enter with text, quotes and images untouched closes the editor without
  sending (Teams would mark the message edited).
- **Opening.** Opening the editor asks the server for the message's editable form: its markdown,
  picked mentions, quotes and trailing images, or the reason it cannot be edited here.
- **Server.** The `edit` verb carries the mentions and which quotes and images are kept; the server
  rebuilds the message from its own copy (kept quotes, composed text, kept images) through the same
  composition as a send, and refuses an edit of a message it cannot reproduce.

## Out of scope

- Editing from the widget (its quick reply only sends).
- Adding quotes or images while editing.
- Editing what miniteams cannot reproduce (tables, headings, nested lists, cards, …): Teams does it.
- Keyboard navigation to reach a message (spec 018).

## Acceptance criteria

- [x] **AC1** In the editor, `:` and `@` open the same completion as the composer, and a picked
  mention is posted as a mention.
- [x] **AC2** In the editor, Ctrl+B / Ctrl+I and the mini bar's buttons format the selection; 👁
  toggles a preview rendered like the composer's.
- [x] **AC3** Saving an edit posts HTML composed exactly as a send of the same text would be:
  markdown rendered, `:shortcode:` emojis expanded, mentions as mentions.
- [x] **AC4** Opening the editor on a message posted by miniteams and saving it unchanged in text
  gives back the same rendering: every element listed under Prefill round-trips.
- [x] **AC5** Characters typed literally (`2*3*4`, `[draft]`, a line starting `- `) survive an edit
  untouched; in the composer, `\*` posts a literal `*`.
- [x] **AC6** A reply's quotes show above the editor and are kept on save; × removes one, and the
  saved message no longer carries it.
- [x] **AC7** Images placed after the text show under the editor and are kept on save; × removes one,
  and the saved message no longer carries it.
- [x] **AC8** A mention of someone who has left the chat, already in the message, survives an edit; a
  new mention of a non-member is refused like in a send; a mention not in the original copy but
  presented as kept is refused.
- [x] **AC9** A message holding a table, a heading, a nested list, underline, a card, an image inside
  the text or a tag mention cannot be edited here: ✎ says which and offers "Open in Teams", and the
  server refuses such an edit on its own.
- [x] **AC10** A change of the message from elsewhere while it is edited shows "edited elsewhere at
  HH:MM" with "take that version", which reloads text, quotes and images; saving still replaces it.
- [x] **AC11** Enter with nothing changed closes the editor and sends nothing.
- [x] **AC12** Every text from the message (prefill, quotes, image alt, the blocking reason) is
  escaped: markup in a message never renders in the editor.

## Phases

### Phase 1 — Composition and the edit verb (server)
- Work: backslash escapes in `compose_html`; HTML → markdown conversion of a message with its
  "reproducible or not" verdict, served by a new page verb that opens an edit; `edit` verb with mentions and kept quotes / images, rebuilt from the
  server's copy through `compose_html`; refusal of a non-reproducible message and of a forged kept
  mention.
- **Covers**: AC3, AC5, AC8, AC9 (server side)
- **Data model impact**: none
- **DoD**: unit tests for escapes, the conversion round-trip of every listed element, the verdict on
  each blocking element, and the verb's validation; full suite and pre-commit green.

### Phase 2 — The editor (page)
- Work: shared formatting bar + preview component in `ui.js`; editor with completion, keys, bar,
  preview, quotes and images with ×, prefill from the conversion, blocked message with its reason
  and "Open in Teams", edited-elsewhere note, no-change close.
- **Covers**: AC1, AC2, AC4, AC6, AC7, AC9 (page side), AC10, AC11, AC12
- **Data model impact**: none
- **DoD**: browser tests for each AC, in Chrome and Firefox for the editor's typing and preview; full
  suite and pre-commit green.

## Data model impact (summary)

None. The `edit` verb gains fields (mentions, kept quotes, kept images); a new page verb returns a
message's editable form.

## Open questions

None.

## Decisions

- "Reopen" after a refusal keeps a text typed since opening, with its picks, and takes the copy's
  current quotes and images; an untouched text takes the copy's — because the refusal (a kept
  quote or image gone) says nothing against what was typed.
  A reopen answered "not editable here" keeps a typed text read-only, to copy, with the reason and
  "Open in Teams"; Escape closes it.

- A message holding the same quote or the same image twice is not editable here — because kept items
  are named by key, so one of the two could not be removed alone.

- `<p></p>` and a paragraph's last `<br>` are no line; `<p><br></p>` and `<p>&nbsp;</p>` are one
  blank line each — because an empty block renders nothing (887 editable archived messages hold one,
  mostly editor leftovers between paragraphs or after code).

- Markup with no visible effect reads as transparent: a span whose only attribute is
  `style="font-size:inherit"`, the Teams client's `<div itemprop="copy-paste-block">` paste wrapper,
  layout whitespace before a line's first text; Teams-client links (`itemtype` exactly
  `…/HyperLink`) become plain links on save — because a sample of the user's own messages showed
  these on otherwise editable messages (268 → 306 of 317 editable; the rest are out of scope).

- An edit's `properties` (mentions) are merged by Teams, not replaced — verified live on the test chat
  (2026-10-16): reactions survive an edit with mentions, and an edit without any drops the old ones.
- A Teams-client message's `<p>`/`<div>` paragraphs read as lines; saving it turns paragraph breaks
  into line breaks — because otherwise most messages written in Teams could not be edited here.
- `strong`/`em`/`del` read as bold/italic/strike, an animated emoji as its unicode character (it
  loses the animation on save), a link whose text is its URL as a bare URL.

- Grill answers, 2026-10-16: non-reproducible message → not editable, with the reason and "Open in
  Teams" (Q1); trailing images kept and removable (Q2); reply quotes kept and removable (Q3);
  original mentions kept without the roster check, verified against the server's copy (Q4);
  backslash escapes added to composition, the composer included (Q5); edited elsewhere → note +
  "take that version", save still allowed (Q6); a mini bar and preview per editor (Q7); widget out
  of scope (Q8); no change → no edit (Q9).
- The conversion runs on the server, next to `compose_html` — because both sides of the round-trip
  live there and are unit-testable, and the server has to judge "reproducible" anyway to refuse a
  forged edit.
- Kept quotes and images are named by a stable key from the server's copy (a quote by its Reply
  block's `itemid`, an image by its AMS object id), never sent as HTML by the page — because the page
  must not inject markup, and positions would point at other blocks after an edit elsewhere; a key the
  copy no longer holds is refused ("the message changed since it was opened: reopen it").
- Line-start escapes (`\- `, `\> `, `\1. `) apply in the chat page's full format only; the widget's
  inline format has no block markers, so a leading `\- ` there posts as typed. `\\` collapses only
  before an escapable character or a line-start marker, so `\\srv\share` posts as typed.
  A line that is only `\>` keeps its `>` literal (a bare `>` before a `> ` line would open a quote);
  backslashes inside `code`, link labels and URLs stay as typed.
- A code block carrying its language (`<pre class="language-…">`) is refused: composition cannot
  write the language.
