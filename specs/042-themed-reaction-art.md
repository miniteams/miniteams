# 042 — Teams art for themed reactions

**Status**: shipped
**Requested by**: babs
**Date**: 2026-11-12

## Problem

Teams gives several emoticon ids the same Unicode character and tells them apart only by its own
art. `rock` (a smiley making the horns) and `1f918_signofthehorns` (the hand) are both 🤘 in Teams'
catalog, so a message carrying both shows two identical 🤘 chips in the chat page. 102 ids are in
that case (`laughcat`, `coolkoala`, `sadrobot`, `hungover`…): every themed variant of a character
whose plain id is another one.

## Solution

A reaction chip, or an `<emoji>` in a message body, whose id is a themed variant shows Teams' own
picture for it, as Teams does. The character stays as the picture's alt text, tooltip and fallback.

## Scope

- Themed id = a `TEAMS_EMOJIS` id whose displayed character the page also shows for another id, and
  which is not the name miniteams sends for that character (`like` for 👍, so `yes` is themed).
- Pictures come from Teams' public static CDN
  (`statics.teams.cdn.office.net/evergreen-assets/personal-expressions/v2/assets/emoticons/<id>/default/30_f.png`),
  fetched once by the server, cached under the cache dir, served by a local route.
- Reaction chips in the chat page; `<emoji id="…">` in rendered message bodies.

## Out of scope

- The Qt widget: it shows no reactions.
- Picking a themed id from the reaction picker or the composer.
- Animated art, tone-specific art: a toned themed id shows its base's default art only per AC6.
- Art for plain ids: their character already says it.

## Acceptance criteria

- [x] **AC1** Given a message with `rock` and `1f918_signofthehorns` reactions, the chat page shows
  the `rock` chip with a picture and the horns chip with 🤘; both keep 🤘 as alt and in the tooltip.
- [x] **AC2** The picture route serves only themed ids: any other id, a path-like or overlong id gets
  404 without a network request.
- [x] **AC3** A picture is fetched from the CDN once, then served from the local cache, also after a
  restart.
- [x] **AC4** When the CDN fails, the chip shows the character, and the id is not re-fetched before
  the failure TTL.
- [x] **AC5** A body `<emoji id="rock">` renders the picture with alt 🤘; `<emoji id="like">` still
  renders 👍 as text.
- [x] **AC6** A toned themed id (`<id>-tone3`) shows the default art only when its toned character is also
  shared by another id (`gran-tone3` 👵🏽 like `oldwoman-tone3`, `dropthemic-tone3` 🎤 like
  `1f3a4_microphone`); else its toned character (`yes-tone3` → 👍🏽).

## Phases

### Phase 1 — Themed art route and rendering
- Work: themed-id set derived from `TEAMS_EMOJIS`/`TEAMS_PLAIN`; cached fetch on the existing
  `Downloads` pattern of `emojis.py`; route `/teams-emoji/<id>`; `src` on themed reaction records;
  `<img class="emoji">` for themed body emojis; chip image `onerror` falls back to the character.
- **Covers**: AC1, AC2, AC3, AC4, AC5, AC6
- **Data model impact**: none (new files under the cache dir only)
- **DoD**: `uv run pytest` green with tests for AC1–AC6; chat page screenshot (light, dark, narrow)
  shows the `rock` picture next to the 🤘 chip on a live message.

## Open questions

- [ ] none

## Decisions

- No etag in the URL or the generated table: the CDN ignores `?v=` (probed: `?v=v1` and no `v`
  return the same 1683-byte PNG), and the art rarely changes. Cache keyed by id only — overturnable.
- 30 px art (`30_f.png`), the size Teams uses for chips — because the chip shows it at text height.
- Fetching from Microsoft's public CDN sends no token; it reveals only which ids are seen, as the
  Teams client does.
