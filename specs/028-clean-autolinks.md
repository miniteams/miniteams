# 028 — Bare URLs sent as clean links

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-20

## Problem

A bare URL typed or pasted in the chat page's composer (send, edit) leaves as plain text: how
it gets linked, trailing punctuation included or not, is left to each Teams client. In miniteams' own view the same bare URL is not clickable at all.

## Solution

At send, `compose_html` writes each bare URL as a real link, `<a href="url">url</a>`, with the trailing
punctuation left out of both the address and the link text. The chat page renders bare URLs of
received messages as links by the same rule.

## Scope

- **Send and edit.** Every message the composer builds (send, edit, scheduled send, the preview) turns a
  bare `http(s)://` URL into a link. `[label](url)` links are unchanged.
- **Trailing punctuation.** Stripped from the end, repeatedly: `. , : ; ! ? * _ ~ ' "`, their Unicode kin `… – — « » ‹ › “ ” ‘ ’`, and closing
  `) ] } >`. A closing `)`, `]` or `}` stays when it closes one opened inside the URL (GitHub's rule):
  `https://fr.wikipedia.org/wiki/Foo_(bar)` keeps it, `(voir https://test.com/a)` does not.
  Inside the URL a `'` is kept only before a letter or digit (`Hitchhiker's_Guide`), and a `'` inside the host
  ends the URL (`https://x.com's API`).
- **Round trip.** Editing a sent message gives the bare URL back (a link whose text is its address
  reads as the address, as today), so re-sending it yields the same link.
- **Display.** A bare `http(s)://` URL in a received or archived message's text renders as a link in the
  chat page (new tab, `noopener`), cut by the same rule. Text inside `<a>`, `<code>` and `<pre>` is left
  alone.

## Out of scope

- The MCP `send_message` tool (plain text path, `send.message_html`).
- URLs without a scheme (`www.test.com`, `test.com`).
- The widget's list previews (plain text).

## Acceptance criteria

- [x] **AC1** `voici https://test.com/.` sends `voici <a href="https://test.com/">https://test.com/</a>.`;
  same for each of `. , : ; ! ? * _ ~ ' " ) ] } >`, the Unicode ones above, and runs of them (`https://test.com/a).`).
- [x] **AC2** A balanced `)`, `]` or `}` inside the URL is kept; an unbalanced trailing one is not.
- [x] **AC3** Editing a message sent with such a link shows the bare URL followed by its punctuation;
  saving it unchanged posts the same HTML.
- [x] **AC4** A received message whose text holds a bare URL shows it as a link cut by the same rule;
  URLs already in `<a>`, `<code>` or `<pre>` are untouched.

## Phases

1. **Send.** `compose.py`: the bare URL token becomes a link with the trailing cut; decompose round trip.
   Done when AC1–AC3 have tests going red without the change.
2. **Display.** `render.py`: bare URLs in text nodes become links with the same cut (one shared helper).
   Done when AC4 is tested, and the chat page shows it in a browser test.
