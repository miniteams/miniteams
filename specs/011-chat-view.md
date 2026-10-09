# 011 — Chat view in the widget (`/chat/<thread>`)

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-09

## Problem

The widget (spec 002) is a list of rows; reading a conversation means leaving for Teams. The quick
reply (spec 006) answers one line blind: no history on screen, no way to see what came before or
what landed while typing. Middle click today opens Teams web, a heavy page that reloads its whole
app for one chat.

## Solution

A ⚙ setting, "middle click opens the chat here", off by default. When on, a middle click on a row
opens a new tab on `/chat/<thread>` served by the same `miniteams web` process: the conversation's
history, newest at the bottom, rendered like Teams (bold, links, lists, quotes, emoji, images from
the archive), followed live, with the same composer as the widget at the bottom. Reply there,
Enter sends. Ctrl click and plain click keep their current behaviour.

## Scope

- **Setting.** ⚙ menu gains "Middle click: Teams web / chat here" (per browser, `localStorage`
  key `midclick`, default `teams`). With `chat`, `onauxclick` button 1 opens `/chat/<id>` in a new
  tab (`window.open`, `noopener`) without any WS verb, whether or not `--browser` is set.
- **Route.** `GET /chat/<thread>` serves `chat.html` (new static page next to `widget.html`),
  `GET /media/<thread>/<file>` serves a file from `<data-dir>/<thread>/media/` (read-only, path
  confined to that directory, 404 otherwise). The thread id is taken from the URL path, url-decoded.
  A thread not on the board and not in the archive gets a 404.
- **History (server).** New WS verb `{"history": "<thread>", "before" | "after" | "around":
  "<composetime>", "limit": N}` (N capped at 200, default 100; no anchor = the latest page).
  `before` pages back, `after` pages forward, `around` returns about N/2 on each side of the
  anchor (jump to a date or to a search hit). Source order: archive `messages.db` when the thread
  has one, else the API (`fetch_history`, one page), same rule as spec 008. Answer to the asking
  socket only: `{"history": "<thread>", "before": <cursor echoed, null for the latest page>,
  "messages": [...], "older": bool, "newer": bool, "source": "archive"|"api"}`, or the same
  `history` and `before` with `"error"`: `invalid before` (not an ISO time), `busy` (one history
  request in flight per socket), `history unavailable` (any unexpected failure; the exception stays
  in the log). A null or empty cursor means the latest page. The API source only serves the latest page and `before` (no `after`/`around`).
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
  file in the archive (`attachments._object_id` + `_existing`); otherwise the image is replaced by a
  "[image not archived]" link to the original URL (no token on the page, the browser cannot fetch
  it). Reply quotes (`blockquote itemtype=…/Reply`) render as a quote with the quoted author.
- **Page (`chat.html`).** Header: chat label, member count, an **Open in Teams** button,
  ◀ back to the board. Open in Teams sends the `open` verb for the thread, so it follows the
  server's `--opener` / `--open-scheme` like a plain click on the board (desktop client by
  default), and falls back to the https deep link in a new tab when `--opener none`. The `open`
  verb accepts a followed thread even when it is not on the board. Each message also has an
  "Open in Teams" action that opens the deep link on that message. Messages grouped by sender with avatar initials (same colour rule as
  the widget), time, `edited` marker, reactions line, the quoted message inside a reply. Opens scrolled to the bottom. Scrolling to
  the top asks `history` with `before` and keeps the scroll position (one request in flight at a
  time, stops when `older` is false); after a jump, scrolling to the bottom asks `after` the same
  way until `newer` is false. A "↓ latest" button shows whenever the latest message is not in
  view, with the count of messages received since; it reloads the latest page if the page was
  detached by a jump, then scrolls to the bottom. Live: the page opens
  its own WS (`/ws`, same Origin check) and subscribes with `{"follow": "<thread>"}`; the server
  then forwards `NewMessage`, `MessageUpdate` (edit, delete, reactions) and typing events for that
  thread as `{"msg": record}`, `{"edit": record}`, `{"typing": name, "on": bool}` to that socket (one
  frame per person, off on ClearTyping, on that person's next message, or after the board's typing
  TTL; my own typing is never relayed). New
  messages append when the latest page is loaded (else they only bump the "↓ latest" count); the
  page sticks to the bottom only when already there.
