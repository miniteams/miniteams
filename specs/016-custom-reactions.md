# 016 — Custom reaction picker

**Status**: approved
**Requested by**: babs
**Date**: 2026-10-13

## Problem

The chat page's reaction picker offers 8 fixed reactions (Teams' 6 defaults plus 🙏 and 🙃), and
only chips of those keys toggle. Reacting with anything else means opening Teams.

## Solution

The picker's 8 slots become favorites, set from a "Reactions" row of the chat page's ⚙ menu through
the existing `:emoji` completion (Unicode and the org's custom emojis); the choice lives in the
browser. A "…" button next to the favorites opens the same emoji search to react with any emoji at
once. Every reaction chip under a message toggles, whatever its emoji.

## Scope

- **Setting.** ⚙ menu of the chat page, "Reactions": 8 slots in picker order; a click on a slot
  opens the `:emoji` completion (spec 012) to replace it; reorder with arrow buttons; "Reset"
  restores the default (👍 ❤️ 😆 😮 😢 😡 🙏 🙃). Stored in `localStorage` like the other ⚙ settings,
  per browser; an unreadable stored value falls back to the default.
- **Keys sent.** The Teams name when Teams has one for that emoji (`like`, `praying`,
  `upsidedownface`… — the names `messages.reaction_emoji` already resolves), else the hex Unicode
  key Teams accepts (`1f525_fire`, the form the CLI `react` already sends), or the org custom emoji
  key (`<name>;<id>`, as listed by `emojis`).
- **Verb.** The page's `react` verb accepts any key of those three shapes, validated by format
  (known Teams name, hex code points with an optional `_name`, custom `<name>;<id>`), length-capped;
  anything else stays `invalid reaction`.
- **Chips.** Every chip toggles: a click adds my reaction with the chip's key, or removes it when I
  already gave it.
- **Picker.** The 4×2 grid of favorites stays; custom emojis show as their image (`/emoji/<id>`).
  A "…" button at the right of the grid, spanning both rows, opens the `:emoji` search; the pick is
  sent as my reaction at once and does not change the favorites; Escape closes it.

## Out of scope

- Server-side storage or syncing the list between browsers.
- More or fewer than 8 slots.
- Reactions in the widget (it has none).

## Acceptance criteria

- [ ] **AC1** The ⚙ "Reactions" row shows the 8 current slots; replacing a slot through the emoji
      completion, reordering and "Reset" update the picker at once and survive a reload.
- [ ] **AC2** A Unicode emoji Teams names (🙏) is sent with its Teams name; one it does not (🦊) with
      its hex key; an org custom emoji with its custom key; each verified once live in the
      "test miniteams" chat (react then remove).
- [ ] **AC3** The `react` verb accepts the three key shapes and refuses anything else
      (`invalid reaction`), including over-long keys and markup.
- [ ] **AC4** Every chip under a message toggles my reaction with its own key, custom and toned
      keys included.
- [ ] **AC5** A corrupt or partial stored list falls back to the default without breaking the page.
- [ ] **AC6** "…" opens the emoji search from the picker; a pick (Unicode or custom) is sent as my
      reaction with the key rules of AC2 and leaves the favorites unchanged; Escape closes it.

## Phases

### Phase 1 — custom picker and open chips
- Work: setting row, storage, key mapping, verb validation, chip toggling, picker rendering of
  custom emojis, README.
- **Covers**: AC1–AC6
- **Data model impact**: none (browser `localStorage`).
- **DoD**: server tests (key validation), browser tests (setting, reload, reset, chips, fallback),
  live check of the three key shapes in the "test miniteams" chat; `pre-commit run --all-files` rc=0.

## Open questions

None.

## Decisions

- The list lives in the browser, like the other ⚙ settings (babs, 2026-10-13).
- Any Unicode emoji and the org's custom emojis (babs, 2026-10-13).
- 8 slots, default = the current picker, with a reset (babs, 2026-10-13).
- Set from the chat page's ⚙ menu (babs, 2026-10-13).
- Every chip toggles, like in Teams (babs, 2026-10-13).
- The 8 slots are favorites; "…" opens the full emoji search to react with any emoji (babs, 2026-10-13).
