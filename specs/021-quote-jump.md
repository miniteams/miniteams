# 021 — A reply's quote leads to the quoted message

**Status**: in progress
**Requested by**: babs
**Date**: 2026-10-16

## Problem

A reply in the chat page shows the message it quotes, but reaching that message means scrolling up
or searching for it by hand.

## Solution

Clicking a reply's quote, or pressing Enter on it, brings the quoted message into view and highlights
it, loading the older history around it when it is not on screen, like a search hit.

## Scope

- The server keeps the quoted message's id on the quote it renders (`data-msg`, digits only).
- The quote is a link-like target: pointer cursor, focusable, `role="link"`, Enter or a click.
- On screen: scroll to it and highlight it. Not on screen: the page's existing jump (history around
  the message's time, from its id) and the same highlight.
- Not found (deleted, older than the history Teams serves): a short "quoted message not found" on
  the error line.

## Out of scope

- The widget (it shows no quotes).
- Quotes in the composer or the editor (already-shown chips, not message content).

## Acceptance criteria

- [ ] **AC1** A rendered reply quote carries the quoted message's id; a non-numeric id is dropped.
- [ ] **AC2** Clicking a quote whose message is on screen scrolls to it and highlights it, without a
  history request.
- [ ] **AC3** Clicking a quote whose message is not loaded asks the history around it and highlights
  it once shown.
- [ ] **AC4** Enter on a focused quote does the same as a click; the quote is reachable by Tab.
- [ ] **AC5** A quote whose message cannot be found says "quoted message not found".

## Phases

### Phase 1 — Quote jump
- Work: `data-msg` in render.py; click/Enter handling and focusability in chat.html, reusing the
  search jump.
- **Covers**: AC1, AC2, AC3, AC4, AC5
- **Data model impact**: none
- **DoD**: unit test for the rendered attribute; browser tests per AC in Chrome and Firefox; full
  suite and pre-commit green.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- The message's time comes from its id (Teams message ids are epoch milliseconds) — because the
  quote carries no timestamp and the existing jump loads by time.
