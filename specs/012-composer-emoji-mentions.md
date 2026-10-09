# 012 — Composer: emoji completion and `@` mentions

**Status**: approved
**Requested by**: babs
**Date**: 2026-10-09

## Problem

The composer (spec 006, reused by the chat view of spec 011) sends plain text. No way to drop an
emoji without a system picker, no way to use the org's custom emojis at all, and `@Name` is just
text: the person is not notified, Teams does not highlight it.

## Solution

In any composer, typing `:` then two letters opens a list of matching emojis (standard ones and
the org's custom ones); Enter or Tab inserts the pick. Typing `@` opens the chat's members plus
`@everyone`; picking one inserts the name, and the sent message carries a real Teams mention, so
the person (or everyone) is notified and the name shows highlighted in Teams, as if typed there.

## Scope

- **Emoji table.** `emoji_table.py`: a built-in `shortcode → character` dict (the common set,
  ~300 entries, GitHub/Slack names: `smile`, `thumbsup`, `fire`, `eyes`, `tada`, `+1` alias…).
  Custom emojis come from `emojis.rows()` (spec: `emojis.py`), fetched once at `web` start and
  refreshed every 6 h; failure to fetch leaves only the built-in set and logs a warning.
- **Lookup verb.** `{"complete": "emoji"|"member", "q": "<prefix>", "thread": "<id>"}` answers
  the asking socket `{"complete": …, "items": [...]}` (at most 12). Emoji items:
  `{name, char}` or `{name, custom: id, src: "/emoji/<id>"}`; `GET /emoji/<id>` serves the cached
  custom image (`emojis.py` download cache, `~/.cache/miniteams/emojis/`, fetched on first use,
  404 when unknown). Member items: `{name, mri}` from the thread roster (`Directory` roster,
  members only, bots left out, me left out), plus `{name: "everyone", mri: "<thread>",
  everyone: true}` first on group chats (any size, as the Teams client offers it; never on a 1:1); prefix match on first name,
  last name and full name, accents ignored.
- **Page.** A popup under the caret opens on `:` followed by 2+ `[a-z0-9_+-]` characters, or on
  `@` followed by 0+ characters; ↑/↓ move, Enter/Tab insert, Escape closes, any other key filters.
  Enter with the popup open never sends. Inserting an emoji writes the character (unicode) or
  `:name:` (custom); inserting a member writes `@Full Name` and records `{name, mri}` in the
  composer's picked set for that draft. Typing `:name:` by hand with a known name also works.
- **Send.** The `send` verb gains `mentions: [{name, mri}]` (the picked set, whatever the page
  still finds in the text). Server-side `compose_html(text, mentions, emojis)`:
  - escape, newlines → `<br>` (as today);
  - each `:name:` with a known name → the unicode character, or for a custom emoji
    `<img alt="<name>" itemid="<name>;<id>" itemtype="http://schema.skype.com/CustomEmoji"
    src="https://<asyncgw host>/v1/objects/<id>/views/imgt2_anim" style="width:20px; height:20px">` (the
    shape Teams sends, checked on the archive: bare name in `alt`, 20px);
  - each `@Name` whose `name` is in `mentions` → `<span itemtype="http://schema.skype.com/Mention"
    itemscope itemid="N">Name</span>` (`Everyone` for the everyone entry), `N` counting from 0 in
    text order, every occurrence replaced; `properties.mentions` becomes the JSON string
    `[{"@type": "http://schema.skype.com/Mention", "itemid": N, "mri": …, "mentionType":
    "person"|"everyone", "displayName": …}]`. A listed mention whose `@Name` is not in the text
    is dropped. A mention whose `mri` is not a member of the thread (or, for everyone, not the
    thread id) is refused: `{"sent": id, "error": "unknown mention"}`, no API call.
- **Formatting, markdown-like.** The textarea stays plain text; the server turns a markdown
  subset into Teams HTML before emoji and mentions are expanded. Nothing inside code is expanded.
  - Widget composer, light (`format: "inline"`): `**bold**`, `*italic*` / `_italic_`,
    `~~strike~~`, `` `code` ``, `[text](https://…)`. Bare URLs left as text, Teams links them.
  - Chat view composer, full (`format: "full"`): the light set plus `` ``` `` code blocks,
    `> ` quote lines, `- ` / `* ` and `1. ` lists, and a small toolbar (B, I, S, `</>`, list,
    quote) that wraps the selection in the markers. A preview toggle shows the rendered HTML
    from the server (verb `{"preview": text, "format": …}` → `{"preview": html}`), no send.
  - The `send` verb gains `format`; missing = `inline`. Markers that do not close stay as typed
    (`2*3*4` and `snake_case_name` stay text: `_` only opens at a word boundary).
  - Only `http(s)` links become `<a>`; anything else stays text.
  - Edits from the chat view (spec 011) stay plain text.
- **`send_message`** gains `properties: dict | None`, sent as-is in the body when given. CLI
  `send` unchanged.
- **Widget rows and chat view** already render mentions (specs 003, 011); the sent message shows
  through the normal echo.
- **Docs.** README composer sentence: `:` and `@` completion.

## Out of scope

- Emoji picker grid, skin tones, recently used, reactions from the composer.
- Mentions in the CLI `send` or in the MCP `send_message` tool.
- Channel/team/tag mentions (not in chats), bot mentions.
- WYSIWYG editing (contenteditable), colours, font sizes, headings, tables.
- Highlighting mentions inside the textarea while typing (plain `@Name` text until sent).

## Acceptance criteria

- [ ] **AC1** `complete emoji` with `q: "fi"` returns `fire` (unicode) and any custom emoji whose
      name starts with `fi`, at most 12 items, case-insensitive; an unknown prefix returns `[]`.
- [ ] **AC2** `complete member` returns the thread's human members matching the prefix on first,
      last or full name with accents folded, me excluded, `everyone` first on groups and absent on
      1:1 chats; a thread not on the board or in the archive is refused with an `error`.
- [ ] **AC3** `compose_html("ok :fire: :nope:", [], table)` yields `ok 🔥 :nope:`; a custom name
      yields the `CustomEmoji` `<img>` with the right `itemid` and `src`; escaping of the rest is
      unchanged (`<` → `&lt;`, newline → `<br>`).
- [ ] **AC4** `compose_html("@Ann Lee and @Ann Lee", [{Ann Lee, mri}], …)` yields two Mention spans
      with `itemid` 0 and 1 and a `mentions` property with two entries in that order; a picked
      mention absent from the text yields no entry.
- [ ] **AC5** `@everyone` picked on a group yields a span reading `Everyone` and a `mentionType:
      "everyone"` entry whose `mri` is the thread id.
- [ ] **AC6** `send` with a mention whose `mri` is not in the thread roster answers
      `error: "unknown mention"` and calls no API.
- [ ] **AC7** `send_message(..., properties=p)` puts `p` in the request body; without it the body
      is byte-identical to today's.
- [ ] **AC8** In the page: `:fi` opens the popup, Enter inserts `🔥` and does not send; `@` opens
      the member list, Enter inserts `@Full Name` and records the pick; Escape closes the popup
      and keeps the text; Enter with the popup closed still sends.
- [ ] **AC9** `GET /emoji/<id>` serves the cached image for a known custom emoji, 404 for an
      unknown id or a path escaping the cache directory.
- [ ] **AC10** Live: a message sent from the widget with a picked member shows in Teams with the
      name highlighted and the person's activity feed shows the mention; a custom emoji renders in
      Teams as the emoji, not as text.
- [ ] **AC11** `compose_html` with `inline` turns `**a** *b* _c_ ~~d~~ `e` [f](https://x)` into
      `<b>`, `<i>`, `<i>`, `<s>`, `<code>`, `<a href>`; with `inline`, a line `> q` or `- item`
      stays text.
- [ ] **AC12** With `full`, a fenced block becomes `<pre><code>` with its content escaped and no
      emoji or mention expanded inside; `> ` lines become a `<blockquote>`; `- ` and `1. ` runs
      become `<ul>` / `<ol>`.
- [ ] **AC13** Unclosed or intra-word markers (`2*3*4`, `snake_case_name`, a lone `**`) stay as
      typed; `[x](javascript:…)` stays text; `<b>` typed by hand is escaped.
- [ ] **AC14** In the chat view, the toolbar wraps the selection in the right markers, and the
      preview shows the server's HTML without sending; the widget composer has no toolbar.

## Phases

### Phase 1 — compose and lookup on the server
- Work: `emoji_table.py`, `compose_html()`, `send_message(properties=)`, `complete` verb, roster
  check, `send` verb `mentions` field, `/emoji/<id>` route, custom emoji refresh.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7, AC9
- **Data model impact**: none
- **DoD**: `pytest tests/test_compose.py tests/test_web_complete.py` green with one test per AC
  above (roster and `emojis.rows` mocked); `pre-commit run --all-files` rc=0.

### Phase 2 — popup in both composers
- Work: completion popup shared by `widget.html` and `chat.html` (one JS snippet, served inline in
  both), picked set per draft, `mentions` on send, README line.
- **Covers**: AC8, AC10
- **Data model impact**: none
- **DoD**: on a live `web` with a test `--bind`, a message with `:fire:`, one custom emoji, one
  member mention and one `@everyone` sent to a throwaway group shows in Teams as described in AC10.

### Phase 3 — markdown-like formatting
- Work: `markdown_html(text, full)` (pure, stdlib) called first in `compose_html`; `format` on
  `send`; `preview` verb; toolbar and preview toggle in `chat.html`; README line.
- **Covers**: AC11, AC12, AC13, AC14
- **Data model impact**: none
- **DoD**: `pytest tests/test_compose.py` green with each case above; a message with bold, a link,
  a list and a code block sent from the chat view renders the same in Teams.

## Data model impact (summary)

None. Custom emoji images cached under `~/.cache/miniteams/emojis/` (existing `emojis.py` cache).

## Open questions

None.

## Decisions

- Formatting split by composer (babs, 2026-10-09; WYSIWYG may come later): light inline markdown in the
  widget, full subset + toolbar + preview in the chat view. Converted on the server so both
  composers and the preview share one function.
- `@everyone` offered on every group chat whatever its size, as the Teams client does (babs,
  2026-10-09; first rule was "more than two members"). Listed first on groups only: a 1:1
  has no everyone in Teams.
- Mentions resolved server-side from a `{name, mri}` list the page sends, not from caret offsets:
  offsets drift with every edit, names do not; the roster check stops a page from mentioning an
  MRI outside the chat.
- Custom emojis sent as `:name:` text and expanded on the server: the textarea stays plain text,
  the page needs no contenteditable.
- Built-in shortcode table shipped in the repo rather than a dependency: ~300 names cover what
  gets typed; the full Unicode set is not worth a package.
- Unknown `:name:` left as typed: `:)` and times like `10:30:` must not vanish.
- Custom emoji image served through `/emoji/<id>` from the local cache, not the asyncgw URL: that
  URL needs the skype token, which stays off the page (same rule as spec 011 media).
- Completion for members comes from the server roster, not from the page's row data: rows only
  carry a label, and the roster is already cached by `Directory`.
