# miniteams

Personal CLI that authenticates as the signed-in user and dumps incoming Microsoft Teams chat
messages to stdout in real time — over the same outbound **Trouter** channel the official client
uses. No inbound endpoint, no polling. Reading is the core; it can also **send** (default target:
your own Notes) and **dump** a conversation's history.

Inspired by [`EionRobb/purple-teams`](https://github.com/EionRobb/purple-teams) (transport +
framing) and [`Gerenios/AADInternals`](https://github.com/Gerenios/AADInternals) (token tricks).

> Interop, not a sanctioned API. Uses a Microsoft first-party `client_id` and undocumented
> endpoints — expect breakage when Microsoft rotates flows. **Use only against your own account.**

## Status

Working. Auth (device-code, or browser), live `stream` (messages, reactions, attachments,
auto-reconnect), `chats` (recent conversations, date-window filter), `dump` (history backfill,
`--jsonl`), and `send` are all implemented.

## Setup

```sh
cp .env.example .env        # then set MINITEAMS_TENANT_ID
uv sync
```

## Usage

```sh
uv run miniteams login            # device code: open the printed URL, enter the code (also copied to a desktop clipboard); masked tokens
uv run miniteams --browser-login login   # Azure CLI browser sign-in, Teams scope via FOCI (device code if refused)
uv run miniteams stream           # live: incoming messages, reactions, attachments
uv run miniteams stream --media images,transcripts   # download only these attachment types
uv run miniteams chats            # 20 most recent private chats (id + last activity + names)
uv run miniteams chats --since 2026-07-01 --until 2026-07-10   # activity window; --all, --jsonl
uv run miniteams send "hello"     # send to your Notes (write-safe default)
uv run miniteams send --thread 19:xxx@thread.v2 "hi"   # send to a conversation
uv run miniteams send --thread 19:xxx@thread.v2 "@Ann Lee **ready** :fire:"   # rendered as the chat
                                  # page's composer: markdown, :shortcode:, bare URLs, @mentions (below)
uv run miniteams send --file msg.txt              # body from a file (rendered the same way)
uv run miniteams send --file test.html --html     # raw RichText/Html, verbatim (no markdown, no @mentions)
uv run miniteams send "look" --thread 19:xxx@thread.v2 --image a.png --image b.jpg  # inline images after the text
uv run miniteams send "here" --thread 19:xxx@thread.v2 --attach f.pdf  # any file, as Teams does: to your OneDrive
                                  # chat files folder, shared with the chat (up to 10, 100 MiB each; --image too)
uv run miniteams update <msg-id-or-deep-link> "fixed text"   # edit a sent message
uv run miniteams react <msg-id-or-deep-link> like    # react (heart, laugh, 1f525_fire…)
uv run miniteams react <msg-id-or-deep-link> like --remove   # take it back
uv run miniteams typing --thread 19:xxx@thread.v2 --for 30   # show "is typing…" there for ~30 s
uv run miniteams emojis           # org custom emojis: creation date, name, creator
uv run miniteams emojis --jsonl --download   # + reaction key, image saved under ~/.cache/miniteams/emojis/
uv run miniteams archive          # resumable local archive under ./data: private chats, meetings,
                                  # and your own `48:` conversations (Notes, feeds)
uv run miniteams archive --thread 19:xxx@thread.v2     # archive one conversation
uv run miniteams archive --no-media                    # messages only, skip attachments
uv run miniteams archive --verify-media                # re-check every asset on disk, fetch missing
                                                       # (recovers old transcripts from SharePoint too)
uv run miniteams archive --videos --verify-media       # also grab meeting recordings + shared videos
                                                       # (large; --verify-media sweeps existing chats)
uv run miniteams archive --media transcripts --verify-media   # only these attachment types (below)
uv run miniteams archive --loop 600                    # re-run forever, sleeping 600s between runs
                                                       # (bare --loop = 300s); stops on auth expiry
uv run miniteams archive --live                        # catch up, then store each message as it
                                                       # arrives, plus a pass every 1800s (--live N);
                                                       # stops on auth expiry (exit 1)
uv run miniteams archive --recheck-since 2026-09-22    # one-shot: re-walk chats active since the
                                                       # date down to it, filling any hole
uv run miniteams web              # local live page: conversations newest-first (see below)
uv run miniteams web --limit 0 --bind 127.0.0.5:8765   # load every chat (with an archive that is
                                                       # every archived chat: a heavier page)
uv run miniteams web --data-dir /srv/teams-archive     # seed rows from an archive kept elsewhere
uv run --extra widget miniteams widget   # the running web's board in its own window + tray icon (spec 024)
uv run miniteams mcp              # MCP server over stdio for an agent (spec 008; see below)
uv run miniteams watch --thread 19:xxx@thread.v2 --once   # one JSON line per matching live event
                                                       # (spec 009; docs/watch.md)
uv run miniteams dump             # dump your Notes' full history (oldest → newest)
uv run miniteams dump --thread 19:xxx@thread.v2        # dump a conversation
uv run miniteams dump --jsonl > notes.jsonl            # full-detail JSON per line
```

`mcp` serves your Teams to an MCP client over stdin/stdout (spec 008). Reads come from the
archive: `find_chats` (label, topic, participant, kind), `read_messages` (plain text, date window,
`next_since` cursor), `search_messages` (substring, case and accents ignored, 7 days back by
default, feeds left out) and `read_transcript` (recordings, then turns). Results say where they come
from (`source`) and carry the archiver's heartbeat (`archiver_seen`, `archiver_state`,
`sync_started_at`, written by `archive` into `index.db`), so an agent can tell a stopped archiver
from a quiet one. When the archive cannot be reached (no `index.db`, or no answer in 3 s) the
first three read from the API instead, with `source: "api"`. `send_message` and `update_message`
write to Teams as you; with `send_at` (ISO 8601 with an offset, 5 seconds to 125 days ahead)
`send_message` schedules instead, and `list_scheduled`, `update_scheduled` and `cancel_scheduled`
manage what Teams is holding (`docs/scheduled-send-wire.md`). Their text, like `miniteams send`'s
without `--html`, is rendered as the chat page's composer does (spec 029): markdown, built-in
`:shortcode:` emojis, bare URLs as links. `@Ann Lee` or `@ann` (a member's full or first name, case and
accents ignored) mentions that member, shown as typed (`Ann Lee`, `Ann`); `@everyone` mentions a group
chat; `\@` is a literal `@`, and an email or code is never a mention. A name matching nobody or
several members, a roster that cannot be fetched, `@everyone` on a 1:1, more than 50 mentions or
28 KB of HTML refuse the send (an MCP error, exit 2 for the CLI), nothing written. `--read-only` keeps every writing tool
out of the list. Nothing needs a token until the
API is called; a dead credential makes that call return a device-login URL and code, and `login` reports the
same flow. The server needs the project's `.env` and `data/`, so register it as
`uv run --directory /path/to/miniteams miniteams mcp` (a gitignored `.mcp.json` at the repo root
does it for Claude Code).

