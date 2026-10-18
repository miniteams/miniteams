# 024 — Native widget window and simpler launch

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-18

## Problem

The widget runs as a `chromium --app` window. Its Alt-Tab icon is Chromium's 16×16 favicon, blurry at
xfwm4's 64 px; it has no tray icon, so closing it closes it; and it opens through `open.html`, a launch
file written under the snap's data directory that only spares the single-use token a short window in argv.

## Solution

`miniteams widget` opens the widget in its own window: a sharp icon in Alt-Tab, an icon in the system
tray showing a count of the user's choice, and close-to-tray. The web pages are the ones served today,
in the same Chromium engine. Launching any page needs no launch file: the token rides in the URL.

## Scope

- **Native window (optional extra).** `miniteams widget` shows the board page of the running
  `miniteams web` in a PySide6 / QtWebEngine window (Chromium engine), with its own persistent profile so
  the sign-in cookie survives restarts. Installed with an optional dependency group; without it the
  command says how to install it.
- **Icon.** A 256×256 miniteams icon on the window, kept across reloads; window class `miniteams`.
- **Tray.** A StatusNotifier tray icon (XFCE's tray speaks it). Closing the window hides it to the tray;
  the tray menu offers Show, Settings… and Quit; activating the icon shows the window.
- **Tray badge.** The tray icon carries a count and the tooltip states it. A preference picks what is
  counted: unread chats (the Unread tab's rule), mentions (the @ tab's rule), the Inbox tab, or none.
  The count ignores the text typed in the filter box. "99+" past 99.
- **Notifications.** A preference picks what raises a desktop notification: none, mentions, or every new
  message in a non-muted chat. Muted and Seen-marked chats follow the tabs' rules; never for my own
  messages; none while the window is focused on that chat. Its Open button shows the widget and opens
  the chat, as clicking its row does; dismissing it opens nothing.
- **Settings.** A "Tray" section in the widget's ⚙, shown only inside the native window, holds the
  badge and notification preferences. Tray → Settings… shows the window with the ⚙ open.
- **Links.** Links the page would open in a new window or tab (target=_blank, middle click,
  `window.open`) open in the system browser. Opening a chat keeps today's path (the server opens it per
  `--browser`).
- **Simpler launch.** `open.html` and `MINITEAMS_LAUNCH_FILE` go. Pages are reached at
  `http://<bind>/#<token>`, the token read from `web.json` (0600) and renewed after each use, as today;
  the cookie and the Host check stay. `miniteams web` logs the bare URL and where the token is (the token
  itself never reaches a log); the README gives the
  `chromium --app` line built from `web.json` for those who keep a browser window.
- **Fixed bind.** When `web.json`'s address is taken, `miniteams web` exits with an error naming it
  instead of moving to a new one (a moved address loses the native window's cookie and every launcher).

## Out of scope

- Installing the widget as a browser web app (PWA): measured, its window icon is only 32×32.
- Starting `miniteams web` from `miniteams widget`; autostart entries.
- Showing chat pages inside the native window.

## Acceptance criteria

- [ ] **AC1** With `miniteams web` running, `miniteams widget` opens a window showing the board, signed
  in through the URL token on first run and through the kept cookie afterwards.
- [ ] **AC2** The window's `_NET_WM_ICON` holds the 256×256 icon, still after a reload; its window class
  is `miniteams`.
- [ ] **AC3** Closing the window hides it; the tray icon shows it again; tray Quit exits the process.
- [ ] **AC4** The tray icon's badge and tooltip follow the chosen preference (unread, mentions, Inbox,
  none) and change as the board changes, also while the window is hidden; the filter box does not
  change them.
- [ ] **AC5** The "Tray" section of ⚙ shows only in the native window; tray → Settings… opens it.
- [ ] **AC6** A link that would open a new window or tab opens in the system browser; opening a chat
  behaves as today.
- [ ] **AC7** Without the optional dependencies, `miniteams widget` exits with the command to install
  them; without a running `miniteams web`, it exits naming the address it tried.
