"""Tab and Shift+Tab indent list items (spec 041) on the chat page's composer and the message editor, against
the composer files' fake chat socket. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_list_tab.py`.
"""

import re
from collections.abc import AsyncIterator

import browser_helpers
import pytest
from browser_helpers import ANN, Pages, shows
from test_browser_chat_compose import Fake, _open, _parts, _pick, _select, _selection
from test_browser_edit import _edit
from test_browser_edit import _select as _select_editor
from test_browser_list_keys import _PICKS, _anchors, _press, _send

from miniteams.compose import compose_html

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)


async def _board(pages: Pages, init: str = "") -> AsyncIterator[Fake]:
    board = Fake()
    page = await _open(pages, board, init=init)
    await page.locator("#reply").focus()
    yield board
    await pages.close()
    assert pages.external == []


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    async for board in _board(pages):
        yield board


@pytest.fixture
async def hooked(pages: Pages) -> AsyncIterator[Fake]:
    """The composer with its raw pick anchors readable (`_anchors`)."""
    async for board in _board(pages, _PICKS):
        yield board


async def _focused(fake: Fake) -> str:
    assert fake.page is not None
    return str(await fake.page.evaluate("document.activeElement.id || document.activeElement.className"))


@BOTH
@pytest.mark.parametrize(
    ("text", "at", "expected"),
    [
        ("- a\n- b", 4, "- a\n  - b"),  # the line's start
        ("- a\n- b", 5, "- a\n  - b"),  # on the marker
        ("- a\n- b", 7, "- a\n  - b"),
        ("1. a\n2. b", 6, "1. a\n  2. b"),
        ("1. a\n2. b", 9, "1. a\n  2. b"),
        ("* a\n\t* b", 8, "* a\n\t* b"),  # a tab-indented first child: nothing above it at its level
        ("* a\n\t* b\n\t* c", 13, "* a\n\t* b\n  \t* c"),
        ("  - a\n- b", 9, "  - a\n    - b"),  # siblings for compose: two past a's indent
    ],
)
async def test_tab_nests_an_item_in_its_previous_sibling(
    fake: Fake, text: str, at: int, expected: str
) -> None:
    """041-AC1: Tab anywhere in an item, its marker included: two more spaces of indent, the caret on the same
    character, marker as typed; nothing sent."""
    page, _ = _parts(fake)
    moved = len(expected) - len(text)
    assert await _press(page, text, at, key="Tab") == [expected, at + moved, at + moved]
    assert await _focused(fake) == "reply"
    await fake.settled()
    assert fake.of("send") == []


def _parent(depths: dict[str, int], label: str) -> str | None:
    """The item `label` nests in: the last one before it a level up (None at the top)."""
    names = list(depths)
    up = [n for n in names[: names.index(label)] if depths[n] == depths[label] - 1]
    return up[-1] if up else None


@BOTH
@pytest.mark.parametrize(
    ("text", "key", "parent"),
    [
        ("- w0z\n    - w1z\n- w2z", "Tab", "w0z"),  # 041-AC1: b joins a's sublist, at its indent
        ("- w0z\n\t- w1z\n- w2z", "Tab", "w0z"),
        ("- w0z\n    - w1z\n  - w2z", "Tab", "w0z"),  # compose reads `  - ` as a's sibling
        ("- w0z\n    - w1z\n  - w2z\n- w3z", "Tab", "w2z"),  # its sibling is `  - w2z`, at the top
        ("  - w0z\n- w1z", "Tab", "w0z"),  # two past the sibling's indent
        ("- w0z\n    - w1z\n      - w2z\n    - w3z", "Shift+Tab", None),  # 041-AC4: s would not be b's
    ],
)
async def test_uneven_indents_move_as_compose_reads_them(
    fake: Fake, text: str, key: str, parent: str | None
) -> None:
    """041-AC1 / AC4: with uneven indents the moved item lands exactly one compose level deeper (in the item
    before it at its level) or up, every other item where it was, followers on Shift+Tab its children; else
    the key does nothing."""
    page, _ = _parts(fake)
    label = "w1z" if key == "Shift+Tab" else list(_depths(text))[-1]
    at = text.index(label) + 3
    value = (await _press(page, text, at, key=key))[0]
    assert await _focused(fake) == "reply"
    assert (value != text) == (parent is not None), value
    if parent is None:
        return
    before, after = _depths(text), _depths(value)
    assert after == {k: v + (k == label) for k, v in before.items()}, value
    assert _parent(after, label) == parent


