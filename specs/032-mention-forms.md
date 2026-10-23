# 032 — Mention by first name, last name or a free label

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-22

## Problem

In the chat page's composer, picking `@John` inserts `@John Doe`. Only one Backspace pressed
right after the full name shortens it to `@John` and keeps the mention (spec 012). Removing the last
name any other way (selection, Ctrl+Backspace, an edit in the middle) loses the pick, and `@John`
leaves as plain text without notifying anyone. Nobody can be mentioned by last name alone, in the
composer, MCP or the CLI (spec 029). A mention cannot read anything but a name, although Teams shows any
label: a probe on 2026-10-22, `le testeur` pointing to a member, was highlighted and pointed to them.
In the chat page a mention reads in bold, with no way to tell who it names.

## Solution

Every sender (composer, MCP, CLI) accepts three forms of a member's name: `@First`, `@Last` and
`@First Last`. `@{First Last|label}` mentions that member under any label. In the composer a pick stays
on the `@` it was inserted at: whatever the edit, that `@` names the picked person while what follows it
is still one of their forms. Hovering a mention in the chat page shows the full name of the person it
names.

## Scope

- **Name forms.** A member matches by full name, first name (first word) or last name (last word),
  case and accents ignored, longest match first, with the name end of spec 029. The message shows the
  form as spelled in the roster (`@john` → `John`). MCP and CLI refuse an ambiguous or unknown form
  as in spec 029 (`ambiguous mention @Lee: Ann Lee, Bob Lee`).
- **Free label.** `@{Who|label}`: `Who` is resolved like a name form (full, first or last, so it can
  disambiguate with the full name), and the mention displays `label` as typed. The last `|` splits them,
  so `Who` may hold a `|` (`@{John Doe | Sales|boss}`). The whole `@{Who|label}` is one token read raw
  before markdown, up to its first `}` (user decision, 2026-10-23): markup characters and URLs in it are
  text, `@` too. `label` is one line of text, not empty, at most MAX_NAME characters: no `|` or `}`, no tab or other blank but the space, no control, format, private-use or unassigned character
  (refused with that rule). Who may hold anything but `}` and a line break, and must name one member
  whole. `\@` keeps the text literal. In the composer, `@{` opens the member list on Who; a pick inserts
  `@{Full Name|Full Name}`, label selected (plain `@Full Name` when the label rule refuses the name).
- **Composer.** Each pick is anchored at the `@` it was inserted at and travels with that offset. The
  `@` is the picked person while what follows it is one of their forms (full, first, last, or a
  `{Who|label}` whose Who is one), whatever the edits that produced it, so the Backspace shortening is no
  longer the only way; otherwise the pick is dropped and the `@` reads as unpicked. An edit that removes
  or replaces the `@` drops its pick, never moves it to another `@`. An `@form` with no pick resolves on
  the roster like MCP; when it is ambiguous or unknown, the send is refused with the reason and the text
  stays in the composer. Namesake picks keep their own recipient.
- **Edit round trip.** Editing a sent message gives back every person mention braced (user decision,
  2026-10-23): `@{Full Name|label}` with the name from the chat's roster, `@{label|label}` when it is
  unknown (no roster, or they left); `@everyone` stays `@everyone` (spec 019 decompose). Each kept
  spelling names one person, and no name read on save can extend it, so the page's name-based tracking
  cannot confuse kept and new mentions. A label holding markup characters comes back raw inside its
  brace; a full name a `{…}` cannot hold (a `}` or a line break) gives `@{label|label}`. Not offered for
  editing ("a mention the edit would read as someone else"): a label no `{…}` can hold (`|`, `}`, or a
  character the label rule refuses), or two people still spelled alike (namesakes, no full name known).
  On save, a kept mention given back as spelled, as `{label|label}` or as `{Full Name|label}` (whichever
  roster the editor opened with) stays the message's own; any other spelling is a new pick, checked on
  the roster: `unknown mention`, or `roster unavailable` without one. So a kept `{Full Name|label}`
  opened with the roster and saved without it is refused `roster unavailable`. A brace with no pick (an
  undo, or its Who edited) whose label is a kept mention's is about the kept people so labelled: its Who
  naming one of them on the roster is that one, unless another so labelled has left (it could be them);
  naming nobody (me and apps are somebody), the one so labelled if only one and Who is the label or a form
  of the name the message knows them by; else refused. Without a
  roster, only `{label|label}` names nobody; any other Who is `roster unavailable`. A brace with no kept label
  takes the roster rule. Regression-tested
  shapes (spec 032 review): a longer roster name after a kept label (`, ` `/` ` (` NBSP `-`), a longer
  member picked beside it, namesakes deleted, re-picked or preceded by a new pick, a namesake or a member
  bearing a departed person's name picked then deleted, text typed after a kept mention, the roster down
  at opening or at saving, the page's picks anchored as ui.js keeps them: none sends a kept mention to
  another person.
