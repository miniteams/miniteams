# 011 — Chat view in the widget (`/chat/<thread>`)

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-09

**Amendment 2026-10-13**: recipient read receipts are draft; the shipped chat view remains unchanged.

## Problem

The widget (spec 002) is a list of rows; reading a conversation means leaving for Teams. The quick
reply (spec 006) answers one line blind: no history on screen, no way to see what came before or
what landed while typing. Middle click today opens Teams web, a heavy page that reloads its whole
app for one chat.

## Solution

A ⚙ setting, "middle click opens the chat here", off by default. When on, a middle click on a row
opens a new tab on `/chat/<thread>` served by the same `miniteams web` process (a second setting,
`chatnav`, adds a chat picker at `/?nav=chat` and a split list beside the chat): the conversation's
history, newest at the bottom, rendered like Teams (bold, links, lists, quotes, emoji, images from
the archive), followed live, with the same composer as the widget at the bottom. Reply there,
Enter sends. Plain click keeps its behaviour; Ctrl/⌘ click does what middle click does, Shift
click opens Teams web (2026-10-14 decision).

## Scope

- **Setting.** ⚙ menu gains "Middle click: Teams web / chat here" (per browser, `localStorage`
  key `midclick`, default `teams`). With `chat`, `onauxclick` button 1 opens the chat page: when
  `--browser` is set, through it (verb `{"open": id, "chat": true, "chatnav"?: "split"}`; the server
  runs `--browser` on its own `http://<bind>/chat/<encoded id>`), else in a new tab of the page's
  browser (`window.open`, `noopener`).
- **Route.** `GET /chat/<thread>` serves `chat.html` (new static page next to `widget.html`),
  `GET /media/<thread>/<file>` serves a file from `<data-dir>/<thread>/media/` (read-only, path
  confined to that directory, 404 otherwise). `<file>` may be a bare object id (`media_url` before the
  archiver wrote it): the file is found by id at request time, else, with
  `?host=<region>.asyncgw.teams.microsoft.com`, the server fetches the object's view with its own token:
  `&view=` (the view the message's `src` names, `[a-z0-9_]{1,40}`; a pasted GIF's `imgo` is a still), else
  `imgo` (URL rebuilt from host, id and view, image types only, memory only, 4 at a time, 8 s at most; a
  failure is not asked again for 1 min under the same token and view) and 404s when Teams refuses. Several files
  for one id: the `.gif` first (a pasted GIF's animated copy), other originals next, `.full.*` last
  (spec 031). `giphy-<id>?giphy=<host><path>` is a Giphy image: archived as `giphy-<id>.<ext>`, else
  fetched from `https://<host><path>` (allowlisted Giphy host and image path, rebuilt, no token; same
  types, cap, slots, deadline, a failure not asked again for 1 min whatever the token). The thread id
  is taken from the URL path, url-decoded. A thread not on the board and not in the archive gets a 404.
  `GET /gif?url=<https URL>` (cookie only) serves the original of a GIF copied from a web page, whose paste
  carries only a still: path ending in `.gif`, https, no userinfo or port, every resolved address public and
  the vetted one dialled, each redirect re-vetted (3 at most), no token, cookie or proxy, identity encoding,
  10 MiB on the wire, body sniffed as a GIF; same 4 slots and 8 s as the live fetches, 404 otherwise.
- **History (server).** New WS verb `{"history": "<thread>", "before" | "after" | "around":
  "<composetime>", "limit": N}` (N capped at 200, default 100; no anchor = the latest page).
  `before` pages back, `after` pages forward, `around` returns about N/2 on each side of the
  anchor (jump to a date or to a search hit). Source order: archive `messages.db` when the thread
  has one, else the API (`fetch_history`, one page), same rule as spec 008. Answer to the asking
  socket only: `{"history": "<thread>", "before": <cursor echoed, null for the latest page>,
  "messages": [...], "older": bool, "newer": bool, "source": "archive"|"api", "cursor": <opaque>}`
  (`cursor`: on an empty page with more beyond, the `before`/`after` to ask next), or the same `history` and `before` with `"error"`:
  `invalid before` (not an ISO time), `busy` (one history request in flight per socket), `archive unavailable` (archive read deadline hit). Any verb that raises
  (history and search included) answers a generic `failed`; the exception stays in the log. "history unavailable" /
  "search unavailable" are only the page's own messages after 10 busy retries. A null or empty cursor means the latest page. The API source only serves the latest page and `before` (no `after`/`around`).
  The archive's tail (`newer` false, not an older `before` page) behind the board row's last message id gets one API page's
  newer messages appended; the API unreachable, it is answered alone plus `"behind": "<that id>"`, and the page asks the
  latest page again (backoff 1 s → 30 s), merging what it adds in place, a "loading latest messages…" indicator shown after
  600 ms and kept at least 400 ms, until an answer has no `behind` or the relay brings that id.
  Message record: `id`, `at` (composetime ISO), `from` (mri), `name`, `me` (bool), `type`
  (`text`, `system`, `call`, `other`), `html` (sanitised body), `edited` (bool), `deleted` (bool),
  `reactions` ([{key, emoji, count, mine}]), `mentions` (spec 003 kind: `me`, `all`, `null`).
- **Sanitiser (server, stdlib `html.parser`).** Allowlist: `p div br hr h1 h2 h3 b strong i em u s del
  code pre blockquote ul ol li a img span table tr td th`, attribute-free unless listed. Attributes kept: `a[href]` (`http(s)` only,
  `rel=noopener target=_blank` forced), `img[src alt title width height]`, `span` only when it is a
  Teams emoji or mention (`itemtype` ending in `/Emoji`, `/CustomEmoji`, `/Mention`): emoji become
  their `alt` character when unicode, else `<img class="emoji" src=…>`; mentions become
  `<span class="mention" data-mri=…>Name</span>` (`mention-me` when it is me). Everything else is
  dropped, text kept. `img src` is rewritten to `/media/<thread>/<file>` when the object id has a
  file in the archive (`attachments.object_id` + `existing`), else to `/media/<thread>/<id>?host=<host>`
  for an asyncgw object (`&view=<view>` when `src` names another view than `imgo`), to `/media/<thread>/giphy-<id>?giphy=<host><path>` with `data-giphy` for a Giphy
  image (spec 031); another Teams object (`/objects/` on an `asm.skype.com` or asyncgw host) is the plain text "[image not archived]" (no
  link: Teams' copy needs the token the page lacks); any other http(s) image is a link to it (new tab,
  `noopener`) reading `[image: <alt>]`, or "[image not archived]" without alt (the CSP keeps it off the
  page; the text alone inside a link); other schemes are the plain text. The page retries a `/media` image that fails to load for about 30 s,
  then replaces it with the same text; a Giphy one then loads Giphy's own copy (no referrer: every page is `Referrer-Policy: no-referrer`; CSP
  `img-src` allows `https://*.giphy.com` alone), then reads as the `[image: <alt>]` link. Reply quotes (`blockquote itemtype=…/Reply`) render as a quote with the quoted author.