@BOTH
@pytest.mark.parametrize(
    ("text", "at", "expected", "caret"),
    [
        ("- a\n  - b", 9, "- a\n- b", 7),  # 041-AC2
        ("- a\n  - b", 5, "- a\n- b", 4),  # a caret in the removed indent: its line's start
        ("- a\n  - b\n  - c", 9, "- a\n- b\n  - c", 7),  # 041-AC4: the sibling below becomes its child
        ("1. a\n    2. b", 13, "1. a\n2. b", 9),  # back to the parent's indent, however deep
    ],
)
async def test_shift_tab_moves_an_item_up_a_level(
    fake: Fake, text: str, at: int, expected: str, caret: int
) -> None:
    """041-AC2 / AC4: Shift+Tab shifts the item to its parent's indent."""
    page, _ = _parts(fake)
    assert await _press(page, text, at, key="Shift+Tab") == [expected, caret, caret]
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize("at", [6, 11])  # on the item line, on its break line
async def test_the_moved_item_takes_its_subtree_and_line_breaks(fake: Fake, at: int) -> None:
    """041-AC3: the item's break lines and deeper items move with it; the next sibling at the old level does
    not; Shift+Tab gives back the original."""
    page, box = _parts(fake)
    text = "- a\n- b\\\n  b2\n  - c\\\n    c2\n- d"
    moved = "- a\n  - b\\\n    b2\n    - c\\\n      c2\n- d"
    assert (await _press(page, text, at, key="Tab"))[0] == moved
    await box.press("Shift+Tab")
    assert await _selection(page) == [text, at, at]


@BOTH
@pytest.mark.parametrize(
    ("text", "at", "key"),
    [
        ("- a\n- b", 2, "Tab"),  # the list's first item
        ("x\n- a", 4, "Tab"),
        ("- a\n  - b", 9, "Tab"),  # its level's first item
        ("- a\n- b", 7, "Shift+Tab"),  # top level
        ("  - a", 5, "Shift+Tab"),  # the run's first item, however indented
        ("- a\\\n  b", 8, "Shift+Tab"),  # on its break line
    ],
)
async def test_a_move_with_nowhere_to_go_changes_nothing_and_keeps_focus(
    fake: Fake, text: str, at: int, key: str
) -> None:
    """041-AC5: Tab on a first item, Shift+Tab on a top-level one: text unchanged, focus kept, none sent."""
    page, _ = _parts(fake)
    assert await _press(page, text, at, key=key) == [text, at, at]
    assert await _focused(fake) == "reply"
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize("key", ["Tab", "Shift+Tab"])
@pytest.mark.parametrize(("text", "at"), [("hello", 2), ("- a\n\nb", 5), ("```\n- a\n- b\n```", 10)])
async def test_tab_outside_a_list_moves_focus(fake: Fake, text: str, at: int, key: str) -> None:
    """041-AC6: outside a list (plain text, a fence) Tab and Shift+Tab move focus, the text unchanged."""
    page, box = _parts(fake)
    await _select(page, text, at, at)
    await box.press(key)
    assert await _focused(fake) != "reply"
    assert await box.input_value() == text


@BOTH
async def test_tab_with_the_completion_open_picks(fake: Fake) -> None:
    """041-AC6: in a list item, Tab with the completion list open picks, nothing indented."""
    page, box = _parts(fake)
    await _select(page, "- a\n- ", 6, 6)
    await box.press_sequentially("@ev")
    await shows(page, ["everyone"])
    await box.press("Tab")
    assert await box.input_value() == "- a\n- @everyone "
    assert await _focused(fake) == "reply"


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "b", "key", "expected"),
    [
        ("- a\n- b\n- c", 6, 11, "Tab", "- a\n  - b\n  - c"),
        ("- a\n- b\n- c\n", 6, 12, "Tab", "- a\n  - b\n  - c\n"),  # a trailing newline ends it
        ("- a\n  - b\n  - c", 8, 15, "Shift+Tab", "- a\n- b\n- c"),
        ("- a\n- b\nx\n- c", 6, 13, "Tab", "- a\n- b\nx\n- c"),  # c starts a list: nothing to nest in
        ("x\n- a\n- b\ny", 0, 11, "Tab", "x\n- a\n- b\ny"),  # list lines inside: the key is kept
        ("- a\n- b\nx", 6, 9, "Tab", "- a\n  - b\nx"),  # an end on a plain line: the items in range move
        (
            "- a\n  - a1\n  - b\n- c\n  - d",
            15,
            20,
            "Tab",
            "- a\n  - a1\n    - b\n  - c\n    - d",
        ),  # d stays c's
        ("- a\n- b\n  - c\n  - e", 6, 13, "Tab", "- a\n  - b\n    - c\n    - e"),  # e is b's: it moves
        ("- a\n  - b\n- c\\\n  c2", 8, 13, "Shift+Tab", "- a\n  - b\n- c\\\n  c2"),  # c's break would go
    ],
)
async def test_a_selection_over_items_moves_them_together(
    fake: Fake, text: str, a: int, b: int, key: str, expected: str
) -> None:
    """041-AC7: every item from the selection's first to its last moves; the selection stays on the same
    characters."""
    page, _ = _parts(fake)
    value, start, end = await _press(page, text, a, b, key=key)
    assert value == expected
    assert await _focused(fake) == "reply"
    assert value[start:end].replace(" ", "") == text[a:b].replace(" ", "")
    assert value[start] == text[a] and value[end - 1] == text[b - 1]