- [ ] **AC8** No launch file is written; `http://<bind>/#<token>` signs a browser in once and the token is
  renewed; `launch_file` / `MINITEAMS_LAUNCH_FILE` are gone from `src/`, `tests/`, `.env.example` and the
  README (past specs and the README's migration note may still name `open.html`).
- [ ] **AC9** When the bind in `web.json` is taken, `miniteams web` exits non-zero naming it, and
  `web.json` is unchanged.
- [ ] **AC10** With notifications on mentions, a new message mentioning me raises one desktop
  notification (sender, chat, text); with every message, any new message in a non-muted chat does; none
  for my own messages, muted chats (a mention excepted, as on the @ tab) or the chat the focused window
  shows; with none, nothing.
- [ ] **AC11** The notification's Open button shows the widget and opens that chat as clicking its row
  does; dismissing it opens nothing.

## Phases

### Phase 1 — Simpler launch and fixed bind
- Work: remove the launch file; log the bare URL and where the token is (never the token); fail on a
  taken bind; README launch line.
- **Covers**: AC8, AC9
- **Data model impact**: none
- **DoD**: unit and browser tests; a test asserts `launch_file` / `MINITEAMS_LAUNCH_FILE` appear nowhere in
  `src/`, `tests/` (outside that test), `.env.example` or the README;
  full suite and pre-commit green.

### Phase 2 — Native window with tray
- Work: `miniteams widget` (PySide6 / QtWebEngine), icon, tray, close-to-tray, external links, the
  count published by the page, the badge, the ⚙ "Tray" section.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7
- **Data model impact**: none
- **DoD**: tests for the count the page publishes (browser) and the badge rendering and preference logic
  (unit); a run under Xvfb with a tray host checking the icon sizes, close-to-tray and the badge; full
  suite and pre-commit green; the user tries it on the desktop.

### Phase 3 — Desktop notifications
- Work: the page raises notifications per the preference; the window shows them on the desktop and
  routes a click; the preference in ⚙ "Tray".
- **Covers**: AC10, AC11
- **Data model impact**: none
- **DoD**: browser tests for which messages notify under each preference; a run under Xvfb with a
  notification daemon checking a notification and its click; full suite and pre-commit green.

## Data model impact (summary)

None. The badge and notification preferences live with the widget's other settings.

## Open questions

None.

## Decisions

- Native window in PySide6 / QtWebEngine, optional; PWA install dropped; settings in the widget's ⚙;
  tray menu Show / Settings… / Quit; count chosen by a preference — user's calls (2026-10-18).
- The page publishes its counts to the window (they keep updating while hidden, measured); the window
  does not open its own socket nor recompute — one counting rule, in `ui.js`.
- `miniteams widget` needs `miniteams web` running and does not start it — overturnable.
- Chat pages keep opening through the server's `--browser`, not in the native window — overturnable.
- Cost, measured under Xvfb: ~261 MB download (~670 MB installed) for the optional extra; ~770 MB RSS
  with the board loaded vs ~500 MB for today's Chromium window (software rendering under Xvfb; likely
  lower on the desktop).
- Notifications in scope as phase 3: a none / mentions / every-message preference, its Open action opens
  the chat — user's call (2026-10-18, review comment).
- Badge colours fixed: red pill white digits for unread/Inbox, amber pill dark digits for mentions — user's
  call (2026-10-18). Red `#c62828` / white 5.6:1, amber `#f59e0b` / `#171a21` 8.1:1; the two hues stay apart at
  22 px (a burnt orange was too close to the red), and the icon's blue was unreadable there.
- Bridge: a QWebChannel, its script injected by the window at document creation; the page reads
  `window.miniteamsTray` (a promise of the `tray` object: `badge(pref, count)` in, `settings` out), absent in a
  browser. Phase 3 adds its notification calls to the same object.
- Badge default: Unread — overturnable. With the mentions preference the icon shows the mention count
  ("99+" past 99) on the amber pill — user's call (2026-10-18).
- Profile in `<config dir>/widget` (0700: its cookie store holds the secret), HTTP cache in memory.
- Notifications default: mentions (**@**) — overturnable. The page decides from two consecutive live board frames
  (a new mention `msg_id`, a new `last_id` from someone else), so no wire change; nothing from the first frame of a
  connection nor while the server seeds (`busy`), which also holds the badge at no count.
- "None while the window is focused on that chat": the native window shows the board, not a chat, so none while the
  window is shown and active (the user sees the list change) — overturnable.
- Delivery over `org.freedesktop.Notifications` by `gdbus` (PySide6 cannot type Notify's uint argument), clicks
  matched by notification id on the session bus (QSystemTrayIcon's messageClicked does not say which one).
  Only the **Open** action opens the chat; a dismissal (`NotificationClosed` reason 2) or an expiry just forgets
  the id — user's call (2026-10-18): a dismiss must stay harmless and "clear all" must not open N chats.
  xfce4-notifyd 0.9.7 reports a body click only as a dismissal, so a body click opens nothing there.
- A mention in a muted chat notifies (AC10's muted exception), as the @ tab still lists it; a plain message
  there does not. One live notification per chat: a new one replaces the chat's previous one.
