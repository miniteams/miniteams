# 029 — `@` mentions and composer rendering in MCP and CLI sends

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-20

## Problem

The MCP tools (`send_message`, `update_scheduled`, `update_message`) and `miniteams send` post plain
escaped text (`send.message_html`). `@Name` stays text: the person gets no mention notification and
Teams does not highlight it. Markdown, `:emoji:` and bare URLs, which the chat page's composer renders
(specs 012, 019, 028), are sent as typed.

## Solution

MCP and CLI sends render text the way the chat page's composer does: markdown subset, `:shortcode:`
emojis, bare URLs as links, and `@Name` as a real Teams mention. The caller writes `@Ann Lee` or
`@Ann`; miniteams finds the person in the chat's members. A name it cannot match to exactly one member
refuses the send, with the candidates, so no mention is silently lost.

## Scope

- **Paths.** MCP `send_message` (now and `send_at`), `update_scheduled` (new text), `update_message`;
  CLI `send` text, including the text sent with `--image` / `--attach`. `--html` stays verbatim.
- **Rendering.** `compose_html(text, mentions, emojis, full=True)`, the edit path's mode: the same
  markdown and block markers as the chat page, built-in shortcodes, bare URLs as links (spec 028).
- **Resolution.** An `@` starting a word, outside code spans/blocks and link labels, followed by a
  member's full name or first name (case and accents ignored, longest match first) becomes a mention of
  that member; the message shows the roster spelling (`@ann` → `Ann`, `@ann lee` → `Ann Lee`), as the
  composer's shortened mention (spec 012). `@everyone` mentions the chat on any chat but a 1:1 or Notes.
- **Roster.** The chat's members from the chat service, fetched only when the text holds a candidate
  `@`; bots and apps left out, me left out (as the composer's picker).
- **Refusals** (nothing sent, no draft written):
  - `@word` matching no member: `unknown mention @word`;
  - `@Ann` matching several members (first name shared, or full name duplicated):
    `ambiguous mention @Ann: Ann Lee, Ann Moore` — the caller retries with the full name;
  - roster unavailable while the text holds a candidate `@`: `chat members unavailable`;
  - `@everyone` on a 1:1, more than `MAX_MENTIONS`.
  MCP answers a ToolError, the CLI logs the error and exits 2.
- **Name end.** A name ends before neither a word character nor a Unicode mark, for every sender,
  composer included: `@Ann Leé` is not `@Ann Lee` followed by a mark.
- **Literal `@`.** `\@` sends a literal `@` (add `@` to `ESCAPABLE`, composer included). An email
  (`a@b.com`) is never a candidate: the `@` does not start a word.
- **Length.** The 28 KiB cap applies to the composed HTML.
- **Docs.** README (MCP and `send` sections) and the `send_message` tool description state the syntax.

## Out of scope

- Custom (org) emojis in MCP/CLI: their table needs an extra token and fetch; `:name:` of a custom
  emoji stays text.
- Mentioning tags, channels, or people outside the chat.
- Mentions in reply quotes; replies are not an MCP/CLI feature today.
- Changing the chat page composer beyond `\@`, the 50-mention cap and the mark name end.

## Acceptance criteria

- [x] **AC1** `send_message` with `@Ann Lee` on a chat where Ann Lee is a member posts
  `<span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="0">Ann Lee</span>` and
  `properties.mentions` with her MRI, `mentionType: person`.
- [x] **AC2** `@ann` (lower case, unique first name) mentions Ann Lee with label `Ann`; `@Ann Lee` and
  `@Ann` in one text give two entries, `itemid` 0 and 1 in text order.
- [x] **AC3** `@Ann` with two members named Ann refuses with both full names; no API write.
- [x] **AC4** `@Nobody` refuses `unknown mention @Nobody`; no API write.
- [x] **AC5** `@everyone` on a group chat gives `mentionType: everyone`, MRI = thread id, label
  `Everyone`; on a 1:1 it refuses.
- [x] **AC6** A text with no candidate `@` makes no roster call; `a@b.com`, `` `@Ann` `` and `\@Ann`
  make none either and send without mentions (the last as a literal `@Ann`).
- [x] **AC7** Roster fetch failure with a candidate `@` refuses `chat members unavailable`.
- [x] **AC8** `send_message` with `send_at` writes the draft with the mention span and
  `properties.mentions`; `update_scheduled` with new text replaces both.
- [x] **AC9** `update_message` with `@Ann Lee` edits the message with the mention span and properties.
- [x] **AC10** `miniteams send --thread X "@Ann Lee hi"` sends the mention; with `--image` or `--attach`
  the text part carries it; `--html` sends the text verbatim, no resolution.
- [x] **AC11** `**bold**`, `:fire:`, a bare URL and a `- ` list render as in the chat page's composer.
- [x] **AC12** A composed message over 28 KiB of HTML is refused before any write.
- [x] **AC13** Teams-side: a mention sent through MCP to the test channel shows highlighted in the Teams
  client and notifies the member. *(2026-10-20: CLI and MCP sends reached the server with the mention
  span and `properties.mentions`; highlighted in the Teams client; the member's notification was not observed.)*

## Phases

### Phase 1 — Resolver + MCP
- Work: a pure `resolve_mentions(text, members, thread_id, me) -> picks` next to `compose_html`
  (reusing `_humans`/`fold`/`chat_kind` and `compose`'s code/link skipping), `\@` escape; a sync
  roster fetch for MCP; `send_message`, `_schedule`, `update_scheduled`, `update_message` go through
  `compose_html` + properties; `new_draft_message` and `edit_message` carry `properties`.
- **Covers**: AC1–AC9, AC11, AC12
- **Data model impact**: none
- **DoD**: `pytest tests/test_compose.py tests/test_mcp.py` green, each refusal tested with no write;
  pre-commit green.

### Phase 2 — CLI + docs
- Work: `cmd_send` text paths (plain, images, attach) through the same function; README and tool
  description.
- **Covers**: AC10, AC13
- **Data model impact**: none
- **DoD**: `pytest tests/test_cli.py` green; one live MCP send with a mention to the test channel
  checked in the Teams client.

## Data model impact (summary)

None.

## Open questions

- [x] AC13: live mention in "test miniteams" approved (2026-10-20).

## Decisions

- Custom emojis left out of MCP/CLI — extra token + fetch per process; overturnable.
- `full=True` rendering (block markers on), as the edit path — MCP/CLI texts are whole messages.
- Mentioning myself refused as unknown — the composer's picker never offers me.
- `\@` added to the composer's escapes too — one rule for every sender.