`dump --jsonl` emits one full-detail record per message (raw resource + resolved sender/thread
+ attachment refs + reactions), every message type, no media download — pipe-friendly.

`web` serves a single page listing private, group and meeting chats newest-message-first
(label, last author — `You` for your own — snippet), updated live from the Trouter stream: new messages move a row
up, edits/deletes of the last message rewrite it, `✍ Name` shows who is typing (cleared after
10s if Teams never says so), and with `--reactions` a reaction becomes the row's last event.
Rows are seeded from the archive under `--data-dir` (default `./data`, read-only): the name, snippet
and read state of a chat `archive` already knows cost no API call, which is what makes a deep
`--limit` bearable — it defaults to 400 chats with an archive, 250 without. The page is served as
soon as the seed is read and fills in from the live listing afterwards; a small spinner at the
bottom right means that walk is still running, and the rows may reshuffle when it lands. Mentions
are scanned at startup on the newest rows only (50); older chats surface theirs when the next
message arrives. It binds a random `127.0.0.X:PORT` drawn once and kept in `~/.config/miniteams/web.json` so the
URL stays bookmarkable; when that address is taken, `web` exits with an error naming it and leaves `web.json` as is
(free the address, or delete `web.json` to draw a new one, then relaunch every browser with a token); `--bind` overrides it and refuses anything but loopback (IPv6 as `[::1]:PORT`). Media files, emoji and the WebSocket
need the auth cookie, see *First launch*. Clicking a row opens the chat at its last message through `--opener` (default `xdg-open`
on an `msteams://` deep link, i.e. the desktop client; `--open-scheme https` for Teams web,
`--opener none` to let the page follow its plain https link); opening does not mark the row
seen. Middle or Ctrl/⌘ click opens the https link (or the chat page, see the ⚙ setting below), Shift click the
https link; through `--browser firefox` when set, else in the page's own browser. `xdg-open` only works when the `msteams` handler's `.desktop` entry passes the
URL along (`Exec=… %u`); otherwise point `--opener` at the client binary, e.g.
`--opener "/path/to/teams-for-linux --no-sandbox"`. Hovering a preview cut by the row width shows the whole message (up to 1000 characters).
Hovering (or tabbing into) a row shows `✎`, `👁` (mark read in Teams) and `⋯`, a menu with **✓ Mark seen**,
**🔕 Mute** and **Section ▸** (same rows in the chat page's split list, without `✎`). Seen: the row leaves the Inbox until something
newer lands on that chat (state in `~/.config/miniteams/seen.json`); the **Seen** tab lists those rows dimmed,
where ⋯ → ↺ Unseen undoes it. A message that @mentions you gets a blue stripe and an `@ you` tag (`@ all` for an
@everyone), also when the mention was added by editing an older message; it clears once you mark the row seen
or read the chat in Teams after the mention. The `@` tab lists mentioned rows, **Unread** follows Teams' own
read marker. Mute is Teams' own mute, shared with the Teams client and your other devices: it takes a chat out of Inbox
whatever lands on it (listed under **Muted** until ⋯ → 🔔 Unmute; mentions still surface). The row turns muted once Teams
accepts the change; a refused one leaves it as it was. On its first start the widget pushes the chats of an older
local `muted.json` to Teams once, then renames the file `muted.json.migrated` (a chat Teams refuses is skipped; a token, network or Teams-side failure keeps the file for the next start). `✎` (or `r` on the row selected with the arrow keys, `Tab` from the filter) opens
a reply box under the row: Enter sends it to the chat as you and keeps the box open, Shift+Enter adds a line,
Escape closes it and keeps the draft; a failed send stays in its queue line with why (sending: see *Composer*). The ⚙ menu picks the theme (system by default), hides avatars for a denser list, and sets
**Middle click** and **Chat navigation** (see *Chat page*). The
Teams session expired (refresh token dead): `web` logs `auth_expired` and waits; run `miniteams login` in another
terminal and it reconnects within seconds (it watches the token cache file).

**Sections** group chats, in the widget and the split list alike (state in `~/.config/miniteams/sections.json`):
- ⋯ → Section ▸ files the chat in a section, **None**, or a **New section…** named inline. A chat is in one section at most.
- Once a section exists, **Recent / Sections** in the list's header switches the arrangement; each view (widget, chat
  page) keeps its own choice. Sections mode shows the sections in order, chats with none under **Other**, last; the
  tabs filter as usual, and a search shows only the sections it reaches.