@BOTH
async def test_a_selection_over_b_and_c_under_a(fake: Fake) -> None:
    """041-AC7: b and c below a selected: Tab nests both in a, selection on the same text; b and c under a's
    child z: Tab nests both in z. A first child selected first has nowhere to go: nothing moves."""
    page, _ = _parts(fake)
    assert await _press(page, "- a\n- b\n- c", 6, 11, key="Tab") == ["- a\n  - b\n  - c", 8, 15]
    text = "- a\n  - b\n  - c"
    assert await _press(page, text, 8, 15, key="Tab") == [text, 8, 15]
    text = "- a\n  - z\n  - b\n  - c"
    assert await _press(page, text, 14, 21, key="Tab") == ["- a\n  - z\n    - b\n    - c", 16, 25]


@BOTH
async def test_ctrl_z_takes_back_each_tab(fake: Fake) -> None:
    """041-AC8: each Tab and Shift+Tab is one undo step."""
    page, box = _parts(fake)
    await _select(page, "- a\n- b\n- c", 11, 11)
    steps = [
        (11, "Tab", "- a\n- b\n  - c"),
        (7, "Tab", "- a\n  - b\n    - c"),
        (17, "Shift+Tab", "- a\n  - b\n  - c"),
    ]
    for at, key, want in steps:
        await page.evaluate(f"(r => r.setSelectionRange({at}, {at}))(document.getElementById('reply'))")
        await box.press(key)
        assert await box.input_value() == want
    for want in [w for _, _, w in reversed(steps[:-1])] + ["- a\n- b\n- c"]:
        await box.press("Control+z")
        assert await box.input_value() == want


@BOTH
@pytest.mark.parametrize("key", ["Tab", "Shift+Tab"])
async def test_picks_in_and_after_a_moved_item_keep_their_people(hooked: Fake, key: str) -> None:
    """041-AC9 / 033: a pick in the moved item (on its break line too) and one in an item after it stay on
    their `@`, raw anchors and sent."""
    page, box = _parts(hooked)
    start = "- x\n- ab\\\n  cd\n- ef" if key == "Tab" else "- x\n  - ab\\\n    cd\n- ef"
    await box.fill(start)
    for word in ("ef", "cd", "ab"):  # from the end: each pick leaves the earlier offsets alone
        at = (await box.input_value()).index(word) + 1
        await page.evaluate(f"(r => r.setSelectionRange({at}, {at}))(document.getElementById('reply'))")
        await _pick(page, box, " @ann", ["Ann Lee"])
    at = start.index("ab")  # the picks all go in after it
    await page.evaluate(f"(r => r.setSelectionRange({at}, {at}))(document.getElementById('reply'))")
    await box.press(key)
    out = await box.input_value()
    assert ("  - a" in out) == (key == "Tab")
    want = [m.start() for m in re.finditer("@Ann Lee", out)]
    assert len(want) == 3
    assert sorted(await _anchors(page)) == want
    assert await _send(hooked, box) == [{**ANN, "at": n} for n in want]
    assert hooked.of("send")[0]["text"] == out


@BOTH
async def test_tab_and_shift_tab_in_the_editor(fake: Fake) -> None:
    """041-AC10: the message editor indents and outdents the same way; a first item keeps the key and the
    focus; nothing saved."""
    page = fake.page
    assert page is not None
    box = await _edit(fake)
    await _select_editor(page, "- a\n- b", 7, 7)
    await box.press("Tab")
    assert await box.input_value() == "- a\n  - b"
    await box.press("Shift+Tab")
    assert await box.input_value() == "- a\n- b"
    await _select_editor(page, "- a\n- b", 2, 2)
    await box.press("Tab")
    assert await box.input_value() == "- a\n- b"
    assert "editor" in await _focused(fake)
    await fake.settled()
    assert fake.of("edit") == [] and fake.of("send") == []