- **Page (`chat.html`).** Header: chat label, member count, an **Open in Teams** button,
  ◀ back to the board. Open in Teams sends the `open` verb for the thread, so it follows the
  server's `--opener` / `--open-scheme` like a plain click on the board (desktop client by
  default), and falls back to the https deep link in a new tab when `--opener none`. The `open`
  verb accepts a followed thread even when it is not on the board. Each message also has an
  "Open in Teams" action that opens the deep link on that message. Each Open in Teams is an `<a>` on
  the https deep link (Copy link address); a plain click or Enter goes through `openTeams` as above,
  a modified click is left to the browser. A message's 🔗 action, offered only in a secure context
  (the loopback bind, or https behind a proxy: the clipboard API's), copies that link, "Copied" for 1.5 s, or says why it could not. Messages grouped by sender with avatar initials (same colour rule as
  the widget), time, `edited` marker, reactions line, the quoted message inside a reply. Opens scrolled to the bottom. Scrolling to
  the top asks `history` with `before` and keeps the scroll position (one request in flight at a
  time, stops when `older` is false); after a jump, scrolling to the bottom asks `after` the same
  way until `newer` is false. A "↓ latest" button shows whenever the latest message is not in
  view, with the count of messages received since; it reloads the latest page if the page was
  detached by a jump, then scrolls to the bottom. Live: the page opens
  its own WS (`/ws`, same Origin check) and subscribes with `{"follow": "<thread>"}`; the server
  then forwards `NewMessage`, `MessageUpdate` (edit, delete, reactions) and typing events for that
  thread as `{"msg": record}`, `{"edit": record}`, `{"typing": name, "on": bool, "mri": mri}` to that
  socket (one frame per person, keyed by `mri`, off on ClearTyping, on that person's next message, or
  after the board's typing TTL; my own typing is never relayed). The `follow` answer carries `typing`
  as `[{name, mri}]`: who already types, so open, split switch and reconnect show what the row shows. New
  messages append when the latest page is loaded (else they only bump the "↓ latest" count); the
  page sticks to the bottom only when already there.
- **Search and jump.** A search field in the header: verb `{"search": "<thread>", "q": text}` runs
  the MCP archive search on that one thread (`mcp.Archive.search_messages`, case and accents
  ignored, whole history), answers up to 50 hits newest first `{id, at, name, text}`. A thread
  without archive answers `{"search": id, "error": "no archive for this chat"}`. Clicking a hit
  asks `history` `around` its time and highlights the message. (A date picker was dropped: see
  Decisions.)
- **Composer.** Same textarea rules as spec 006 (Enter sends, Shift+Enter newline, error under the
  field, draft kept on failure), reusing the `send` verb; no in-flight lock, sends queue per chat
  (spec 012 Decisions). The server accepts
  `send` for a followed thread even when it is not on the board (archived-only chats), as long as it
  is a private, group or meeting chat: a team channel is refused (a post there reaches the whole team).
- **Message actions**, on hover of a message, like Teams:
  - **Reply with quote** (any message): puts the quoted message above the composer (author +
    first line, ✕ to drop it). Quoting another message adds it, up to 10, in the order picked;
    quoting one already there does nothing. The `send` verb gains `quote: ["<message id>", …]`; the
    server prepends one Teams reply block per id, in that order (Teams'
    own shape for a multi-quote), each in the shape the archive holds: `<blockquote itemscope
    itemtype="http://schema.skype.com/Reply" itemid="<id>"><strong itemprop="mri"
    itemid="<mri>">Name</strong><span itemprop="time" itemid="<id>"></span><p
    itemprop="preview">first 100 chars</p></blockquote>`, author and preview taken from the
    server's copy of the message, never from the page.
  - **React** (any message): a picker with Teams' 6 defaults plus 🙏 and 🙃 (`like heart laugh
    surprised sad angry praying upsidedownface`; `yes`/`no` stay accepted keys of `messages.EMOJI`
    but would repeat 👍); clicking a
    reaction I already gave removes it. The reactions line under a message is clickable too: a
    chip I gave removes my reaction, any other chip adds mine, as in Teams (chip marked when
    `mine`). Verb `{"react": "<thread>", "msg": id, "key": k,
    "remove": bool}` → `send.react()`. Keys outside `messages.EMOJI`'s 8 are refused.
  - **Edit** (own messages only): the message body turns into a textarea holding its plain text;
    Enter saves, Escape cancels. Verb `{"edit": "<thread>", "msg": id, "text": …}` →
    `send.edit_message()`, same validation as `send`. Mentions, emoji and quotes in the original
    are lost on edit (plain text only, like the composer).
  - **Delete** (own messages only), after a confirm: verb `{"delete": "<thread>", "msg": id}` →
    new `send.delete_message()`: `DELETE …/v1/users/ME/conversations/<thread>/messages/<id>`, no
    query, no body (confirmed live: 200, then the message carries `deletetime` and an empty body).
  - Each action answers the asking socket `{"<verb>": id, "ok": true}` or `{…, "error": …}`; the
    message itself updates from the Trouter echo (AC7), no optimistic change.
  - "Own message" is decided on the server from its copy of the message (`from` equals my MRI);
    edit and delete on another's message are refused without an API call.
- **Navigation between chats.** A second ⚙ setting in the widget, "Chat navigation", stored per
  browser under `chatnav`, values `list` (default) and `split`; the chat page reads the same key.
  - `list`: the chat page's ◀ goes to `/?nav=chat`, the board in navigation mode: a plain click on a
    row opens `/chat/<id>` in the same tab, a middle click opens it in a new tab (through
    `--browser` when set, as the middle-click setting does), Shift click opens Teams. The
    widget opened on `/` without `nav` is unchanged.
  - `split`: `/chat/<id>` shows the conversation list on the left (same rows, tabs, seen and mute
    state as the board, kept live) and the chat on the right. A click on a row switches the chat in
    place: the page follows the new thread, loads its history and puts the new id in the URL
    (`history.pushState`; Back returns to the previous chat). Each chat keeps its composer draft
    and quotes while the page stays open. Below ~700px wide the list collapses behind a ☰ button.
    No ◀ in this mode.
  - The widget carries `chatnav=split` in the chat URLs it opens, since the target browser has its
    own storage.
- **Cards.** Bot and app messages carry an Adaptive Card: `RichText/Media_Card` holds it base64
  in `<Swift b64>` (`attachments[].content`, with `summary`), a `RichText/Html` may hold one in
  `properties.cards[].content`; entries sharing a `cardClientId` render once, the last copy. The
  server renders the card body to the same sanitised HTML subset, no script, no external fetch:
  `TextBlock` and `RichTextBlock` (weight, size, wrap; the Teams markdown subset
  bold/italic/links/lists), `Container`, `ColumnSet`/`Column` (as a
  flex row), `FactSet` (a two-column table), `Image` (archived or asyncgw → `/media`, else alt
  text, never a link to its external `url`, unlike a message `<img>`; a `/media` one that never loads
  reads "[image not archived]"),
  `Action.OpenUrl` and `selectAction` (http(s) links, `rel=noopener`), `codesnippet` cards
  (`<pre>`). Unknown elements render their `text`/`title` when present, else nothing. A card with
  no renderable body shows its `summary` or `fallbackText`. Rendered inside `div.card` with the
  app name when known. `type` stays `text` for these messages.
- **Deleted runs.** Two or more consecutive deleted messages collapse into one line, "N messages
  deleted", which expands on click to the usual per-message lines (and collapses back). A lone
  deleted message stays as today. A live delete that extends a run joins it; the composer's
  quotes are unaffected. Bots delete their old notifications in bulk, which otherwise fills the
  page.
- **Theme.** Follows the widget's `theme` and `avatars` settings (same `localStorage`).
- **Docs.** README `web` paragraph: the setting and the route. `docs/agent-archive.md` untouched.

## Out of scope

- File or image upload, paste of images.
- Reactions beyond `messages.EMOJI`'s 10 keys (any-emoji reactions could reuse spec 012's popup later).
- An edit keeping the original's mentions, emoji or quote (edit is plain text).
- Marking the chat read in Teams on open or on send.
- Unread separator, search across chats (the MCP server already does it).
- Video / recording playback, transcripts, call details beyond "call started / ended".
- Card inputs and submit actions (`Input.*`, `Action.Submit`, `Action.Execute`): shown as their
  title, not wired.
- Fetching images that are not in the archive (would need the skype token on the page).
- Message history for threads the archive does not hold beyond one API page.
- Emoji completion and `@` mentions in the composer (spec 012).
- Changing what plain click does.

## Acceptance criteria

- [x] **AC1** Given the ⚙ setting at `chat`, a middle click on a row sends `{"open": id, "chat":
      true}` when `--browser` is set and the server runs `--browser` on `http://<bind>/chat/<encoded
      id>` (changed by the 2026-10-12 decision: plus `#<launch token>`); without `--browser` it opens a new tab on `/chat/<encoded id>` and sends no verb; with the setting at `teams` (default) the
      behaviour of today is unchanged (`open`+`web` when `--browser` is set, else the plain link).
- [x] **AC2** Plain click is unchanged whatever the setting; Ctrl/⌘ click does what middle click does
      (both settings, nav mode, the chat page's list), Shift click opens Teams web (changed by the
      2026-10-14 decision).
- [x] **AC3** `GET /chat/<id>` returns the chat page for a thread on the board or in the archive,
      404 otherwise (changed by the 2026-10-12 decision: 404 only with the cookie, without it every
      `/chat/<id>` gets the empty shell); `GET /media/<id>/<file>` returns the file for an existing archived asset, 404
      for a missing one and for any path escaping the media directory (`..`, absolute, encoded); a bare
      object id with an asyncgw `host` is the archived file, else Teams' copy, else 404.
- [x] **AC4** `history` on an archived thread returns the last `limit` messages oldest-first,
      `older: true` while earlier ones exist; a second call with `before` returns the page before
      it without overlap or gap, `older: false` at the start.
- [x] **AC5** `history` on a thread without `messages.db` returns one API page with
      `source: "api"`; an API failure answers `{"history": id, "error": …}` and leaves the page
      usable.
- [x] **AC6** The sanitiser keeps the allowlisted tags, drops `script`, `style`, `iframe`, event
      attributes and `javascript:` hrefs, renders a unicode Teams emoji as its character, a custom
      emoji as `<img class="emoji">` pointing to the archived file, a mention as
      `span.mention` (`mention-me` for my MRI) and rewrites an archived, asyncgw or Giphy (spec 031) image to
      `/media/…`;
      another Teams object image becomes the plain text "[image not archived]", any other http(s)
      image a link to it reading `[image: <alt>]` (the text alone inside a link).
- [x] **AC7** With the page open and followed, a new message on that thread appends within one
      event; an edit replaces the message in place with the `edited` marker; a delete shows
      "message deleted"; a reaction updates the reactions line.
- [x] **AC8** Enter in the composer calls `send_message` once with the thread id and text; a
      failure keeps the text and shows the reason; success clears the field.
- [x] **AC9** `follow`, `history` and `send` for a thread that is neither on the board nor in the
      archive are refused with an `error` and no API call.
- [x] **AC10** The page is reachable only from the bound loopback address (same bind and Origin
      rules as the board) and its data needs the auth cookie (see Decisions; changed by the 2026-10-12
      decision: the page shell itself loads without it); no token or MRI other than those of the chat's own messages reaches
      the page.
- [x] **AC11** Reply with quote: `send` with `quote: [a, b]` posts a body starting with the
      Reply blockquotes of `a` then `b`, author MRI, name and preview from the server's copy; any
      `quote` id that is not a message of that thread, or more than 10 ids, is refused without
      posting anything (an id the archive lacks may cost one history read); the page renders the sent reply with its quotes.
- [x] **AC12** `react` calls `send.react()` once with the key, `remove: true` when I already
      reacted with it; a key outside `messages.EMOJI` is refused without an API call.
- [x] **AC13** `edit` on my own message calls `edit_message()` once with the new text; on another
      person's message, an empty text or a body over 28 KB it is refused without an API call.
- [x] **AC14** `delete` on my own message calls `delete_message()` once after the confirm; on
      another person's message it is refused without an API call; the page then shows "message
      deleted" from the echo.
- [x] **AC15** Reaching the top of the list loads the previous page once (no duplicate request
      while one is in flight), keeps the visible message in place, and stops at `older: false`.
- [x] **AC16** With the latest message out of view, "↓ latest" shows and counts live messages
      received meanwhile; clicking it shows the latest message at the bottom, after a jump too.
- [x] **AC17** `history` with `around` returns messages on both sides of the anchor with `older`
      and `newer` set; `after` pages forward without overlap or gap.
- [x] **AC18** `search` returns the thread's matching messages, case and accents ignored, at most
      50, newest first; clicking a hit shows that message highlighted with its neighbours; a thread
      without archive answers the `error` above. ~~The date picker shows the first message of the
      picked day.~~
- [x] **AC19** The header's Open in Teams sends `open` for the thread and the server runs the
      opener on its deep link, also for a thread followed but not on the board; with
      `--opener none` it opens the https deep link in a new tab. A message's Open in Teams opens
      the deep link on that message id. Each is a link carrying that https deep link, out of Tab
      on a message; 🔗 copies a message's link.
- [x] **AC20** With `chatnav` at `list`, the chat page's ◀ opens `/?nav=chat`; there a plain click
      on a row opens that chat in the same tab, a middle click opens it in a new tab (through
      `--browser` when set), Ctrl/⌘ click too, Shift click opens Teams; `/` without `nav` behaves as before.
- [x] **AC21** With `chatnav` at `split`, `/chat/<id>` shows the live conversation list beside the
      chat; clicking another row follows that thread, shows its history and changes the URL
      without a page load; browser Back returns to the previous chat.
- [x] **AC22** In `split`, switching chats keeps each chat's unsent draft and quotes, and live
      events of the previous chat no longer reach the page (one followed thread at a time).
- [x] **AC23** In `split`, the list's rows update live like the board (new message moves the row
      up, unread and mention marks), and the socket still never receives data of chats that are not
      on the board beyond their rows.