- A section's header folds it (folds are kept per view); folded, it shows its unread count and `@`. Its ⋯ renames,
  sets **Recent** (by activity) or **Manual** order (an arrival never moves a chat), moves it up or down, and
  deletes it (its chats go to Other).
- Drag and drop (Sections mode): a chat onto a header (folded too) or a chat files it in that section, at the drop
  position in a Manual one; onto Other it leaves its section. A header dropped on another lands before it; Other stays last.
- Keys: ↑/↓ walk headers and chats; on a header Enter, ← and → fold and unfold; ← on a chat goes to its header;
  Alt+↑/↓ moves a chat within a Manual section. From a filter box, ←/→ act only while it is empty.

**First launch**
- Open `http://<bind>/#<token>`, both read from `web.json`; `web` logs the bare URL and the file at start (`web_listening url=… token_in=…`), never the token:
  ```bash
  chromium --class=miniteams --user-data-dir=$HOME/snap/chromium/common/miniteams/profile \
    --app="$(jq -r '"http://\(if (.host | contains(":")) then "[\(.host)]" else .host end):\(.port)/#\(.token)"' \
      "${XDG_CONFIG_HOME:-$HOME/.config}/miniteams/web.json")"
  ```
  It needs `jq`; `web.json` lives in the config dir (`$XDG_CONFIG_HOME/miniteams` when set, else `~/.config/miniteams`; `MINITEAMS_CONFIG_DIR` overrides it), and an IPv6 host is bracketed.
  `--user-data-dir` gives the widget its own profile and process, so it neither stacks with the other Chromium windows nor reuses their session; `--class=miniteams` gives it its own window class (taskbar entry, window rules). With a non-snap browser use any profile dir.