- **Search and jump.** A search field in the header: verb `{"search": "<thread>", "q": text}` runs
  the MCP archive search on that one thread (`mcp.Archive.search_messages`, case and accents
  ignored, whole history), answers up to 50 hits newest first `{id, at, name, text}`. A thread
  without archive answers `{"search": id, "error": "no archive for this chat"}`. Clicking a hit
  asks `history` `around` its time and highlights the message. A date picker in the header does
  the same `around` the start of the picked day.
- **Composer.** Same textarea rules as spec 006 (Enter sends, Shift+Enter newline, in-flight lock,
  error under the field, draft kept on failure), reusing the `send` verb. The server accepts
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
  - **React** (any message): a picker with Teams' 8 default keys (`messages.EMOJI`); clicking a
    reaction I already gave removes it. Verb `{"react": "<thread>", "msg": id, "key": k,
    "remove": bool}` → `send.react()`. Keys outside the 8 are refused.
  - **Edit** (own messages only): the message body turns into a textarea holding its plain text;
    Enter saves, Escape cancels. Verb `{"edit": "<thread>", "msg": id, "text": …}` →
    `send.edit_message()`, same validation as `send`. Mentions, emoji and quotes in the original
    are lost on edit (plain text only, like the composer).
  - **Delete** (own messages only), after a confirm: verb `{"delete": "<thread>", "msg": id}` →
    new `send.delete_message()`. Wire format confirmed against a live delete in phase 4 before
    the function is written; the Teams client sends a soft delete.
  - Each action answers the asking socket `{"<verb>": id, "ok": true}` or `{…, "error": …}`; the
    message itself updates from the Trouter echo (AC7), no optimistic change.
  - "Own message" is decided on the server from its copy of the message (`from` equals my MRI);
    edit and delete on another's message are refused without an API call.
- **Theme.** Follows the widget's `theme` and `avatars` settings (same `localStorage`).
- **Docs.** README `web` paragraph: the setting and the route. `docs/agent-archive.md` untouched.

## Out of scope

- File or image upload, paste of images.
- Reactions beyond Teams' 8 default keys (any-emoji reactions could reuse spec 012's popup later).
- An edit keeping the original's mentions, emoji or quote (edit is plain text).
- Marking the chat read in Teams on open or on send.
- Unread separator, search across chats (the MCP server already does it).
- Video / recording playback, transcripts, cards (`Swift`, adaptive cards render as their text
  fallback), call details beyond "call started / ended".
- Fetching images that are not in the archive (would need the skype token on the page).
- Message history for threads the archive does not hold beyond one API page.
- Emoji completion and `@` mentions in the composer (spec 012).
- Changing what plain click or Ctrl click do.

## Acceptance criteria

- [ ] **AC1** Given the ⚙ setting at `chat`, a middle click on a row opens a new tab on
      `/chat/<thread id>` and sends no `open` verb; with the setting at `teams` (default) the
      behaviour of today is unchanged (`open`+`web` when `--browser` is set, else the plain link).
- [ ] **AC2** Ctrl click and plain click are unchanged whatever the setting.
- [ ] **AC3** `GET /chat/<id>` returns the chat page for a thread on the board or in the archive,
      404 otherwise; `GET /media/<id>/<file>` returns the file for an existing archived asset, 404
      for a missing one and for any path escaping the media directory (`..`, absolute, encoded).
- [ ] **AC4** `history` on an archived thread returns the last `limit` messages oldest-first,
      `older: true` while earlier ones exist; a second call with `before` returns the page before
      it without overlap or gap, `older: false` at the start.
- [ ] **AC5** `history` on a thread without `messages.db` returns one API page with
      `source: "api"`; an API failure answers `{"history": id, "error": …}` and leaves the page
      usable.
