# 003 — Widget redesign + mention highlight

**Status**: shipped
**Requested by**: babs
**Date**: 2026-09-17

## Problem

The widget (spec 002) is plain: a flat list with no visual hierarchy and no dark theme, so the
chats that need attention don't stand out. It also misses what matters most: a message that
mentions me looks like any other, and a mention added by editing an older message after I read
the chat doesn't show at all.

## Solution

Same page and data, new look, picked from the prototype (layout "A" with "C"'s stripes): an
inbox-style list with initials avatars, a coloured left stripe on chats needing attention, tabs
instead of checkboxes, and a ⚙ options menu (light/dark theme following the system by default,
avatars on/off for a denser list). Chats where
someone mentioned me get a light-blue highlight and an `@ you` tag, including when the mention
was added by editing an older message; an `@everyone` mention gets a paler version with an `@ all`
tag, so a direct mention still stands out. The highlight goes away when I mark the chat seen or read
it in Teams after the mention.

## Scope

- **Mentions (server)**
  - "Me" = `8:orgid:<oid>`, `oid` read from the AAD access token claims (verified: present, equals
    my MRI).
  - A message mentions me when `properties.mentions` (JSON string or list) holds an entry with
    `mentionType: "person"` and my MRI (`kind: "me"`), or `mentionType: "everyone"` (`kind:
    "all"`; its `mri` is the thread itself and is not checked — the message is in this thread). Archive: 30,273 `person`, 1,118 `everyone`; `bot` and
    `share-contact` entries are ignored.
  - Row gains `mention: {kind, by, text, at, edited}` (or `null`): `at` = `edittime` when the
    message was edited, else `composetime`; `text` = stripped snippet of the mentioning message.
    While uncleared, a `me` mention is kept over a newer `all` one.
  - Live: `NewMessage` and `MessageUpdate` edits on **any** message of an in-scope thread set it
    (today edits of a non-last message are dropped, `web.py:404`); an edit that removes the
    mention clears it only if that message set it. Otherwise the newest mention wins, within the
    `me` over `all` rule above.
  - Clears when `seen_at >= mention.at` or when Teams' read marker timestamp
    (`consumptionhorizon` 2nd field, time of reading — verified ≥ message id on 400/400 chats)
    is `>= mention.at`. `seen` stamps `max(last_activity, mention.at)` so it always clears it.
  - Bootstrap: for each bootstrapped row, one small history page (last 20 messages) is scanned
    for mentions newer than the read-marker timestamp and the seen stamp. Bounded fan-out like
    labels.
  - Row labels omit my own name from roster-built labels (1:1 and untitled groups).
- **Page (`widget.html`)**
  - Layout A: avatar (initials, colour from thread id; round for 1:1, badge 👥/📅 for group/meeting),
    name, last message line; top-right the time (always visible), with the seen/unseen button
    appearing beside it on hover (no reserved column); under it the unread dot or `@ you` tag
    (`@ you · edited <ago>` for a mention added by edit).
  - Left stripe: chat hue on unread rows; light blue + tinted background on `me`-mentioned rows,
    paler blue stripe + lighter tint and an outlined `@ all` tag on `all`-mentioned rows; none on
    read rows.
  - Header on one line: `miniteams`, tabs with counts, live status dot (red when disconnected,
    word in its tooltip), ⚙ button. Tabs: Inbox (not seen, or mentioned) · `@` (mentions, `me`
    and `all`) · Unread · Seen; replaces the "unread only" / "show seen" checkboxes. Below
    ~420px the tabs scroll sideways. Selected tab remembered per browser.
  - ⚙ menu (closes on outside click / Escape), choices remembered per browser:
    - Theme: Auto (system) / Light / Dark. Mention colour `#0284c7` (light) / `#7dd3fc` (dark,
      dark text on the tag).
    - Show avatars (default on). Off = compact rows: no avatar column, tighter padding, a
      hairline separator between rows (hidden next to a hovered or mentioned row); stripes, time,
      dot and tags unchanged.
  - Existing behaviour kept: live push, typing line, open on click (opener / Ctrl-click
    browser), `status` live/disconnected, page auto-reload.

## Out of scope

- Bot mentions (`bot`/`BOT`) and shared contact cards (`share-contact`). Team/channel/tag mentions
  don't occur in chats (none in the archive).
- Mentions added by edit **while `web` was not running**, on chats outside the last 20 messages or
  already read before restart: bootstrap only sees the last history page.
- Persisting mention state across restarts (recomputed at bootstrap).
- Notifications, sounds, badge counts outside the page.
- Real profile pictures in avatars (initials only).
- Layouts B (terminal) and C (triage).

## Acceptance criteria

- [ ] A live `NewMessage` mentioning me on a chat sets `mention` on its row; the page shows the
      light-blue stripe, tinted row and `@ you` tag, and the row appears under **@ Mentions**.
- [ ] A live `MessageUpdate` that adds my mention to an **older** (non-last) message of an
      already-read chat sets `mention` with `edited: true` and `at` = its `edittime`; the row's
      last-message line is unchanged otherwise.
- [ ] An `@everyone` mention (`mentionType: "everyone"`, `mri` = thread id) sets `mention.kind:
      "all"`; the page shows the paler stripe and `@ all` tag; a later direct mention upgrades it
      to `me`, a later `@everyone` does not downgrade a `me`.
- [ ] A mention of someone else, a bot mention or a shared contact card sets nothing.
- [ ] Clicking seen on a mentioned row clears the mention and it stays cleared across page reload
      and process restart.
- [ ] A `ConversationUpdate` whose read-marker timestamp is `>=` `mention.at` clears it; one
      older than `mention.at` does not.
- [ ] At startup, a chat whose last 20 messages contain a mention newer than both its read-marker
      timestamp and its seen stamp comes up mentioned; one read after the mention does not.
- [ ] 1:1 labels no longer include my own name.
- [ ] With the system in dark mode and no choice made, the page renders the dark palette;
      the ⚙ theme choice overrides it and survives reload.
- [ ] Turning "Show avatars" off removes the avatar column and adds row separators; the choice
      survives reload.
- [ ] The time is visible on every row, including unread and mentioned ones; the seen button
      only appears on hover and takes no space otherwise.
- [ ] Tabs filter as described and show counts; the chosen tab survives reload.
- [ ] `stream` / `chats` / `archive` output unchanged (`uv run pytest` green).

## Phases

### Phase 1 — Mention detection (server)
- Work: `me` MRI from the access token; `mentions_me(resource, me)` pure helper; `mention` on
  rows from live `NewMessage` / `MessageUpdate` (any message of the thread); clearing on seen and
  on read-marker timestamp; bootstrap history scan; self removed from roster labels. First step:
  capture one real live edit that adds a mention (`--log-level DEBUG` event dump) to confirm
  `properties.mentions` and `edittime` travel in the `MessageUpdate`.
- **Data model impact**: none (`seen.json` format unchanged; the seen stamp can now be the
  mention time).
- **DoD**: live capture confirms the edit payload; `uv run pytest tests/test_web.py` green —
  one test per mention acceptance criterion above, each red when its code is removed.

### Phase 2 — Page redesign
- Work: `widget.html` rewritten to layout A with stripes, tabs, theme toggle, mention rendering,
  hover seen button; same websocket protocol plus the `mention` field.
- **Data model impact**: none (browser `localStorage`: tab, theme, avatars).
- **DoD**: `uv run pytest` green; headless screenshots in light and dark on real rows show time
  on every row and the mention styling, with avatars on and off; manual check on the running
  widget: tab, theme and avatars survive reload, ⚙ menu closes on outside click / Escape, hover
  button, click-to-open still works.

## Addendum — mark read in Teams (2026-09-17)

A `👁` button next to `✓` (hover) tells Teams the chat is read: `PUT
/v1/users/ME/conversations/<thread>/properties?name=consumptionhorizon` with
`{"consumptionhorizon": "<last_id>;<now_ms>;<last_id>"}` (purple-teams shape; the third field is
the client message id, which the row does not know — the message id is accepted in its place,
verified live). Teams then answers with a `ConversationUpdate`, which the widget already folds
(unread off, mention cleared). Distinct from **seen**, which stays private to the widget; "read" is
visible on every device (and read receipts where the tenant shows them).

- [ ] Clicking `👁` sends `{"read": id}`; the server PUTs with the row's `last_id`; a failed call
      is logged and leaves the row unchanged.
- [ ] A row without `last_id` has no `👁`.

## Data model impact (summary)

None. No new files; `seen.json` unchanged in shape; page preferences in browser `localStorage`.

## Open questions

- (none)

## Decisions

- New spec 003 instead of amending 002 — 002 is shipped; this changes its look and adds a feature.
- "Me" from the access token `oid` (the silent refresh returns no `id_token`), not a config value.
- Mention time = `edittime` when present: an edit keeps the original id/`composetime`, so an
  id-based comparison would treat a late-added mention as already read.
- Read-in-Teams clearing compares the read marker's *timestamp* field, not its message id, for the
  same reason (user choice: clear on seen **or** Teams read).
- A re-edit of a message that already mentioned me (even a typo fix) re-surfaces the mention,
  dated by the edit: the content changed after I saw it. Overturnable if it proves noisy.
- Bootstrap scans 20 messages per row: enough for the usual unread backlog, bounded cost on the
  already rate-limited startup.
- `@everyone` highlighted in a lighter style than a direct mention (user choice 2026-09-17): it
  matters, but less than being named.
- Compact mode drops the 1:1 / group / meeting badge along with the avatar — overturnable (a
  small glyph before the name) if it's missed.
- Initials avatars only: profile pictures need extra fetches and caching for little glance value.
- Prototype and verdict: `.claude/scratchpad/widget-prototype/` (`NOTES.md`), deleted once phase 2
  ships.