- `web.json` (`0600`) holds the bind and a single-use token; the page trades the token at `/auth` for the `mt` cookie (`HttpOnly; SameSite=Strict`, one year, refreshed on each page load) and drops it from the address bar. Each launch consumes the token and `web` writes the next one into `web.json`, so the same command always works, and once a browser holds the cookie the plain `http://host:port/` URL works too.
- An `open.html` left by an older version is no longer written nor read: delete it and switch launchers to the line above.
- The token sits in argv until the page has traded it, where a process reading `/proc` could spend it first; the page then shows `Not signed in`: relaunch.
- The secret never appears on a command line, in the terminal or in the browser history.
- Pages load without the cookie, but show `Not signed in` and stay empty until launched with a token: data (the socket, media, emoji) needs the cookie.
- Open the page by the bound address: any other `Host` name (e.g. `localhost`) gets 404 (DNS-rebinding guard).
- The secret is persistent, in `~/.config/miniteams/web-secret` (`0600`); delete it to rotate, then relaunch each browser with a token.
- Chat pages opened through `--browser` get the current token in their URL fragment, so a fresh `--browser` profile signs in on the first open.
- Known limit: the cookie is host-scoped, not port-scoped, so another HTTP server on the same `127.0.0.X` address receives it. `web` draws a dedicated `127.0.0.X` and keeps it across restarts to make that unlikely.
- Archive reads have a deadline: a hung mount answers `archive unavailable` (history, search, follow) or HTTP 503 (pages, media) and logs one `archive_unavailable` warning per 30 s. A chat page that missed live frames reloads its latest page. At exit, threads stuck on a dead mount do not hold the process.