def _depths(text: str) -> dict[str, int]:
    """Each `wNz` item's list depth as compose renders `text`."""
    html, _ = compose_html(text, [], {}, full=True)
    depth, out = 0, {}
    for m in re.finditer(r"<(/?)[uo]l>|<li>(w\d+z)", html):
        if m[2]:
            out[m[2]] = depth
        else:
            depth += -1 if m[1] else 1
    return out


def _below(depths: dict[str, int], labels: list[str]) -> set[str]:
    """`labels` and every item nested in one of them."""
    names, out = list(depths), set(labels)
    for label in labels:
        for name in names[names.index(label) + 1 :]:
            if depths[name] <= depths[label]:
                break
            out.add(name)
    return out


@BOTH
async def test_tab_and_shift_tab_keep_composes_tree_on_random_lists(fake: Fake) -> None:
    """041-AC1 / AC2 / AC3 / AC5 / AC7: random lists (each level's step 1-4 spaces or a tab), line breaks,
    selections over 1-3 items: a press either changes nothing or moves the selected items and their compose
    descendants one level, every other item where it was. Top-level items at random indents. A Tab on one item
    (on several, on two-space lists) with a compose sibling above never refuses; on two-space lists Shift+Tab
    after it gives the original back."""
    import random

    rnd = random.Random(41)
    cases = []
    for _ in range(400):
        even = rnd.random() < 0.4
        steps = ["  " if even else rnd.choice([" ", "  ", "   ", "    ", "\t"]) for _ in range(8)]
        lines: list[str] = []
        items: list[int] = []
        level, base = 0, ""
        for n in range(rnd.randint(2, 7)):
            level = rnd.randint(0, level + 1) if lines else 0
            if not level and not even:  # each top-level item at its own indent
                base = " " * rnd.randint(0, 3)
            indent, marker = base + "".join(steps[:level]), rnd.choice(["- ", "1. "])
            items.append(len(lines))
            lines.append(indent + marker + f"w{n}z")
            if rnd.random() < 0.3:
                lines[-1] += "\\"
                lines.append(indent + " " * (len(marker) + rnd.randint(0, 1)) + rnd.choice(["u", "\\- u"]))
        i = rnd.randrange(len(items))
        j = min(len(items) - 1, i + rnd.choice([0, 0, 1, 2]))
        starts = [sum(len(x) + 1 for x in lines[:k]) for k in range(len(lines))]
        a = starts[items[i]] + rnd.randint(0, len(lines[items[i]]))
        b = starts[items[j]] + rnd.randint(j > i, len(lines[items[j]]))  # column 0 would end before it
        a, b = min(a, b), max(a, b)
        shift = rnd.random() < 0.4
        cases.append(("\n".join(lines), a, b, shift, even, [f"w{n}z" for n in range(i, j + 1)]))
    page, _ = _parts(fake)
    got = await page.evaluate(
        """cs => cs.map(([c, a, b, shift]) => { const r = document.getElementById('reply'); r.value = c;
        r.focus(); r.setSelectionRange(a, b);
        const press = (shiftKey) => r.dispatchEvent(new KeyboardEvent('keydown',
          { key: 'Tab', shiftKey, bubbles: true, cancelable: true }));
        const free = press(shift), out = r.value, caret = r.selectionStart;
        if (!shift && out !== c) press(true);
        return [free, out, caret, r.value]; })""",
        [[text, a, b, shift] for text, a, b, shift, _, _ in cases],
    )
    moved = 0
    for (text, a, b, shift, even, picked), (free, out, caret, back) in zip(cases, got, strict=True):
        case = (text, a, b, shift, out)
        assert not free, case  # a list item's Tab is always the bar's
        before = _depths(text)
        first = picked[0]
        names = list(before)
        older = [before[n] for n in names[: names.index(first)]]
        has_sibling = any(
            d == before[first] and all(e >= before[first] for e in older[k:]) for k, d in enumerate(older)
        )
        if out == text:
            # A uniform shift cannot always place several items at uneven indents.
            assert shift or not has_sibling or (len(picked) > 1 and not even), case
            continue
        moved += 1
        sub = _below(before, picked)
        assert _depths(out) == {k: v + (k in sub) * (-1 if shift else 1) for k, v in before.items()}, case
        if not shift:
            assert out[caret:].split("\n")[0] == text[a:].split("\n")[0], case  # the caret on its character
            if even:
                assert back == text, case
    assert moved > 150, moved
    await fake.settled()
    assert fake.of("send") == []
