# 039 — Notifications and sounds per interaction

**Status**: shipped
**Requested by**: babs
**Date**: 2026-11-05

## Problem

The native widget's ⚙ → Notify is one coarse choice (@ / Messages / None) plus a Meeting start switch
(#101). A direct message and a busy group chat notify alike, and nothing makes a sound: xfce4-notifyd
plays none (its capabilities lack `sound`). What deserves a ping, and which one, differs per person
and per kind of interaction.

## Solution

⚙ → Notify becomes one table, a row per interaction. Each row has a notification switch and a sound
picker: None, a system sound, or a file of your own, with a ▶ to hear it. A direct message can ping
while a group message only shows a notification, and a meeting start can ring differently from a
mention. Nobody hears a sound they did not pick.

## Scope

- Interactions, in this order:

  | Row | Fires on | Mute | Notify by default |
  |---|---|---|---|
  | @ you | a new mention of me (`mention.kind = "me"`) | ignored | on |
  | @ everyone | a new team/channel-wide mention (`kind = "all"`) | ignored | on |
  | Direct message | a new message in a 1:1 chat | follows the Inbox rule | off |
  | Group message | a new message in a group chat | follows the Inbox rule | off |
  | Meeting chat | a new message in a meeting chat | follows the Inbox rule | off |
  | Meeting start | a call starting in a meeting chat (#101) | ignored | on |

  "Inbox rule" = today's Messages rule: not muted, not older than the Seen mark, not mine, has a
  sender. Mentions and meeting starts keep #101's rules: never my own, a mention wins over a meeting
  start in the same frame.
- Each row: label, notify switch, sound picker, ▶ preview. Notify and sound are independent: a sound
  with no notification is allowed.
- Sound picker: **None** (default), the freedesktop theme's sounds that fit a notification (bell,
  complete, message, message-new-instant, phone-incoming-call, window-attention, dialog-information,
  dialog-warning, alarm-clock-elapsed) when present, then **Custom file…**. Names shown without the
  extension (`message new instant`).
- **Custom file…** opens a Qt file dialog (audio files). The native window keeps the chosen path per
  row in `~/.config/miniteams/widget/sounds.json`; the page only sees the file name. A custom file
  that went missing plays nothing, is logged once, and its row shows "(missing)".
- Playback in the native window through QtMultimedia, no external player.
- No sound while the window is in front (no notification shows then either) nor while xfce4-notifyd
  is in Do Not Disturb (`xfconf-query -c xfce4-notifyd -p /do-not-disturb`, read at play time; absent
  command or property = not DND).
- A burst plays one sound: the first interaction with a sound plays, others within 3 s are silent.
  Notifications are unchanged by this (one per chat, as today).
- Migration, once, from the stored prefs: `notify=mention` → the two @ rows on; `notify=all` → all
  message rows on; `notify=none` → all message and mention rows off; `meetings=0` → Meeting start off.
  The old keys are removed after migration.
- Narrow window (200 px, spec 024 test sizes): each row stacks label on one line, controls on the next;
  nothing clipped.
- README: the ⚙ → Notify paragraph and #101's Meeting start lines.

## Out of scope

- Reactions to my messages, replies in followed threads, incoming calls (Teams rings them).
- Volume control: the system mixer handles it.
- Sounds in the browser widget or the chat page: no tray there, no notifications either.
- Per-chat overrides.

## Acceptance criteria

- [x] **AC1** ⚙ → Notify shows the six rows of Scope, in order, each with a switch, a picker and ▶; the
  @ / Messages / None segment and the Meeting start switch are gone.
- [x] **AC2** With no stored pref, the defaults of Scope apply and no row has a sound.
- [x] **AC3** Stored `notify` / `meetings` prefs migrate as listed in Scope, once; a reload keeps the
  migrated values and the old keys are gone.
- [x] **AC4** Each row notifies exactly on its interaction under its mute rule, and only when its
  switch is on (one test per row, plus my own message / my own mention never notifying).
- [x] **AC5** A row with a sound plays it on its interaction, with or without its notification; a row
  with None plays nothing.
- [x] **AC6** No sound while the window is active, nor in Do Not Disturb.
- [x] **AC7** Two interactions with sounds within 3 s play one sound.
- [x] **AC8** ▶ plays the row's current sound right away, window in front or not, DND or not.
- [x] **AC9** The picker lists only the system sounds present on the machine; Custom file… stores the
  path in `sounds.json`, the page shows its file name; a missing file plays nothing and shows
  "(missing)".
- [x] **AC10** The page never passes a file path to the native window: it asks for a sound by name or
  by row (`custom`), the window resolves it.
- [x] **AC11** Every choice survives a reload and a widget restart.
- [x] **AC12** At 200 × 700 and 267 × 450 no control is clipped; screenshots in both themes reviewed
  before commit.

## Phases

### Phase 1 — native sound bridge
- Work: `widget_qt.py`: the list of available system sounds, `play(row, sound)` with the front-window,
  DND and 3 s burst rules, `preview(row, sound)`, Custom file… dialog and `sounds.json`; exposed on the
  `Tray` bridge.
- **Covers**: AC6, AC7, AC9, AC10
- **Data model impact**: new file `~/.config/miniteams/widget/sounds.json` (row → custom path).
- **DoD**: `tests/test_widget_qt.py` green with the new cases (player stubbed); a live play of one
  system sound and one custom file from the running widget profile.

### Phase 2 — Notify table
- Work: `widget.html` / `ui.css`: the table, per-row rules in `news()`, migration, preview, narrow
  layout; README.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC8, AC11, AC12
- **Data model impact**: localStorage keys per row (`notify.<row>`, `sound.<row>`); `notify`,
  `meetings` removed.
- **DoD**: `tests/test_browser_tray.py` green with per-row cases; screenshots at both sizes, both
  themes, looked at; live check in the native widget on a test server (own `--bind`).

## Data model impact (summary)

- `~/.config/miniteams/widget/sounds.json`: custom sound path per row, written by the native window.
- Widget localStorage: `notify.<row>`, `sound.<row>` replace `notify` and `meetings`.

## Open questions

- (none)

## Decisions

- QtMultimedia instead of `pw-play` / `paplay` / `aplay` — `paplay` is absent here and `aplay` cannot
  decode `.oga`; QtMultimedia ships with PySide6 and needs no player on colleagues' machines.
- Custom paths stay on the native side — a page that cannot hand over a path cannot make the window
  read an arbitrary file.
- Sound and notification independent per row — the point of the table (option A).
- First sound of a burst wins, not the "most important" — no priority order to explain.
- DND read at play time — no watcher to keep in sync; one `xfconf-query` call per played sound at most.
