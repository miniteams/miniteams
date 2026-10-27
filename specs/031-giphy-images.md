# 031 — Show Giphy GIFs inline

**Status**: shipped
**Requested by**: babs
**Date**: 2026-10-21

## Problem

GIFs picked in Teams' Giphy panel arrive as `<img src="https://mediaN.giphy.com/…">`. The chat page's
CSP only allows images served by miniteams, so since #71 they show as a `[image: <alt>]` link, never
as the picture. They are the only external image host in the archive (Teams emojis and asyncgw images
are already shown).

## Solution

A Giphy image is shown inline, taken from the first source that has it:

1. **Archive** — the archiver, in media mode, keeps a copy like any other image.
2. **Web** — else the web fetches it from Giphy and serves it, as it does for unarchived Teams images
   (#69).
3. **Browser** — else the page loads it straight from Giphy (CSP allows `https://*.giphy.com`).

The `[image: <alt>]` link stays as the last fallback when none loads.

## Scope

- **Hosts.** Only `https://media[0-9]*.giphy.com` and `https://i.giphy.com` paths ending in an image
  file. No other external host is fetched, proxied or allowed.
- **Archive (C).** `attachments.extract` takes Giphy images as items: downloaded once into the chat's
  media folder (no token sent), honouring `--no-media` and `--media` types (`images`), in the one-shot
  archive, the live archiver and `--verify-media`. Same naming as other media so `existing()` finds it.
- **Web (B).** `/media/<thread>/<name>?giphy=<path>` (or equivalent) serves the archived copy, else
  fetches `https://<giphy host>/<path>` server-side: host rebuilt from an allowlist, never a page URL
  passed through; image types only (gif, webp, png, jpeg), size cap, the #69 concurrency, deadline
  and failure memo. No cookies or token on the request. Nothing written to the archive.
- **Browser (A).** CSP `img-src` gains `https://*.giphy.com`; when `/media` fails (the live
  fetch at once, as its failure memo would answer the #69 retries; the archived copy after them), the
  image switches to the Giphy URL with no referrer (page-wide `Referrer-Policy: no-referrer`), then
  to the link.
- **Render.** A Giphy `<img>` renders as an `<img>` (width/height/alt kept), not as the link.
- **Animated Teams GIFs.** When several archived files share an object id (`.gif` and `.full.png`),
  `/media` serves the animated one first (`.gif`, then other originals, `.full.*` last), whatever the
  directory order. The #69 live fetch keeps a GIF animated: it asks the view the message's `src` names
  (`imgpsh_mobile_save_anim` for a pasted GIF; `imgo` is a still), passed as `&view=`.

## Decisions

- **Naming.** A Giphy image is archived as `giphy-<id>.<ext>`: `<id>` from its path (`/media/[v1.<blob>/]<id>/…`,
  or i.giphy.com's `/<id>.<ext>`), `<ext>` from the served content type. Render and web find it from the `src`
  alone (`attachments.giphy` → `existing`). Extract kind `giphy`, type `images`.
- **The src's view.** A pasted Teams GIF is archived as `<id>.gif` (the view its `src` names, animated) +
  `<id>.full.png` (`imgpsh_fullsize`, a still); `imgo` answers a still JPEG (checked live on 2 GIFs). The live
  fetch asks the `src`'s view, `[a-z0-9_]{1,40}` (else `imgo`), in the URL and the failure memo key.
- **Giphy stills.** `_s` renditions (`giphy_s.gif`, `480w_s.jpg`) are not Giphy images here: under the GIF's
  stem a still archived first would be served for it. They stay links.

## Out of scope

- Other external image hosts.
- Giphy in Adaptive Cards.
- Sending GIFs from the composer.

## Acceptance criteria

- [x] **AC1** A message with a Giphy `<img>` shows the GIF inline in the chat page, live and from
  history.
- [x] **AC2** With media on, the archiver saves each Giphy image once; the page then serves the
  archived copy without contacting Giphy.
- [x] **AC3** Unarchived, the web fetches it from Giphy (allowlisted host, image types, size cap, no
  token); any other host or a non-image answer is refused.
- [x] **AC4** When the web cannot serve it, the page loads it from Giphy directly (no referrer); when
  that fails too, the `[image: <alt>]` link shows.
- [x] **AC5** No other external host is fetched by the archiver or the web, nor allowed by the CSP.
- [x] **AC6** A pasted Teams GIF archived as `.gif` + `.full.png` is always served as the `.gif`; an
  unarchived one fetched live by the web stays animated.

## Phases

1. **Render + web + CSP** (A, B) and Teams GIFs: AC1, AC3, AC4, AC5, AC6.
2. **Archive** (C): AC2.
