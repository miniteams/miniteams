# 012 — Composer: emoji completion and `@` mentions

**Status**: implemented
**Requested by**: babs
**Date**: 2026-10-09

**Amendment 2026-10-13**: phase 5 (clipboard images) validated after review (5a protocol proof, then 5b UI); phases 1–4 implemented.

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
  ~450 entries, GitHub/Slack names: `smile`, `thumbsup`, `fire`, `eyes`, `tada`, `+1` alias…).
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
- **Shortened mentions.** In either composer, Backspace immediately after a picked
  full-name mention, including its inserted trailing space, reduces `@First Last ` to `@First `
  while retaining the selected person's identity. The server validates the shortened label against
  that person's roster name; the sent message remains a real Teams mention.
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

- [x] **AC1** `complete emoji` with `q: "fi"` returns `fire` (unicode) and any custom emoji whose
      name starts with `fi`, at most 12 items, case-insensitive; an unknown prefix returns `[]`.
- [x] **AC2** `complete member` returns the thread's human members matching the prefix on first,
      last or full name with accents folded, me excluded, `everyone` first on groups and absent on
      1:1 chats; a thread not on the board or in the archive is refused with an `error`.
- [x] **AC3** `compose_html("ok :fire: :nope:", [], table)` yields `ok 🔥 :nope:`; a custom name
      yields the `CustomEmoji` `<img>` with the right `itemid` and `src`; escaping of the rest is
      unchanged (`<` → `&lt;`, newline → `<br>`).
- [x] **AC4** `compose_html("@Ann Lee and @Ann Lee", [{Ann Lee, mri}], …)` yields two Mention spans
      with `itemid` 0 and 1 and a `mentions` property with two entries in that order; a picked
      mention absent from the text yields no entry.
- [x] **AC5** `@everyone` picked on a group yields a span reading `Everyone` and a `mentionType:
      "everyone"` entry whose `mri` is the thread id.
- [x] **AC6** `send` with a mention whose `mri` is not in the thread roster answers
      `error: "unknown mention"` and calls no API.
- [x] **AC7** `send_message(..., properties=p)` puts `p` in the request body; without it the body
      is byte-identical to today's.
- [x] **AC8** In the page: `:fi` opens the popup, Enter inserts `🔥` and does not send; `@` opens
      the member list, Enter inserts `@Full Name` and records the pick; Escape closes the popup
      and keeps the text; Enter with the popup closed still sends.
- [x] **AC9** `GET /emoji/<id>` serves the cached image for a known custom emoji, 404 for an
      unknown id or a path escaping the cache directory.