- [x] **AC24** A `Media_Card` message with a Swift payload and a `RichText/Html` message with
      `properties.cards` both render their card: text blocks with weight, facts as a table, column
      sets side by side, OpenUrl actions as links; a card with an unknown-only body shows its
      summary; nothing of a card is fetched from the network and no `on*` or script survives.
- [x] **AC25** Three consecutive deleted messages render as one "3 messages deleted" line that
      expands on click; a single deleted message renders as itself; a live delete of the message
      right after a run grows the run's count.
- [x] **AC26** Clicking a reaction chip under a message sends `react` with that key: `remove:
      true` when the chip is mine, else `remove: false`; the chip shows a "mine" state from the
      record; chips are reachable by keyboard.
- [x] **AC27** Split list column resizes: a focusable separator (`role="separator"`, aria values)
      between the list and the chat; dragging +100 px gives a 400 px list saved as
      `localStorage.chatnav_w` and kept after a reload; ←/→ step 16 px (Shift 64), Home = 280 min,
      End = `min(560, window − 420)`; double-click or Enter resets to 300 and clears the key; a
      stored out-of-range value is clamped; no separator at 700 px and below or in list mode.
- [x] **AC28** Split filters: the filter row is as tall as the chat header (bottom borders aligned
      within 1 px); tabs Inbox, @, Unread and a "⋯" menu holding Seen and Muted fit a 280 px column
      without scrolling, the active tab always visible; from 360 px the five tabs show inline; the
      filter has a × clear, ↑/↓/Enter/Escape like the widget's, and Ctrl+K focuses it from anywhere.
