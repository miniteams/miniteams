# 006 — Quick reply from the widget

**Status**: approved
**Requested by**: babs
**Date**: 2026-09-23

## Problem

Answering a chat from the widget means opening Teams, waiting for it to route to the chat, typing,
then switching back. For a one-line "ok, je regarde" that round trip costs more than the answer.

## Solution

Select a row in the widget, press `r` (or click ✎), type, press **Enter**. The message goes to
that chat as if typed in Teams. Shift+Enter adds a line, as in Teams. The composer opens under the selected row,
so the target is always visible. Escape closes it.

## Scope

- A composer (`<textarea>`) that opens under the selected row. It starts at one line, grows up to
  6 lines, then scrolls.
- Open: `r` on the selected row (keyboard selection from spec 003's filter), or a ✎ action next to
  ✓ 👁 🔕. Escape closes it and puts focus back in the filter.
- Keys, as in Teams: **Enter** sends, **Shift+Enter** inserts a newline. **Ctrl+Enter** also sends.
  Enter during an IME composition (`isComposing`) never sends.
- Plain text only. Newlines become `<br>`, everything else is escaped (the existing `send.py`
  `_content`).
- New WS verb `{"send": "<thread>", "text": "…"}`. The server calls `send_message()` off the event
  loop, with the live skype token and `me_name` as display name.
- The server answers the sending client only, with `{"sent": "<thread>", "ok": true}` or
  `{"sent": "<thread>", "error": "<short reason>"}`. The row itself updates from the normal Trouter
  echo; no local row insert.
- In flight: the textarea is disabled and a second Enter does nothing.
- On success the composer closes and its draft is cleared. On failure the text stays, the reason
  shows under the field, and Enter retries.
- One unsent draft per chat, kept in page memory. Switching rows keeps the other drafts.

## Out of scope

- Replying to a specific message (quote), mentions, emoji picker, markdown or rich text.
- Attachments, paste of images.
- Editing or deleting a sent message (`edit_message` exists, not wired).
- Outgoing typing indicator.
- Drafts surviving a page reload.
- Marking the chat read on send.

## Acceptance criteria

- [ ] Given a selected row, when I press `r`, then a composer opens under that row with focus in it.
- [ ] Given a row selected from the filter, Tab opens its composer and `r` types into the filter.
- [ ] Given the composer has text, when I press Enter or Ctrl+Enter, then the server calls `send_message`
      with that row's thread id and the exact text, once.
- [ ] Shift+Enter inserts a newline and never sends; Enter inside an IME composition never sends.
- [ ] Given a send succeeds, the client receives `{"sent": id, "ok": true}`, the composer closes,
      and the draft for that chat is empty.
- [ ] Given `send_message` raises (HTTP error, `errorCode` envelope, network), the client receives
      `{"sent": id, "error": …}`, the text stays in the field and a retry sends it again.
- [ ] A `send` verb with empty or whitespace-only text, a non-string `text`, a body over 28 KB once
      escaped, or a thread id that is not a row on the board is refused without any API call
      and answered with an `error`.
- [ ] The ack goes to the sending socket only; other open pages get nothing but the normal row
      update.
- [ ] Two Enter presses while a send is in flight produce one API call.
- [ ] Escape closes the composer, keeps its draft, and returns focus to the filter.

## Phases

### Phase 1 — `send` verb on the server
- Work: `Board.send(thread, text)` in `web.py`, validation as above, `send_message` via
  `asyncio.to_thread`, per-socket ack. Wired next to `read` / `mute` / `open` in `handle`.
- **Data model impact**: none
- **DoD**: `pytest` green with tests for the ok path, each refusal case and the error path
  (`send_message` mocked); `pre-commit run --all-files` rc=0.

### Phase 2 — composer in the page
- Work: ✎ action, `r` shortcut, textarea with the key rules, in-flight lock, ack handling, per-chat
  drafts. README section on the shortcut.
- **Data model impact**: none
- **DoD**: on a live widget started with a test `--bind`, a reply sent to a throwaway chat on the board shows up in
  Teams and as the row's new last message; a forced failure (token blanked) shows the error and
  keeps the text.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- Enter sends, like Teams (babs, 2026-09-23). Ctrl+Enter kept as a second send key.
- Both `r` and the ✎ icon open the composer (babs, 2026-09-23).
- `r` opens the composer only when focus is outside a text field. The arrow keys move the selection
  while focus stays in the filter, where `r` has to stay a letter, so **Tab** from the filter opens
  the composer on the selected row.

- Composer under the row, not a bar at the bottom. Keeps the recipient visible and avoids sending
  to the wrong chat.
- Enter from the filter still opens the chat in Teams. The composer only opens on `r` or ✎, so the
  existing shortcut keeps working.
- No optimistic row update. The Trouter echo already carries the sent message (same
  `clientmessageid`), and a local insert would show a message that may have failed.
- Refuse sends to a thread that is not on the board. The page can only target rows it shows, and it
  stops a stray verb from posting anywhere.
- 28 KB cap on the escaped body, Teams' documented per-message limit, checked before the API call
  so the error is readable.
- Security: sending posts as the user. Only the served page can reach the socket (loopback bind and
  the Origin check in `serve_board`), which is the same guard `read` and `open` already rely on.