- **Hover.** In the chat page a mention shows the full name of its person on hover (`title`), from the
  chat's roster or the directory. When the name is unknown, nothing is shown. `@everyone` shows nothing
  extra.
- **Docs.** README (composer, MCP, CLI) and the `send_message` tool description list the forms.

## Out of scope

- A chip-like atomic mention in the composer: the textarea stays plain text.
- Forms other than first, last and full name (middle names, initials, nicknames).
- Mentions of tags, channels, people outside the chat.

## Acceptance criteria

- [ ] **AC1** MCP `send_message` with `@Lee` on a chat with one Lee mentions Ann Lee, label `Lee`;
  `@ann`, `@Ann Lee` and `@lee` each mention her with the roster spelling.
- [ ] **AC2** `@Lee` with two members named Lee refuses with both full names, no write; `@{Ann Lee|Lee}`
  sends a mention of Ann Lee labelled `Lee`.
- [ ] **AC3** `@{Ann Lee|the boss}` sends `<span …>the boss</span>` and a mention entry with Ann's MRI
  and `displayName` `the boss`; `@{John Doe | Sales|boss}` mentions John Doe | Sales; an empty label, one
  over MAX_NAME, or one with a line break or a refused character is refused; the label ends at the first
  `}` (`@{Ann Lee|a}b}` → mention `a` + `b}`), the last `|` splits; `\@{Ann Lee|x}` stays literal.
- [ ] **AC4** CLI `miniteams send` accepts the same forms (AC1–AC3), refusals exit 2 before any write.
- [ ] **AC5** Composer: pick Ann Lee, then turn `@Ann Lee` into `@Ann` or `@Lee` by selection-delete,
  by Ctrl+Backspace or by an edit in the middle: the send carries Ann's mention each time.
- [ ] **AC6** Composer: a typed `@Bob` (no pick) with a single Bob in the chat sends his mention; with
  two Bobs the send is refused with both names, and the text stays in the composer.
- [ ] **AC7** Composer: namesakes Ann Lee and Ann Moore both picked, shortened to `@Ann` and `@Moore`:
  each mention goes to its own person.
- [ ] **AC8** Editing a sent message gives back `@{Ann Lee|Lee}` for a `Lee` label and `@{Ann Lee|the boss}` for
  a free label; re-sending it unchanged yields the same mentions.
- [ ] **AC9** Hovering a mention in the chat page shows its person's full name; a mention whose person is
  unknown shows no title.
- [ ] **AC10** Teams-side: a free-label mention and a last-name mention sent from the composer to the
  test chat are highlighted in the Teams client.

## Phases

### Phase 1 — Server: forms and free label
- Work: `resolve_mentions` gains last name and `@{Who|label}`; `compose_html` renders the free label;
  the web send/edit path resolves an unpicked `@form` on the roster and accepts last-name and free-label
  picks; decompose gives `@{Full Name|label}` back.
- **Covers**: AC1, AC2, AC3, AC4, AC8
- **Data model impact**: none
- **DoD**: `pytest tests/test_compose.py tests/test_decompose.py tests/test_mcp.py tests/test_cli.py`
  green; pre-commit green.