- [x] **AC29** Split rows and focus: row buttons (👁 Teams read, ⋯ menu with seen and mute, as on the
      widget) take no width until hover or focus; rows use a
      roving tabindex (↑/↓/Home/End move, Enter switches), so the chat header is at most 10 Tab
      presses from the filter; ☰ toggles the docked list above 700 px (saved) and opens a drawer
      with a backdrop below it, with `aria-expanded`, focus into the filter, Escape back to ☰.
- [x] **AC30** Split polish: the chat header never overflows (label at least 80 px at 720 px wide;
      Open in Teams as an icon below 640 px of chat width; breakpoints measured on the real header); the current row is marked by a fill and a 3 px bar and
      scrolled into view on load and Back/Forward, not on live updates; empty tabs and empty filters
      say so right under the bar; focus rings use the accent colour; row times and seen rows reach
      4.5:1 contrast in both themes. Switching chats keeps the list's scroll position.
- [x] **AC31** The chat page has its own ⚙ menu (list and split modes) with the settings it reads:
      theme, avatars, chat navigation. They share the widget's keys in the browser's storage, so a
      page opened in another browser through `--browser` can be set there. Choosing a chat
      navigation value stores it, drops any `chatnav` URL parameter and reloads the page in that
      mode; theme and avatars apply at once.

