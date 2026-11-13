# 043 — Emoji search inside names

**Status**: shipped
**Requested by**: babs
**Date**: 2026-11-13

## Problem

The emoji search (`:` completion, the picker's "…", the reaction slots) matches name prefixes only:
`horns` finds nothing, though `signofthehorns` 🤘 and `angryfacewithhorns` 👿 exist.

## Solution

From 3 characters, a query also matches inside a name. Prefix matches stay first. The typed part
shows in bold in the list, so it is clear why an item is there.

## Acceptance criteria

- [x] **AC1** `horns` lists 🤘 `signofthehorns` and 👿 `angryfacewithhorns`; built-in and custom names alike.
- [x] **AC2** Prefix matches list before inside matches (`fire` still starts with `fire`, 🔥 …).
- [x] **AC3** Under 3 characters, only prefixes match (`af` lists no `…af…` names).
- [x] **AC4** Each item shows the typed part in bold, any case (`:signofthe`**`horns`**`:`).

## Phase 1 — Match inside, bold the match
- Work: `_emoji_items` matches inside from 3 characters, prefix first; the completion list bolds the
  typed part.
- **Covers**: AC1, AC2, AC3, AC4
- **Data model impact**: none
- **DoD**: `uv run pytest tests/test_web_complete.py` and the compose browser tests green.

## Decisions

- 3-character floor: one or two letters inside names match most of the table — overturnable.
- Teams keywords (`rock` has "horns") are not searched: they are not in the generated table.