### Phase 2 — Composer and hover
- Work: `ui.js` anchors each pick at its `@` and keeps the anchors current through every edit; the server
  reads each anchored `@` as its pick's person; refusal shown in the composer; hover title on mentions.
- **Covers**: AC5, AC6, AC7, AC9, AC10
- **Data model impact**: none
- **DoD**: browser tests green; one composer send to the test chat checked in the Teams client.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- The free label is typed by hand; the member list only fills Who (user request, 2026-10-23).

- Last name = the name's last word, first name = its first word: no middle-name handling. Overturnable.
- The composer refuses an ambiguous unpicked `@form` instead of sending it as plain text, as MCP does.
- Free label in braces, not `@[Who]{label}`: no look-alike of the `[label](url)` link.
- `@{Who|label}` is read raw before markdown, like a code span (user decision, 2026-10-23): markup
  characters are allowed in a label; `|`, `}`, line breaks and non-printing characters stay refused.
- Full name unknown on edit → `@{label|label}` (user decision, 2026-10-22): only the kept pick reads it,
  so a roster back at save cannot re-read it as someone else.
- Every kept person mention comes back braced (user decision, 2026-10-23), rather than braces only where
  a reading looked uncertain: one spelling per person makes kept and new mentions impossible to confuse,
  so the heuristics that guessed which ones needed a brace, and the save-time checks behind them, went.
- Namesakes with no full name known cannot be told apart, so the message is not offered for editing.
- First and last names lose the punctuation around them (`Doe, John` → `Doe`, `(Sales)` → `Sales`).
- Phase 2 (user decision, review C1): a pick is anchored at its `@`, not read back from names. The page
  sends `{name, mri, at}`, `at` the `@`'s offset in UTF-16 code units (JavaScript's), converted by the
  server; an offset not in the text, inside a character or not an integer refuses the send. The page
  never reads names: it reads no markdown, so it cannot disagree with the server about where a mention is.
- The server reads an anchored `@` only as its pick's person, by the forms of that member's roster name
  (a kept mention also by its own Who and label); the page's name is never trusted. The pick stands down,
  the `@` then reading as unpicked, where another member reads longer there (`@Ann Moore` typed over a
  picked `@Ann Lee`, `@Ann Lee Smith`), or at a brace whose Who names another member whole; namesakes
  reading as far leave it to the pick, and a kept brace given back unchanged stays the message's own,
  whoever its Who names (review H1). A pick at an `@` the
  server does not read as a mention (in code, escaped, a link label) or sharing its `@` with another is
  dropped, never moved: the worst case is a refusal or plain text, never another recipient.
- Anchors follow each change of the text: the old span `[a, b)` became the new `[a, caret)`, so anchors
  before stay, after shift, inside go (review round 2: no text matching). The caret places the change, so
  deleting the first of two identical mentions keeps the second's pick; a paste or typing over the `@`
  always drops it, even an `@` over itself. An input method's composition is placed once, when it commits,
  over the selection it started on (wherever the caret sat in its preedit), the caret at the end of what it
  committed; committed anywhere else, it drops them all. Only a change `beforeinput` announced is placed so; the page's own writes need none, and
  its toolbar wraps and line prefixes (bold, italic, strike, code, list, quote) say what they add and where,
  each anchor shifting by the markers added at or before its `@`, in one undo step. An undo or redo, a change
  nothing announced (a script's `input`, even after a `beforeinput`), or a write no input event reported drops them all; a failed
  message edited in its bubble keeps the picks before its first change. A draft, a failed message taken
  back and an opened edit restore their picks with their anchors.
- A refusal ending with the `\@` hint (the resolver's) goes back into the empty composer, its reason on the
  chat's line; with text already in the composer it stays a failed bubble (spec 017's ✎ brings it back).
- Hover: the full name comes from the directory's names (each chat roster fetched is noted there), as
  `title` on the mention span; an unknown name (the directory's MRI-tail placeholder) or everyone shows none.
