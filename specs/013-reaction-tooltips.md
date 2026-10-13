# 013 — Reaction tooltips

**Status**: shipped
**Requested by**: user
**Date**: 2026-10-13

## Problem

Chat reactions show a count and an internal key on hover, so the user cannot tell who reacted.

## Solution

Hovering any reaction shows every reaction on that message, grouped in their displayed order by emoji with the names of the people who added each, using the existing directory and authenticated chat connection.

## Acceptance criteria

- Hovering any reaction shows all reactions on the message, one line per reaction in displayed order.
- Each line lists only that reaction's participants; live updates refresh the list.
- An unresolved participant uses their existing identifier as a fallback.
- Reaction counts and add/remove behavior stay consistent.
- Names are rendered as plain text. No new storage or external request on hover.
- Hexadecimal Unicode keys (`1f680_rocket`, `1f469-200d-1f4bb_technologist`) display their code
  points. Controls, surrogates, unassigned, format and line/paragraph separator characters fall back
  to `:key:`, except the ZWJ and tag characters that emoji sequences need.
- Teams reaction names (`launch`, `fistbump`, `hearteyes`…) map to the composer's shortcodes only when
  the meaning is unambiguous; any other name shows as `:key:`.
- A `-tone1`..`-tone5` suffix applies that skin tone to a single toneable emoji; other toned keys
  (multi-person, already toned, unknown base, tone outside 1–5) show as `:key:`.
- `yes`/`no` display 👍/👎: in the archive only `yes` carries skin tones (Teams' picker thumbs up),
  `like` never does. The chat page's picker offers the other 6 keys, so 👍 is not offered twice.
- Custom org reactions (`partyparrot;0-frs-d1-<hash>`) show their name, `:partyparrot:`.

## Phase 1 — Supply participants and display tooltips

- Work: include participant names in chat reaction records and use them in the existing native tooltip.
- **Data model impact**: additive reaction payload field only; reuse existing archived participant IDs and directory names, with no migration or additional personal-data storage.
- **DoD**: reaction payload tests and a browser tooltip test pass; pre-commit passes.

## Validation

- Python suite: 1,122 passed, browser suite: 256 passed in headless Chrome; ruff and mypy clean.
- Archive check (read-only, 39,883 reactions, 707 distinct keys): 1,310 still show `:key:`, 621 of
  them custom org reactions; 436 carry a skin tone, 343 of them on `yes`, none on `like`.
- Live Chrome check: every chip on an existing message showed the same two emoji groups and their
  participants. No message or reaction was sent.
