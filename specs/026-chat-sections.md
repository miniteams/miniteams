# 026 — Chat sections

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-17

## Problem

The chat list (widget and the chat page's split list) is one flat list, newest activity first. Chats
that matter keep sinking under busy ones, and nothing groups them by topic the way Teams' sections do.

## Solution

The user makes named sections, files chats into them, and switches the list between **Recent** (the
flat list of today) and **Sections** (the chats grouped under collapsible section headers). Sections are
miniteams' own; Teams does not see them.

## Scope

- **Model.** Named, ordered sections, saved by the server (like `muted.json`), shared by every view. A
  chat is in at most one section; chats in none go to **Other**, always last. Deleting a section moves
  its chats to Other.
- **Order inside a section.** Each section is ordered **Recent** (default, newest activity first) or
  **Manual**, set in its header's ⋯. In a Manual section an arrival never moves a row, a newly filed chat
  goes on top, and chats are reordered by drag and drop or Alt+↑/↓. Other is always Recent.
- **Mode.** A Recent / Sections button in the list header, shown only once a section exists. The mode
  is remembered per view (widget, chat page), on its own preference key: today the ⚙ pages share keys.
- **Tabs and mode are independent.** The tab (Inbox, @, Unread, Seen, Muted) decides which chats show;
  the mode decides how they are arranged. A chat marked seen leaves its section in Inbox, like it leaves
  the list today.
- **Empty sections.** Shown in every tab, even with no chat to show (a drop target). Hidden while a
  search (quick filter or Ctrl+K) runs, except those holding a result.
- **Accordion.** A section header folds and unfolds its chats. Folded, it shows the section's unread
  count and an `@` when one of its chats mentions the user, by the tabs' rules; an arrival does not
  unfold it. Folded state is remembered per view. A search unfolds the sections holding a result for its
  duration, without changing the remembered state.
- **Managing, by menu.** A chat's ⋯ gets **Section ▸** (the sections, **None**, **New section…**). A
  section header's ⋯: rename, Recent / Manual order, move up, move down, delete.
- **Managing, by drag and drop.** A chat dropped on a section header (folded or not) or on a chat of
  that section files it there (in a Manual section, at the drop position); dropped on Other, it leaves its
  section. Dropped among its own section's chats, it moves there if the section is Manual. A header dragged between two
  sections reorders them; Other stays last. Sections are created by the menu only.
- **Keyboard.** ↑/↓ walk chats and section headers; a folded section is skipped as one step. On a
  header, Enter, ← and → fold and unfold. ← on a chat goes to its section's header. Alt+↑/↓ moves a chat
  within a Manual section.
- **Sync.** A change made in one view shows in every open view without a reload.

## Out of scope

- Importing or syncing Teams' own sections or pins (see Open questions).
- A chat in several sections.
- Per-view sets of sections.

## Acceptance criteria

- [ ] **AC1** With no section, the list looks as today and the mode button is absent; creating a
  section from a chat's ⋯ → **New section…** shows the button and files the chat there.
- [ ] **AC2** In Sections mode, chats are grouped under their section headers in the saved section
  order, by recency inside each, chats with no section under Other, last.
- [ ] **AC3** Recent / Sections switches the arrangement; each view remembers its own mode across
  reloads, and switching one view leaves the other unchanged.
- [ ] **AC4** The tabs filter as today in both modes; a chat marked seen leaves its section in Inbox and
  shows under it in Seen.
- [ ] **AC5** A section with no chat to show stays shown in every tab; during a search only sections
  holding a result show, unfolded, and their remembered fold state is back once the search is cleared.
- [ ] **AC6** A folded section shows its unread count and `@` by the tabs' rules, stays folded on an
  arrival, and its fold state is remembered per view.
- [ ] **AC7** From the menus: file a chat in a section or None, create, rename, move up/down and delete
  a section; deleting moves its chats to Other.
- [ ] **AC8** By drag and drop: a chat onto a header (folded too), onto a chat of another section, or
  onto Other files it accordingly; a header dragged between sections reorders them; Other stays last.
- [ ] **AC9** ↑/↓ walk headers and chats, skipping folded sections' chats; Enter, ←, → fold and unfold
  a header; ← on a chat reaches its header; the list's other keys and Ctrl+K behave as before.
- [ ] **AC10** A change made in one view shows in another open view without a reload, and survives a
  server restart.
- [ ] **AC11** A section name is plain text everywhere (no markup), non-empty, and bounded in length; a
  chat id unknown to the board is refused.
- [ ] **AC12** A section set to Manual keeps its order when a message arrives; a newly filed chat lands on
  top; Alt+↑/↓ moves a chat within it; the order survives a reload and shows in every view; switching back
  to Recent orders by activity again.
- [ ] **AC13** In a Manual section, a chat dragged among its chats lands at the drop position.

## Phases

### Phase 1 — Sections by menu
- Work: server model and verbs (create, rename, move, delete, file a chat), saved file, broadcast; list
  rendering in Sections mode for the widget and the split list; mode button; accordion with badges;
  per-view preferences; search interplay; keyboard.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6, AC7, AC9, AC10, AC11, AC12
- **Data model impact**: new `sections.json` in the config dir (ordered sections: id, name, order, chat ids)
- **DoD**: server unit tests for the verbs, the file and its validation; browser tests per AC in Chrome
  and Firefox, widget and split list; full suite and pre-commit green.

### Phase 2 — Drag and drop
- Work: drag a chat onto a header, a chat or Other; drag a header to reorder; drop targets shown.
- **Covers**: AC8, AC13
- **Data model impact**: none
- **DoD**: browser tests for each drop in Chrome and Firefox; full suite and pre-commit green.

## Data model impact (summary)

`sections.json` in the config dir: the ordered sections, each an id, a name, its order (recent | manual)
and its chat ids (in manual order when Manual). No
archive change.

## Open questions

None blocking. Later, outside this spec: a read-only look (no Graph) at where Teams keeps its sections;
if readable, a **Teams sections** mode gets its own spec, alongside the local ones.

## Decisions

- Local sections, one set for every view; the mode and fold state per view — user's calls (2026-10-17).
- Tabs and mode independent; seen chats leave their section in Inbox; one section per chat; menus and
  drag and drop both in v1; recency inside a section; accordion with badges, no auto-unfold; mode button
  in the list header; empty sections always shown, hidden by a search — user's calls (2026-10-17).
- A folded section's badge counts only the chats the current tab and search show — user's call
  (2026-10-17).
- Manual order inside a section, per section, no separate edit mode — user's call (2026-10-17, review
  comment).
- "Other" for the implicit last section, in English like the rest of the UI — overturnable.
- Drag and drop in its own phase so the menus ship first — overturnable.