**Native window** (spec 024)
- System package (X11): `libxcb-cursor0` (Debian/Ubuntu; Qt's xcb plugin fails to load without it).
- `uv sync --extra widget` once (PySide6 / QtWebEngine, ~260 MB download), then `uv run --extra widget miniteams widget`
  while `miniteams web` runs. It shows the board in its own window: 256 px icon in Alt-Tab, window class `miniteams`.
- It signs in with `web.json`'s token, then keeps the cookie in its own profile (`~/.config/miniteams/widget`, `0700`).
  It exits 1 when `web.json` is missing or nothing answers at its address. Launched again while it runs, it shows
  the running window and exits (one window per profile).
- Closing the window hides it to the tray (with no tray to bring it back, closing quits); the tray menu has **Show**,
  **Settings…** and **Quit**. A click on the icon shows the window; on a panel with *menu is primary* (XFCE's default) a left click opens the menu, a middle click shows it.
- ⚙ → **Tray** (native window only) picks the count on the icon: **Unread**, **@** (mentions, on an amber
  pill instead of red), **Inbox** or **None**; "99+" past 99, the tooltip says how many. Counts follow the tabs'
  rules, ignore the filter box, and keep updating while the window is hidden; disconnected from `web`, or while
  `web` is still seeding its list, the icon shows no count.
- ⚙ → **Notify** (native window only) picks what raises a desktop notification: **@** (a new mention, the default,
  also in a muted chat as on the @ tab), **Messages** (that, plus any new message in a chat the Inbox shows: not
  muted, not older than its Seen mark) or **None**. Never for your own messages, nor for the board a (re)connection
  or the seeding loads; none while the window is in front. Title: sender · chat; body: the mentioning message's text
  for a mention, else the chat's last message, cut to 200 characters. One notification per chat: a newer one
  replaces it. Sent with `gdbus` (`libglib2.0-bin`) to the desktop's notification service.
- A notification's **Open** button shows the window and opens the chat as clicking its row does. Dismissing it (or
  "clear all") opens nothing; xfce4-notifyd reports a body click only as a dismissal, so there a body click opens
  nothing either.
- Links that would open a new window or tab (and any page other than the board) open in the system browser;
  rows open chats as in the browser widget. A chat page ("Chat here" without `--browser`) gets `web.json`'s current
  token in its fragment, as with `--browser`, so that browser signs in.
- Chromium's sandbox needs unprivileged user namespaces or a setuid `chrome-sandbox`. Where AppArmor restricts the
  namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`, Ubuntu's default), QtWebEngine uses
  `$CHROME_DEVEL_SANDBOX`, else `/usr/lib/chromium/chrome-sandbox`, else the chromium snap's; with none of them it fails to start.
  Never run it with the sandbox disabled.

**Presence** (spec 015)
- A dot on the avatar of 1:1 rows (widget and split list) and of message authors: green available, red busy or
  do-not-disturb, amber away, grey ring offline. Hover: `Availability · Activity` (e.g. `Busy · In a meeting`).
- Unknown presence (no answer, `PresenceUnknown`, refused subscription) shows no dot, never Offline.
- Followed: the peers of the 50 newest 1:1 rows plus the authors an open chat page shows (50 per page, 100 people in all).
- Native presence service + `web`'s own Trouter socket, no Graph; re-subscribed on every reconnect and every 10 min.
  Two periods (20 min) without a confirmation and the dots go. The collector publishes nothing of your own presence.
- Latest states are cached in `~/.cache/miniteams/presence.sqlite3` (local disk, `0600`): after a restart they show
  at once, faded and `(unconfirmed)`, until the service confirms them. Delete the file anytime; it is regenerated.

### Chat page

A chat's own view, `/chat/<thread>`: history, live updates, composer. Settings are per browser (`localStorage`).

- **Middle click**, Ctrl/⌘ click alike (widget ⚙): `Teams web` (default) or `Chat here`, which opens the chat page through `--browser` when set,
  else in a new tab. The same `--browser` serves chat pages opened from the widget.
- **`/?nav=chat`**: the board as a chat picker; the chat page's ◀ goes there. Click opens the chat in the same tab,
  middle or Ctrl/⌘ click in a new one, Shift click opens Teams.
- **Chat navigation** (⚙, on the widget and on the chat page): `List` (default) or `Split`. `Split` shows the list on the
  chat page's left instead of ◀: a click switches chats in place (Back returns), each chat keeps its draft and quotes.
  The widget adds `?chatnav=split` to the chat URLs it opens, so a page in another browser follows the setting.
  - Drag the list's edge to resize (or focus it: ←/→, Home/End, Enter or double click resets); width and fold are kept per browser.
  - ☰ folds the list, or opens it over the chat below 700px.
  - Ctrl+K jumps to the list's filter: ↑/↓ pick a row, Enter opens it. Seen and Muted sit behind ⋯ in a narrow list.
  - **Sections** (see above) work the same here, with their own Recent / Sections choice and folds.
- **Images**: served from the archive's `media/` folder, else fetched from Teams by `web` (memory only, never written
  to the archive; archives run with `--no-media` or without `images` in `--media` show theirs this way). One the page cannot load yet is retried
  for about 30 s, then reads `[image not archived]`. A Giphy GIF (spec 031) shows inline too: the archived copy
  (`giphy-<id>.<ext>`), else fetched from Giphy by `web` (no token), else loaded by the page from Giphy itself (no
  referrer), else the link below. Any other external image is a link to it, `[image: <alt>]` (`[image not archived]`
  without alt; the text alone inside a link): the page loads no other external image.
- **Mark read in Teams when shown** (⚙, on by default): a chat opened, and each message from someone else landing in
  it, marks it read in Teams once the window is visible, focused and at the latest message, and the archive has caught
  up (no "loading latest messages…"); one `read` a second per chat, the last one sent only if all that still holds.
- **Composer** (also the widget's quick reply): completes `:emoji` and `@member` (`@everyone` on group chats, not 1:1;
  picked members are notified; a mention needs a non-word character before `@`, and two members with the same name map
  in the order they were picked). Messages are limited to 28 KB and 50 mentions.
  - A backslash keeps markup literal: `\*` `\_` `\~` `` \` `` `\[` `\@`, and on the chat page a line starting `\- ` `\* `
    `\> ` `\1. ` (or a line that is only `\>`); `\\` is one backslash before another backslash or one of those, or
    (chat page) before a line-start marker; any other backslash stays as typed (`\\srv\share`), and backslashes
    inside `code`, link labels and URLs stay as typed.
  - A bare `http(s)://` URL is sent as a link (spec 028), without the punctuation that ends the sentence
    (`. , : ; ! ? * _ ~ ' "`, `… – — « » ‹ › “ ” ‘ ’`, and a closing `) ] } >` unless it closes a bracket opened
    inside the URL). A `'` is kept only before a letter or digit, and a `'` inside the host ends the URL
    (`https://x.com's`). Received messages show their bare URLs as links by the same rule.
  - Enter empties the box at once: sends queue one at a time per chat, in order. Queued sends survive a reload or a
    dropped connection and go out in order, an unanswered one checked (their images and files do not survive a reload).
    A chat page in list mode sends only its own chat's: other chats' wait for the widget (◀) or their own page.
  - The queue shows: greyed bubbles after the chat page's last message (`queued`, `sending…`, `sent` (acknowledged,
    waiting for Teams' echo), `failed` with its reason, `paused`), each replaced by the message once Teams echoes it;
    one line per message under the widget's reply, gone once sent. A failed send stays in the queue, even across a
    reload, the box untouched, the messages behind it `paused`: ↻ retries it, ✕ drops it, ✎ moves it into an empty box
    or edits it in place (Enter resends, Escape cancels). On the chat page a failed message Teams echoes counts as
    sent and the rest goes on (the widget sees no echo: ↻ checks and finds it); a `sent` bubble whose echo never
    comes leaves after 30 s.
  - Backspace after a picked full-name mention shortens it to the first name while keeping its recipient.
  - Paste images (PNG, JPEG, GIF, WebP; up to 8, 10 MiB each, 40 MiB in all): thumbnails per chat, click to preview,
    × to drop; Enter sends them after the text, or alone. They upload to Teams only on send and live in the page:
    a reload drops them, the text draft stays. A failed or unanswered send may have gone out: ↻, or the unchanged
    message sent again, even after a reload, looks for it in the chat before posting again; an edited one goes out
    as a new message.
  - Attach any file with 📎 or by dropping it on the composer (up to 10, 100 MiB each): a chip with its name and size,
    × to drop. A dropped or pasted PNG, JPEG, GIF or WebP stays an inline image; 📎 attaches even those as files.
    Enter sends them with the text, as Teams does (your OneDrive chat files folder, shared with the chat). The page
    streams them to the local server, which spools them to unnamed temp files under `~/.cache/miniteams/uploads/`.
  - Widget: inline formatting.
  - Chat page: bold, italic, strike, code, lists, quotes through a toolbar or Ctrl+B / Ctrl+I, plus a preview of the message as posted.
- **Messages**: hover for actions: reply with a quote, react, edit and delete your own. The picker shows 8 favorites
  (default: Teams' 6 reactions plus 🙏 and 🙃) and `…`, which reacts with any emoji found by `:name`, the org's custom ones
  included. Set the favorites in ⚙ **Reactions**: click a slot to replace it, ◀ ▶ move the chosen slot, `Reset`; kept
  in this browser's `localStorage`. An emoji goes as Teams' name for it (`praying`) when it has one, else as its hex key
  (`1f98a_foxface`); a custom one as its `<name>;<id>` key. An emoji already under the message reacts with that
  chip's key, mine first. Every reaction chip toggles yours.
  Hover any reaction chip to see all the message's reactions, grouped by emoji with their participants.
  Native skin tones (`yes-tone1` → 👍🏻 through `yes-tone5` → 👍🏿) are preserved on supported emoji.
  Cards from bots and apps are rendered; runs of deleted messages are folded.
- **Editing** (✎, spec 019): the message turns into an editor like the composer: `:emoji` / `@member` completion,
  Ctrl+B / Ctrl+I and a mini toolbar with a 👁 preview. It opens on the message's own markdown (formatting, links,
  emojis, mentions kept as picked), its reply quotes above and its pasted images under it, each removable (✕ / ×),
  none addable. Enter saves the message composed as a send, Shift+Enter breaks the line, Escape cancels; Enter with
  nothing changed closes it without an edit (Teams would mark it edited). A message miniteams cannot write back
  (table, heading, nested list, underline, card, image inside the text, tag mention…) opens no editor: it says why,
  with Open in Teams. Edited in Teams meanwhile: "edited elsewhere at HH:MM", and "take that version" reloads it;
  saving still replaces it. A message changed so that a kept quote or image is gone is refused: "reopen" loads it again, keeping a text typed meanwhile; a reopen answered "not editable here" keeps that text read-only, to copy, with the reason and Open in Teams (Escape closes it).
- **Read receipts** (spec 011): each member's small avatar sits under the newest message they read, whoever wrote it.
  More than 3 readers on one message show 3 avatars and `+N`; hover lists every reader. Your own reads and an author's
  read of their own message are not shown. A member without a receipt (disabled, unsupported, not read yet) shows
  nothing, never "unread". Kept live from Teams' receipt events on `web`'s Trouter socket, reconciled by one read-only
  GET on chat open and after every reconnect; cached in the presence file so a reopened chat paints at once.
- **Browsing**: infinite scroll both ways, "↓ latest" when away from the bottom, header search, Open in Teams.
- **Message links**: every Open in Teams (header, ↗ Teams on a message, can't-edit line) is a real link: right-click,
  Copy link address. A plain click still goes through `--opener`; a modified (Ctrl, Shift…) or middle click is the browser's. 🔗 on a
  message copies its https Teams link; it shows only in a secure context (the loopback bind, or https behind a proxy), where the browser offers a clipboard.
- **Received files** (spec 027): a card per file (icon, name) linking the copy the archive saved for that file's own link (when held and ≤ 50 MiB), else its sharing link, else its objectUrl; a pasted file link reads as the file name.
- **Reply quotes** (spec 021): click a quote (or Tab to it, Enter) to jump to the quoted message, loading the history around it; "quoted message not found" when it is gone.
- **Members** (spec 022): hover, click or Tab to "N members" for the people, You first then by name, with avatar and presence when known; 50 rows then "… and N more", bots and apps as "+ N apps", "members unavailable" when the roster cannot be had (a roster not cached is fetched without delaying the page). Escape or leaving closes it.
- **Private chat** (spec 023): in a chat with several people, click someone else's name (or Tab to it, Enter) to switch to your 1:1 with them; Back returns. Middle or Ctrl/⌘ click opens it in a new tab. The server finds the 1:1 in the board rows, then the archive index, with no Teams call; "no private chat with them yet" when there is none.
- **Archive**: history and search read the archive under `--data-dir`. Without one the page shows the latest API page only
  and search is off.

`watch` follows the stream and prints one JSON line per event matching its criteria: `--thread`,
`--from me|others|anyone`, `--event message|reaction`, `--after MSG` (also catches up from the
chat's history), `--reaction KEY`, `--once`, `--note`. Built for Claude Code's Monitor tool, which
turns each line into an event in the session; `docs/watch.md` has the call and the service lines
(`gap`, `suppressed`, `stopped`).

`stream` enriches each line with thread name + participants, resolves MRIs → display names
(cached in `~/.cache/miniteams/names.json`), downloads images/files to `~/.cache/miniteams/media/`
(full-res + optimized, shown as `file://`), and renders reactions (`↳ 👍 Alice reacted`), edits
(`✏ …`) and deletes (`🗑 …`). `--typing` adds `✍ is typing / stopped` indicators.
`stream --jsonl` = per-event JSON; `stream --raw` = full firehose NDJSON of every frame (all
endpoints, presence/calls, named events), decoded.

`archive` keeps private chats, meeting chats and your own `48:` conversations: Notes, scheduled
and parked drafts, and the activity feeds (mentions, notifications, saved, …). `--all` adds
channels. A draft you cancel loses its text on Teams' side; the archive keeps the version it saw
before.

`archive` builds a resumable local archive under `./data/`: `index.db` (chat metadata) plus
one folder per conversation with a `messages.db` (every message as raw API JSON, keyed by id)
and a `media/` folder (attachments at original quality). Interrupt anytime — re-running resumes
from the oldest stored message, tops up new ones, and skips media already on disk. Assets the
archive gave up on are remembered per chat: a 403 (deleted object, lost share permission) or a
413 (transcript over the 50 MiB cap) is permanent, a 404 backs off (1h → 6h → 12h → 24h → 48h,
then every 48h) because it may just be a transcript still being generated, and a 415 (transcript
of the wrong content type or encoding) backs off the same way. `--retry-assets` forces them all, and refuses to run under `--loop`
— a one-shot override on a timer is not an override. Likewise a chat whose whole history is
403 (revoked meeting access) is skipped on later runs; `--retry-denied` re-attempts it. Every run
ends with an `archive_recap` log line (chats, failures, new messages, media files, avatars,
duration). `data/` is gitignored (personal chat data).

`--media TYPES` (spec 014, on `archive`, `archive --live` and `stream`) picks the attachment
types downloaded, comma-separated; the rest of the run is unchanged:

| Type | Downloads |
|---|---|
| `images` | inline pictures, full and optimized views; picture `URIObject`s; Giphy GIFs (`giphy-<id>.<ext>`, fetched without any token) |
| `files` | shared documents (SharePoint, linked in the body or sent as a file card) and other attachments (card and recording thumbnails) |
| `transcripts` | meeting transcripts, every format served (AMS by content type, SharePoint JSON + VTT) |
| `videos` | meeting recordings and shared video files (large) |

The default is `images,files,transcripts`. `--no-media` is an empty selection and `--videos` the
default plus `videos`; neither combines with `--media`. `--verify-media` and `--assets-only` only
fetch the selected types, and a deselected type is neither fetched nor annotated. Files already on
disk are never deleted, whatever the selection.

SharePoint files are saved under their own name; a different file arriving under a taken name is
saved as `name (2).ext`, `name (3).ext`… Each chat's `messages.db` records which file every
SharePoint link was saved as (`sp_files`): a held file costs no SharePoint call on later passes,
and the chat page opens each card's own copy, never one that merely shares its name. Files saved
before this record existed have none: their cards keep the hotlink until the chat's next media pass
(`--verify-media` sweeps every chat) spends one metadata call per such file. A copy whose size matches
SharePoint's is then recorded for good. Any other, or one SharePoint gives no size for, is taken for
another file: it stays, an `sp_file_legacy_mismatch` warning names it, and the file is downloaded again
as `name (2).ext`. SharePoint names past 250 bytes are cut
to fit, extension kept.

`archive --live` (spec 005) replaces the `--loop` timer with the event stream. It subscribes first,
buffers events while a normal pass catches the archive up, then applies them and writes each new
one as it arrives. Edits, reactions and deletes update `messages.raw`; the version they replace is
kept in `message_versions`. Attachments download in the background as each message arrives
(`--no-media`, `--videos` and `--media` apply). Read markers land in `index.db`. A reconnect, a real
`trouter.message_loss`, a failed write or the reconcile timer (`--live SECONDS`, default 1800)
triggers a new catch-up. A live write stays pending until a pass tops the chat up past it, so a
message the stream dropped without saying so is still fetched by the next pass. It uses its own Trouter endpoint,
so it runs beside `web` and `stream`. It cannot be combined with `--loop`, `--thread`,
`--assets-only`, `--retry-assets` or `--recheck-since`.

The top-up stops at the newest message a complete walk reached. `--recheck-since WHEN` lowers
that bound to WHEN for every chat active since then: a one-shot repair for an archive that holds
a hole under a newer message, e.g. rows `--live` wrote before that bound existed. It only adds
missing messages; an edit missed meanwhile stays at the stored version.

Tokens cache under `~/.config/miniteams/` (`0600`); re-runs are silent until the refresh
token expires. Downloaded media (from `stream`) lives under `~/.cache/miniteams/media/`.

> **send writes to real chats as you.** Default target is `48:notes` (your own Notes), but
> `--thread` can post anywhere. `typing` shows your indicator the same way. The rest of the tool is
> read-only.

## Config

All via `MINITEAMS_*` env vars / `.env` (see `.env.example`). `MINITEAMS_ENDPOINT_SUFFIX=-test` gives a
second `web`, `stream` or `archive --live` its own Trouter endpoint ids, so it does not take the
deliveries of the one already running; a second `web` also needs its own `--bind` (sharing the config
dir, it leaves the first one's `web.json` alone, and prints its own launch URL on
stderr at start and after each use: enter it through that URL, or with a cookie a browser already holds for that host).
Defaults are the purple-teams
**work/TFW** constants — override only if auth starts returning 4xx, after re-capturing from a
live browser session. `MINITEAMS_ASYNCGW_HOST` sets the custom emoji host (default `fr-prod.asyncgw.teams.microsoft.com`); set it when your tenant lives in another region.

## Dev

```sh
uv run ruff check . && uv run mypy src && uv run pytest
uv run --with playwright pytest tests/test_browser*.py   # opt-in headless Chrome; skipped without playwright, fails under CI=1
uv run --with playwright playwright install firefox     # scroll and typing tests also run in Firefox; skipped without it
pre-commit run --all-files
```