## Phases

### Phase 1 — history verb + sanitiser
- Work: `Board.history()` (archive page via `ChatStore`, API fallback), `chat_record()`,
  `sanitize()` in a new `render.py` (stdlib parser, media rewrite), `follow` bookkeeping on the
  socket, forwarding of thread events to followers.
- **Covers**: AC4, AC5, AC6, AC7 (server side), AC9
- **Data model impact**: none
- **DoD**: `pytest tests/test_render.py tests/test_web_chat.py` green covering each AC above with
  archive fixtures and a mocked API; `pre-commit run --all-files` rc=0.

### Phase 2 — routes + page
- Work: `/chat/<id>` and `/media/<id>/<file>` in `_process_request`, `chat.html` (history,
  live append, composer, theme), tests for the routes.
- **Covers**: AC3, AC7 (page), AC8, AC10, AC19
- **Data model impact**: none
- **DoD**: route tests green; on a live `web` with a test `--bind`, opening `/chat/<id>` of a
  throwaway chat shows its history with an image, a message sent from Teams appends live, a
  reply from the page appears in Teams.

### Phase 3 — setting + middle click + docs
- Work: ⚙ entry, `midclick` preference, `onauxclick` branch, README.
- **Covers**: AC1, AC2
- **Data model impact**: none
- **DoD**: manual check on the live widget: setting at `chat` → middle click opens the tab; at
  `teams` → previous behaviour; Ctrl click unchanged in both.

