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
`@First Last`. `@{First Last|label}` mentions that member under any label. In the composer a picked
mention stays a mention whatever the edit, as long as what follows the `@` is still one of the picked
person's forms. Hovering a mention in the chat page shows the full name of the person it names.

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
  whole. `\@` keeps the text literal. In the composer, `@{` does not open the member list.
- **Composer.** Picks stay the source of who a mention names. When a `@form` matches a form of one
  picked person, it is that pick, whatever the edits that produced it, so the Backspace shortening is no
  longer the only way. An `@form` that matches no pick resolves on the roster like MCP; when it is
  ambiguous or unknown, the send is refused with the reason and the text stays in the composer. Namesake
  picks keep their own recipient.
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
  opened with the roster and saved without it is refused `roster unavailable`. Regression-tested
  shapes (spec 032 review): a longer roster name after a kept label (`, ` `/` ` (` NBSP `-`), a longer
  member picked beside it, namesakes deleted, re-picked or preceded by a new pick, a namesake or a member
  bearing a departed person's name picked then deleted, text typed after a kept mention, the roster down
  at opening or at saving, with the page's own reading (ui.js `scan`, `mentions`, Backspace) simulated:
  none sends a kept mention to another person.
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
- Work: `ui.js` matches picks by form, not by a single shortening gesture; refusal shown in the composer;
  hover title on mentions.
- **Covers**: AC5, AC6, AC7, AC9, AC10
- **Data model impact**: none
- **DoD**: browser tests green; one composer send to the test chat checked in the Teams client.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- The free label is typed by hand, never offered from the member list.

- Last name = the name's last word, first name = its first word: no middle-name handling. Overturnable.
- The composer refuses an ambiguous unpicked `@form` instead of sending it as plain text, as MCP does.
- `@{` does not open the member list: the brace form is typed on purpose.
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
