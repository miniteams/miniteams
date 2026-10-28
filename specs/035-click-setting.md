# 035 — Choose what each click on a chat row opens

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-28

## Problem

A plain click on a widget row always opens the Teams desktop client (through `--opener`), and the ⚙
**Middle click** setting only offers Teams web or the chat page. Someone who reads chats in miniteams's
own chat page has to middle click or Ctrl click every time, and no click can open the desktop client
from a middle click. The split list on the chat page has no setting at all.

## Solution

Each list gets two settings in its ⚙ menu, **Click opens** and **Middle click opens**, set
independently. Both offer the same three targets: `Teams app` (the desktop client), `Teams web`,
`miniteams` (the chat page). Defaults keep today's behaviour.

## Scope

- Stored values, both keys: `app` (Teams app), `teams` (Teams web), `chat` (miniteams), the spelling
  `midclick` has used since spec 011. An unknown value reads as the key's default.
- **Widget** (board): keys `click` (default `app`) and `midclick` (default `teams`).
- **Split list** (chat page, `chatnav=split`): keys `listclick` (default `chat`) and `listmidclick`
  (default `chat`), in the chat page's ⚙.
- **Click** covers the plain click and Enter on the selected row; on the widget, also a click on a
  desktop notification. **Middle click** covers Ctrl/⌘ click alike.
- Targets:
  - `app`: the server runs `--opener` on the deep link (`{"open": id, "web": false}`). No opener or no
    message id falls back to `teams`.
  - `teams` (Teams web): through `--browser` when set (`{"open": id, "web": true}`), else the row's
    https link in a new tab.
  - `chat` (miniteams):
    - widget, either click: the chat page through `--browser` when set, else a new tab (Middle click
      `chat` today).
    - split list, plain click: switches chats in place (today); middle click: a new tab through
      `--browser` when set (today).
- README: the ⚙ settings lines and the `--opener` / `--browser` wording; `--help` texts of both.

## Out of scope

- Shift click: stays Teams web.
- `/?nav=chat` (the chat page's picker): its clicks stay fixed (plain = same tab, middle = new tab).
- Server, CLI flags, native window code: the pages already have every verb they need (help texts and
  docstrings follow the new wording).

## Acceptance criteria

- [x] **AC1** With no stored choice, every click does what it did before this change, on the widget
  and on the split list, but for the `Teams app` fallback (Decisions).
- [x] **AC2** Widget, Click `teams`: a plain click sends `{"open": id, "web": true}` with `--browser`,
  else opens the row's link in a new tab.
- [x] **AC3** Widget, Click `chat`: a plain click sends `{"open": id, "chat": true}` (plus `chatnav` in
  split mode) with `--browser`, else opens `/chat/<id>` in a new tab.
- [x] **AC4** Widget, Middle click `app`: a middle click and a Ctrl/⌘ click send
  `{"open": id, "web": false}`, and the browser opens no Teams tab of its own.
- [x] **AC5** Split list: each of the 3 × 2 choices routes as listed in Scope; a plain click on
  `app` or `teams` leaves the current chat in place.
- [x] **AC6** Enter on a selected row follows Click, on both lists; a widget notification click too.
- [x] **AC7** `?nav=chat` ignores the settings.
- [x] **AC8** Each ⚙ menu shows **Click opens** and **Middle click opens**, three choices each, set
  independently; a choice survives a reload.

## Phases

### Phase 1 — widget
- Work: `widget.html`: one routing function both clicks go through, the **Click opens** segment,
  `app` on Middle click.
- **Covers**: AC1, AC2, AC3, AC4, AC6, AC7, AC8
- **Data model impact**: none
- **DoD**: page tests green; live check on a test server (own `--bind`), each setting, Enter and a
  notification click included.

### Phase 2 — split list + README
- Work: `chat.html`: same routing for the split list's rows, its two ⚙ segments; README; the "Chat here"
  left in README (native widget section) and the `widget_qt.py` docstring becomes ⚙ "miniteams".
- **Covers**: AC1, AC5, AC6, AC8
- **Data model impact**: none
- **DoD**: page tests green; live check on a split chat page in each setting.

## Decisions

- Labels `Teams app`, `Teams web`, `miniteams` under "Click opens" / "Middle click opens" (babs asked
  for "Open in" wording naming native, browser and miniteams).
- `miniteams` on a widget plain click opens a new tab (or `--browser`): in the native widget window,
  opening it in place would replace the board (babs, 2026-10-28).
- The split list has its own keys: its default plain click switches in place, the widget's opens the
  desktop client; one shared key would change one of them.
- `Teams app` without `--opener` or without a message id falls back to Teams web instead of hiding
  the choice, through `--browser` like the `teams` target (a plain click used to open the link in the
  widget's own browser there).

## Open questions

- none