### Phase 4 — message actions
- Work: hover actions in `chat.html`; `quote` on `send`; `react`, `edit`, `delete` verbs with the
  own-message check; `delete_message()` in `send.py` after capturing a live delete from the Teams
  web client (wire noted in the function docstring); tests.
- **Covers**: AC11, AC12, AC13, AC14
- **Data model impact**: none
- **DoD**: `pytest` green with one test per AC above (API mocked); on a throwaway chat, a quoted
  reply, a reaction added then removed, an edit and a delete made from the page all show in Teams.

### Phase 5 — scroll, latest button, search
- Work: `after` / `around` on `history`, `search` verb, infinite scroll both ways, "↓ latest",
  search field in `chat.html`, tests.
- **Covers**: AC15, AC16, AC17, AC18
- **Data model impact**: none
- **DoD**: `pytest` green for `after`, `around` and `search` on archive fixtures; on the live
  page, a chat with 1000+ messages scrolls back to its start, a search hit lands on
  the right message, "↓ latest" returns to the bottom.

### Phase 6 — navigation between chats
- Work: `chatnav` setting in the widget ⚙; `nav=chat` mode of the board; split layout in
  `chat.html` (list pane reusing the board's row rendering, in-place switch with re-follow and
  `pushState`, per-chat drafts, narrow-width collapse); tests.
- **Covers**: AC20, AC21, AC22, AC23
- **Data model impact**: none
- **DoD**: `pytest` green incl. browser tests for each AC; on the live test window, both settings
  move between three chats without opening Teams, and Back works in `split`.

### Phase 7 — cards, deleted runs, reaction chips
- Work: collapsed deleted runs and clickable reaction chips in `chat.html`; `render.card_html()` (pure, from the card JSON), hooked into `chat_record` for both
  carriers; CSS for `div.card`, facts and columns; tests on fixture cards.
- **Covers**: AC24, AC25, AC26
- **Data model impact**: none
- **DoD**: `pytest tests/test_render.py` green for each element kind and the fallbacks; on the live
  page, a bot card from the archive reads like its Teams rendering (title, facts, action links).

### Phase 8 — split mode UX pass
- Work: from the UX audit of split mode (screenshots at 4 widths, both themes): resizable column,
  filter row and tabs, row width and roving focus, narrow header, current row, empty states,
  contrast; tests.
- **Covers**: AC27, AC28, AC29, AC30, AC31
- **Data model impact**: none
- **DoD**: browser tests for each AC; screenshots at 1920, 1300, 1024 and 700 px in both themes show
  no clipped tab, no overflowing header and aligned pane borders.

## Data model impact (summary)

None. Archive read-only; auth adds `~/.config/miniteams/web-secret` and the launch token in `web.json` and the launch file (see Decisions).

## Open questions

None.

## Decisions

- Setting stored per browser in `localStorage`, like theme and avatars, not a server flag: it
  changes what the page does with a click, nothing server-side. Overturnable.
- Middle click with the setting at `chat` goes through `--browser` when set, like the Teams web link
  does (babs, 2026-10-09: switch browsers, e.g. Firefox, from the command line); without it, the
  page's own browser.
- Separate page (`chat.html`) rather than a panel inside the widget: a tab per chat is what
  "middle click" means, and the widget stays a list.
- Sanitiser in stdlib (`html.parser`), no new dependency; allowlist, never denylist.
- History served over the existing WS, not a REST endpoint: one Origin check, one code path for
  live and past messages.
- Default page 100 messages, cap 200: the archive query is cheap, the DOM is not.
- Unarchived images are not proxied with the token: it would turn the page into a token-holding
  proxy for any URL and the archive already fetches media.
- Upload stays out; the Open in Teams button covers it, and anything else the page lacks
  (babs, 2026-10-09). Multi-quote kept, as Teams does it.
- The chat page gets its own ⚙ (babs, 2026-10-09): settings are per browser and the page may
  open in another one, so it must be able to set them; it now writes `chatnav` too.