- [ ] **AC10** (wire shape checked live on a throwaway group, 2026-10-09: two Mention spans,
      `properties.mentions` person + everyone, CustomEmoji image; Teams-side rendering and the
      notification await the user's look) Live: a message sent from the widget with a picked member shows in Teams with the
      name highlighted and the person's activity feed shows the mention; a custom emoji renders in
      Teams as the emoji, not as text.
- [x] **AC11** `compose_html` with `inline` turns `**a** *b* _c_ ~~d~~ `e` [f](https://x)` into
      `<b>`, `<i>`, `<i>`, `<s>`, `<code>`, `<a href>`; with `inline`, a line `> q` or `- item`
      stays text.
- [x] **AC12** With `full`, a fenced block becomes `<pre><code>` with its content escaped and no
      emoji or mention expanded inside; `> ` lines become a `<blockquote>`; `- ` and `1. ` runs
      become `<ul>` / `<ol>`.
- [x] **AC13** Unclosed or intra-word markers (`2*3*4`, `snake_case_name`, a lone `**`) stay as
      typed; `[x](javascript:…)` stays text; `<b>` typed by hand is escaped.
- [x] **AC14** In the chat view, the toolbar wraps the selection in the right markers, and the
      preview shows the server's HTML without sending; the widget composer has no toolbar.
- [x] **AC15** In both composers, picking Ann Lee and pressing Backspace immediately after
      `@Ann Lee ` changes it to `@Ann ` and retains Ann Lee as the recipient. Backspace elsewhere
      remains ordinary deletion; everyone and single-word names are not shortened.
- [ ] **AC16** A shortened mention displays Ann in the sent Teams message, keeps the selected
      member's original identifier, and remains highlighted. The server accepts only that member's
      complete name or first name as the label and still rejects recipients outside the roster.
      (Generated Mention span, display name, recipient and roster validation tested; Teams-side
      highlight awaits the user's look.)
- [x] **AC17** Two members sharing a first name retain their respective identities when their
      picked mentions are shortened.
- [x] **AC18** Restoring a draft retains shortened mentions and their selected recipients.

### Clipboard images (phase 5)

- [ ] **AC19** In either composer, pasting image files adds local thumbnails to the current
      chat's draft. Pasting does not send a message or upload the images to Teams.
- [ ] **AC20** Several images in one paste and repeated pastes accumulate in order. A user can
      open each thumbnail in a larger local preview and remove each pending image independently
      during composition. Closing the preview preserves the image and text; deleting the
      previewed image closes its preview. Ordinary text paste still works.
- [ ] **AC21** The user can write or edit text and mentions after pasting, then explicitly send
      the queued images with that text, or send images alone. Enter follows the existing
      completion and send rules. Pending images cannot be silently omitted.
- [ ] **AC22** Pending images stay attached to their original chat when switching chats in the
      same page. A successful send clears only its submitted draft; new edits remain intact.
- [ ] **AC23** Upload or send failure retains the text and pending images, displays an actionable
      error and reuses completed uploads on retry. An ambiguous send result must not trigger
      an automatic duplicate send; a manual retry sends only if the chat's recent messages do not
      already hold that `clientmessageid`. Sending twice concurrently is blocked.
- [ ] **AC24** The upload route requires the existing authenticated session and same-origin
      protections. The server validates image content, destination, count and byte limits;
      it never trusts a MIME label, filename or browser-supplied remote URL alone.
- [ ] **AC25** Unsupported or oversized images produce a clear error while preserving the
      draft. Thumbnails and object URLs are released on removal, success and page teardown.
- [x] **AC26** A controlled live send shows every pasted image in native Teams with the
      accompanying text. Any protocol uncertainty is resolved before claiming this phase shipped.

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

### Phase 4 — shorten picked mentions (implemented, approved 2026-10-13)
- Work: handle Backspace for picked mentions, preserve shortened labels in drafts and sending,
  and validate those labels against the existing roster.
- **Covers**: AC15, AC16, AC17, AC18
- **Data model impact**: shortened mentions reuse the existing `name` field in mention payloads
  and session drafts; no schema change, migration or additional directory storage.
- **DoD**: browser tests cover both composers, first-name collisions and draft restoration;
  server tests cover shortened rendering and validation; `pre-commit run --all-files` rc=0.

### Phase 5a — image send protocol proof

- Work: `miniteams send --image <file>…` (CLI): create the AMS object (`POST
  https://<asyncgw host>/v1/objects/` with `{"type": "pish/image", "permissions": {"<thread>":
  ["read"]}, "filename": …, "sharingMode": "Inline"}`, skype token), upload the bytes (`PUT
  …/v1/objects/<id>/content/imgpsh`), then send one message: text first, then the images in order
  (`<p>text</p><p><img …></p>`), each `<img>` with `alt`, `height`, `id`, `itemid`, `itemscope`,
  `itemtype="http://schema.skype.com/AMSImage"`, `src="https://<asyncgw host>/v1/objects/<id>/views/imgo"`
  and `width` (natural pixel size). `send_message` takes the `clientmessageid` as a parameter.
- **Covers**: AC26 (first), the server half of AC24.
- **Data model impact**: none.
- **DoD**: two images and text sent in one message to the throwaway 1:1 test chat show in native
  Teams; a repeated `clientmessageid` is checked for Teams-side dedup and the result recorded here.
- **Live proof 2026-10-13** (`send_images` from a scratch script, code after review round 1,
  silent auth, throwaway 1:1 test chat, one message "miniteams image test 2" with a generated 64x48 PNG and an 80x40 JPEG carrying
  EXIF Orientation=6): object create, `imgpsh` upload and send all answered 201 on `fr-prod`.
  - Sent: `<p>text</p><p><img alt="image" height id="<oid>" itemid="<oid>" itemscope="<png|jpeg>"
    itemtype=AMSImage src="https://fr-prod…/v1/objects/<oid>/views/imgo" width>…</p>`, images in order.
  - Stored (read back): same, with a newline between the paragraphs and `id` turned into `x_<oid>`
    by the service (`itemid` stays bare); a first run that sent `x_<oid>` came back as `x_x_<oid>`.
  - Rotated JPEG: sent as width 40, height 80 (the displayed size); its `views/imgo` is 40x80 with
    no EXIF left, so Teams bakes the rotation in. Both `views/imgo` answer 200 with JPEG bytes
    (Teams re-encodes the PNG too, same size).
  - Final code after review round 2: one message "miniteams image test 3" with the same two
    images sent the same way to the "test miniteams" group chat; read back identical in shape
    (bare `id` sent, `x_` stored, PNG 64x48, rotated JPEG 40x80, both `views/imgo` 200).
  - Real CLI path (`miniteams send TEXT --thread … --image … --image …`, final code): "miniteams image
    test 4 (CLI)" to the "test miniteams" chat, two uploads then one send, 201.
  - On-screen check in the native client (babs, 2026-10-13, "test miniteams" chat): both images shown,
    the green PNG and the orange JPEG, in one message with the text.
- **Dedup 2026-10-13**: no. The same `clientmessageid` sent twice gave two 201s and two distinct
  messages; a retry with a reused `clientmessageid` can duplicate, so 5b must never retry blindly.

### Phase 5b — clipboard queue in both composers

- Work: paste queue, thumbnails (compact 32 px in the widget), enlarged preview, removal, per-chat
  isolation; send = one JSON frame then one binary WebSocket frame per image, in order (socket
  `max_size` ~11 MiB); upload to Teams only at send; a per-draft cache (image digest → AMS object
  id) reuses objects on retry; an ambiguous send shows a spinner and waits for the self-echo (same
  `clientmessageid`, ~15 s), never retries on its own; a manual retry first looks for that
  `clientmessageid` in the chat's recent messages (server side, read-only) and sends, with the same
  id, only if it is absent.
- **Covers**: AC19–AC25.
- **Data model impact**: none; image blobs stay in browser memory until sent.
- **DoD**: browser tests cover repeated/multiple paste, image-only send, removal, per-chat isolation,
  ambiguous send and retry; server tests cover validation (magic bytes, limits, dimensions) and
  failed uploads; pre-commit passes.

## Data model impact (summary, including proposed phase 5)

No database migration. Custom emoji images cached under `~/.cache/miniteams/emojis/` (existing `emojis.py` cache).
Phase 4 reuses the existing `name` field for shortened labels; payload and draft schemas stay the same.

## Open questions

None.

## Decisions

- Phase 5 is an amendment to this composer spec, rather than a separate spec 014.
- Clipboard attachments cover images; arbitrary document upload and drag-and-drop are outside
  this amendment. Multiple clipboard image files and repeated Ctrl+V are supported.
- Pending image blobs are not persisted across a page reload in this iteration. Existing text
  draft persistence remains; the attachment queue explicitly indicates its temporary lifetime.
- Initial local bounds are 8 images and 10 MiB per image, with a 40 MiB total per draft. These
  are application limits, not claimed Teams limits, and can be revised before implementation.
- No Microsoft Graph API is introduced for image upload.
- Phase 5 is split: 5a proves the protocol from the CLI before 5b builds the UI (babs, 2026-10-13).
- Several pasted images go in one message, as the native client does (724 multi-image messages in
  the archive) (babs, 2026-10-13).
- One host for the three calls and the `src`: `MINITEAMS_ASYNCGW_HOST` (`fr-prod` by default), what
  the native client uses here, not purple-teams' hard-coded `as-prod`/`as-api` (babs, 2026-10-13).
- Text first, then the images in paste order (babs, 2026-10-13).
- The browser measures natural width/height; the server checks magic bytes (PNG, JPEG, GIF incl.
  animated, WebP), byte and count limits and integer dimensions 1–100000; no Pillow (babs, 2026-10-13).
- Images travel as binary WebSocket frames after the JSON send frame; nothing reaches Teams before
  send (babs, 2026-10-13).
- Ambiguous sends: spinner, self-echo decides, no automatic retry. Teams does not dedupe a repeated
  `clientmessageid`, so a manual retry first checks the chat's recent messages for it (server side,
  read-only) and sends, reusing the id, only if it is absent (babs, 2026-10-13, after the 5a dedup
  proof).
- Both composers, the widget in a compact layout; may be switched off from usage (babs, 2026-10-13).

- Shortened mentions use the first whitespace-separated word of the existing display name.
  Highlighting remains in the sent message; the textarea stays plain text.
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
- Built-in shortcode table shipped in the repo rather than a dependency: ~450 names cover what
  gets typed; the full Unicode set is not worth a package.
- Unknown `:name:` left as typed: `:)` and times like `10:30:` must not vanish.
- Custom emoji image served through `/emoji/<id>` from the local cache, not the asyncgw URL: that
  URL needs the skype token, which stays off the page (same rule as spec 011 media).
- Completion for members comes from the server roster, not from the page's row data: rows only
  carry a label, and the roster is already cached by `Directory`.
- Known limits: namesake mentions (two members sharing a name) are paired with the `@Name` occurrences by
  count, best effort: of the picks under one name the last k count, k being its `@Name`s in the text; the
  page does not track positions. The server matches `@Name` on word boundaries only.
