# 013 — Reaction tooltips

**Status**: shipped
**Requested by**: user
**Date**: 2026-10-13

## Problem

Chat reactions show a count and an internal key on hover, so the user cannot tell who reacted.

## Solution

Hovering any reaction shows every reaction on that message, grouped in their displayed order by emoji with the names of the people who added each, using the existing directory and authenticated chat connection.

## Acceptance criteria

- Hovering any reaction shows all reactions on the message, one line per emoji in displayed order.
- Each line lists only that emoji's participants; live updates refresh the list.
- An unresolved participant uses their existing identifier as a fallback.
- Reaction counts and add/remove behavior stay consistent.
- Names are rendered as plain text. No new storage or external request on hover.

## Phase 1 — Supply participants and display tooltips

- Work: include participant names in chat reaction records and use them in the existing native tooltip.
- **Data model impact**: additive reaction payload field only; reuse existing archived participant IDs and directory names, with no migration or additional personal-data storage.
- **DoD**: reaction payload tests and a browser tooltip test pass; pre-commit passes.

## Validation

- Python suite: 1,073 passed with the personal `.env` excluded from the test process.
- Browser suite: 249 passed in headless Chrome.
- Pre-commit passed.
- Live Chrome check: every chip on an existing message showed the same two emoji groups and their participants. No message or reaction was sent.