- Split mode UX pass (babs, 2026-10-09): resizable list, reorganised filters, then a full audit.
  Seen and Muted move into a "⋯" menu below 360 px rather than a native select, to keep the
  widget's segmented look. Not this pass: drag-to-collapse, Alt+↑/↓ between chats, cross-tab filter
  hints, absolute old dates, an unread-only Inbox count (would change the widget's meaning too).
- Runs of deleted messages collapse (babs, 2026-10-09): the data is right (the API confirms bots
  delete their old notifications), the page just must not drown in them.
- Cards rendered, not just their text fallback (babs, 2026-10-09): both carriers are Adaptive
  Cards, so one renderer covers them.
- Navigation between chats from the chat page, both as a list page and side by side, chosen by a
  setting (babs, 2026-10-09). Default `list`: smaller change, overturnable.
- Jump to a date dropped (babs, 2026-10-09): the header field looked out of place and added little
  next to the search. `history` keeps `around`, which search hits use.
- Search in the chat, jump to date, infinite scroll and "↓ latest" in scope (babs, 2026-10-09).
  Search reads the archive only: the API has no per-chat search the project uses.
- Quote, reactions, edit and delete in scope (babs, 2026-10-09): the view must work like a normal
  Teams conversation. File upload stays out.
- Quote author and preview come from the server's copy of the message: the page could otherwise
  post a quote attributing any text to anyone.
- Emoji and mention markup is rendered now (read side) so spec 012 only adds the write side.
- Fix round, "fix everything before ship" (babs, 2026-10-10): the chat-page verbs move out of
  `web.py` into their own module.
- The code shared by the two pages becomes one file, inlined into both at serve time (no new route).
- Media, emoji and the WebSocket require the `mt` cookie (`HttpOnly; SameSite=Strict`, one
  year, refreshed on each page load), derived from a persistent secret (`~/.config/miniteams/web-secret`,
  `0600`; delete to rotate).
- Launch through a single-use token in the URL fragment (babs, 2026-10-12; replaces the
  `$XDG_RUNTIME_DIR` launch files, the run dir and its lock, KISS): pages are static shells served without the cookie; `web.json` (`0600`)
  holds `{host, port, token}`; the page strips the fragment at once and sends its token in an
  `X-Miniteams-Token` header to `GET /auth` (204 + cookie, token re-minted into `web.json`; 403 otherwise).
  A header, not the URL: no history, and cross-site it needs a preflight, which the GET-only websockets
  server never answers. A failed `web.json` write keeps the old token valid. Middle click "chat here"
  through `--browser` opens `/chat/<id>#<token>`. Each token also goes into a launch file
  (`MINITEAMS_LAUNCH_FILE`, default `~/.config/miniteams/open.html`, `0600`), a page redirecting to
  `http://<host:port>/#<token>`: `chromium --app=file://<launch file>` keeps the token out of argv (a
  failed launch-file write is logged, rotation goes on; superseded by spec 024: no launch file). A `--bind` run leaves a `web.json` naming another
  bind alone, re-checked at every re-mint, and prints its launch URL on stderr at start and after each re-mint. Known limit: the jq
  launch and `--browser` middle-click opens put the token in argv; a process reading `/proc` may spend it
  first, and the page then shows "Not signed in".
- Without the cookie the page shows "Not signed in" (its socket is refused: a plain `GET /ws` answers
  403); a foreign Host name gets 404.
  Origin checks kept. Known limit: another local user cannot read the secret (file mode); the remaining
  exposure is a process running as the same user, which can read the file.
- Known limit: the cookie is host-scoped, not port-scoped, so another HTTP server on the same `127.0.0.X`
  loopback address receives it. Nothing in HTTP fixes that; miniteams draws a dedicated `127.0.0.X` host and
  keeps it across redraws to make that unlikely.
- Archive reads have deadlines: `archive unavailable` (history, search, follow) or 503 (pages, media) when the
  mount hangs, with one `archive_unavailable` warning per 30 s in the log; a follower gets a `gap` frame and
  reloads the latest page when live frames were dropped.
- Known limits after the fix round: the custom emoji host defaults to the fr-prod region
  (`MINITEAMS_ASYNCGW_HOST` overrides it; per-region wiring waits for a check of the authz
  response); at exit, threads stuck on a dead mount do not hold the process.
- Ctrl/⌘ click = middle click (touchpads); Shift click opens Teams web (babs, 2026-10-14).
- The page's typing line matches the row (babs, 2026-10-14): live frames only carry changes, so the
  `follow` answer lists the current typers (TTL timers, my own left out); expiry clears both.
- A growing composer never covers the last message (babs, 2026-10-14): the list shrinks from below,
  a reader at the bottom stays there, any other keeps the list's bottom edge.

## Recipient read receipts amendment (validated 2026-10-13, spike first)

### Problem and solution

The chat view does not show who read what. Like Element X, each member's avatar sits at the right of
the newest message they read, whatever its author, and moves as they read on; more than 3 readers on
a message show 3 avatars and "+N", the hover lists them all. Only confirmed reads count; missing
receipts prove nothing.

### Evidence and limits

- Miniteams currently consumes our own `consumptionhorizon` from ConversationUpdate to update
  unread rows. It deliberately ignores `ThreadActivity/MemberConsumptionHorizonUpdate`.
- The inspected purple-teams message handlers send our own consumption horizon when we read
  a conversation. They do not establish how to consume recipient read receipts.
  [Native reference](https://github.com/EionRobb/purple-teams/blob/master/teams_messages.c).
- Teams supports receipts in 1:1 and group chats of 20 people or fewer, subject to policy,
  individual settings and recipient availability. Disabled or unavailable receipts remain unknown.
  [Microsoft reference](https://support.microsoft.com/en-us/teams/chat/use-read-receipts-for-messages-in-microsoft-teams).
- Native recipient identity, message-boundary semantics, initial snapshot and live update format
  remain to be verified. Our read marker must never stand in for another person's receipt.

### Acceptance criteria

- [x] A read-only native spike identifies an actual recipient receipt and its message boundary,
      confirms snapshot/reconnect behaviour and records a sanitized fixture. No Graph API.
- [x] An outgoing message shows Seen only with a confirmed recipient read; hovering lists
      confirmed readers by name. Group partial counts do not imply everyone has read.
- [x] Live receipt updates refresh the indicator without changing message time, chat ordering,
      unread counters or local seen state. Out-of-order events never regress a confirmed receipt.
- [x] Missing, disabled, unsupported or malformed receipts show no false Seen or Unread claim.
- [x] Reconnection reconciles native state; tests cover 1:1, partial group reads, unknown state,
      recipient identity and unrelated activity events.

### Phases and data impact

1. Spike native receipt retrieval and Trouter events, including the libpurple reference.
   **Data model impact**: none. **DoD**: validated protocol and sanitized fixtures saved in misc;
   unresolved protocol questions block implementation.
2. Implement receipt state and the chat indicator after validation of this amendment and spike.
   **Data model impact**: proposed latest horizon per tenant/chat/recipient; storage choice follows
   the verified snapshot/reconnect behaviour. **DoD**: acceptance tests and pre-commit pass,
   and the indicator agrees with native Teams on a controlled conversation.
   **Done (2026-10-14)**: `receipts.py`, a `horizons` table in the presence cache, avatars on the
   chat page ("Seen" = a reader's avatar, per the Element X decision). Live, GET only, on the
   test group chat: snapshot 200, 2 members, ours left out, 1 reader shown, the cached paint
   identical after a restart. The side-by-side look against the Teams client is left to babs.

### Spike findings (2026-10-13)

Evidence: `misc/presence-spike/README.md`, fixture `receipts-result.json`.

- **Live event** on the shared Trouter socket (`/messaging`): `NewMessage` with messagetype
  `ThreadActivity/MemberConsumptionHorizonUpdate`; `content` is a JSON string
  `{"user": <mri>, "consumptionhorizon": "<msgId>;<readAtMs>;<clientMsgId>", "messageVisibilityTime", "version"}`;
  chat = `conversationLink` tail; no resource id. Boundary = `msgId` (compare numerically); our own
  reads never come this way. 60 min: 4 events, 2 chats, monotonic.
- **Snapshot found**: `GET https://<chat host>/v1/threads/<id>/consumptionhorizons` → 200,
  `consumptionhorizons: [{id: <mri>, consumptionhorizon, messageVisibilityTime}]` for every member
  with a receipt (peer + me in a real 1:1; both members in the group test chat). Absent member =
  unknown. `/v1/users/ME/conversations/<id>/consumptionhorizons` 404; `…/properties` 405; thread and
  conversation views carry only our own horizon.
- **Design consequences**: on chat open and on every reconnect/gap, one GET reconciles the open
  chat; live events update it after. Persistence is not needed for correctness (the snapshot
  rebuilds it); the SQLite cache only saves the first paint and must merge with max(msgId). No
  Graph, no write call. Self-chat (test "1:1") has no peer: use a real 1:1 for the controlled check.

### Decisions (grill, babs, 2026-10-13)

- The spike reuses the presence harness (`misc/presence-spike/probe.py`, a receipts mode): a
  dedicated Trouter endpoint logs `ThreadActivity/MemberConsumptionHorizonUpdate` on the 1:1 test
  chat then the group, and a GET probe looks for a per-member horizons snapshot (unverified, e.g.
  `/v1/threads/<id>/consumptionhorizons`); sanitized fixtures in `misc/`; no Graph.
- Element X layout: one avatar per member at their newest read message, any author; own avatar
  excluded; a message's author not shown on it; >3 → 3 avatars + "+N", hover lists every reader;
  avatars from the archive, initials fallback.
- Unknown state shows nothing (never "unread"); partial knowledge shows confirmed members only;
  the latest horizon per chat and member is kept in the local SQLite cache shared with presence
  (spec 015) across restarts; horizons never move backwards.
- Chat page only; widget rows unchanged.