- [ ] **AC6** The sanitiser keeps the allowlisted tags, drops `script`, `style`, `iframe`, event
      attributes and `javascript:` hrefs, renders a unicode Teams emoji as its character, a custom
      emoji as `<img class="emoji">` pointing to the archived file, a mention as
      `span.mention` (`mention-me` for my MRI) and rewrites an archived image to `/media/…`; an
      unarchived image becomes the "[image not archived]" link.
- [ ] **AC7** With the page open and followed, a new message on that thread appends within one
      event; an edit replaces the message in place with the `edited` marker; a delete shows
      "message deleted"; a reaction updates the reactions line.
- [ ] **AC8** Enter in the composer calls `send_message` once with the thread id and text; a
      failure keeps the text and shows the reason; success clears the field.
- [ ] **AC9** `follow`, `history` and `send` for a thread that is neither on the board nor in the
      archive are refused with an `error` and no API call.
- [ ] **AC10** The page is reachable only from the bound loopback address (same bind and Origin
      rules as the board); no token or MRI other than those of the chat's own messages reaches
      the page.
- [ ] **AC11** Reply with quote: `send` with `quote: [a, b]` posts a body starting with the
      Reply blockquotes of `a` then `b`, author MRI, name and preview from the server's copy; any
      `quote` id that is not a message of that thread, or more than 10 ids, is refused without an
      API call; the page renders the sent reply with its quotes.
- [ ] **AC12** `react` calls `send.react()` once with the key, `remove: true` when I already
      reacted with it; a key outside the 8 defaults is refused without an API call.
- [ ] **AC13** `edit` on my own message calls `edit_message()` once with the new text; on another
      person's message, an empty text or a body over 28 KB it is refused without an API call.
- [ ] **AC14** `delete` on my own message calls `delete_message()` once after the confirm; on
      another person's message it is refused without an API call; the page then shows "message
      deleted" from the echo.
- [ ] **AC15** Reaching the top of the list loads the previous page once (no duplicate request
      while one is in flight), keeps the visible message in place, and stops at `older: false`.
- [ ] **AC16** With the latest message out of view, "↓ latest" shows and counts live messages
      received meanwhile; clicking it shows the latest message at the bottom, after a jump too.
- [ ] **AC17** `history` with `around` returns messages on both sides of the anchor with `older`
      and `newer` set; `after` pages forward without overlap or gap.
- [ ] **AC18** `search` returns the thread's matching messages, case and accents ignored, at most
      50, newest first; clicking a hit shows that message highlighted with its neighbours; the
      date picker shows the first message of the picked day; a thread without archive answers the
      `error` above.
- [ ] **AC19** The header's Open in Teams sends `open` for the thread and the server runs the
      opener on its deep link, also for a thread followed but not on the board; with
      `--opener none` it opens the https deep link in a new tab. A message's Open in Teams opens
      the deep link on that message id.

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

### Phase 5 — scroll, latest button, search, jump
- Work: `after` / `around` on `history`, `search` verb, infinite scroll both ways, "↓ latest",
  search field and date picker in `chat.html`, tests.
- **Covers**: AC15, AC16, AC17, AC18
- **Data model impact**: none
- **DoD**: `pytest` green for `after`, `around` and `search` on archive fixtures; on the live
  page, a chat with 1000+ messages scrolls back to its start, a search hit and a date jump land on
  the right message, "↓ latest" returns to the bottom.

## Data model impact (summary)

None. Archive read-only, no new file under `~/.config/miniteams`.

## Open questions

None.

## Decisions

- Setting stored per browser in `localStorage`, like theme and avatars, not a server flag: it
  changes what the page does with a click, nothing server-side. Overturnable.
- Middle click with the setting at `chat` works without `--browser`: the target is our own page,
  the browser running the widget is the right one.
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
- Search in the chat, jump to date, infinite scroll and "↓ latest" in scope (babs, 2026-10-09).
  Search reads the archive only: the API has no per-chat search the project uses.
- Quote, reactions, edit and delete in scope (babs, 2026-10-09): the view must work like a normal
  Teams conversation. File upload stays out.
- Quote author and preview come from the server's copy of the message: the page could otherwise
  post a quote attributing any text to anyone.
- Emoji and mention markup is rendered now (read side) so spec 012 only adds the write side.
