"""Enter and Shift+Enter in lists (spec 040 phases 3 and 4) on the chat page's composer, against the composer
files' fake chat socket. Synthetic data, no network, no Teams.

Run: `uv run --with playwright pytest tests/test_browser_list_keys.py`.
"""

import re
from collections.abc import AsyncIterator
from html import unescape
from typing import Any

import browser_helpers
import pytest
from browser_helpers import ANN, Page, Pages, shows, wait
from test_browser_chat_compose import Fake, _open, _parts, _pick, _select, _selection

from miniteams.compose import compose_html

pytestmark = pytest.mark.browser
pages = browser_helpers.pages
BOTH = pytest.mark.parametrize("pages", ["chromium", "firefox"], indirect=True)


@pytest.fixture
async def fake(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    page = await _open(pages, board)
    await page.locator("#reply").focus()
    yield board
    await pages.close()
    assert pages.external == []


# --- Enter and Shift+Enter in lists (spec 040 phases 3 and 4) ---


async def _press(page: Page, text: str, a: int, b: int | None = None, key: str = "Enter") -> list[Any]:
    """`text` with `[a, b)` selected (a caret without `b`), then `key`: [value, start, end]."""
    await _select(page, text, a, a if b is None else b)
    await page.locator("#reply").press(key)
    return await _selection(page)


def _depth(text: str) -> int:
    """How many lists hold the closing `Q` item, as compose renders `text` (0: not an item)."""
    html, _ = compose_html(text, [], {}, full=True)
    found = [*re.finditer(r"<li>\s*Q", html)]
    head = html[: found[-1].start()] if found else ""
    return len(re.findall(r"<[uo]l>", head)) - len(re.findall(r"</[uo]l>", head))


def _items(text: str) -> int:
    return compose_html(text, [], {}, full=True)[0].count("<li>")


@BOTH
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- a\n  - b", "- a\n  - b\n  - "),
        ("1. a\n2. c", "1. a\n2. c\n3. "),
        ("* x", "* x\n* "),
        ("\t- t", "\t- t\n\t- "),
        ("9. n", "9. n\n10. "),
        ("```\n- a", "```\n- a\n- "),  # an unclosed fence is text
        ("```\nx\n```\n- a", "```\nx\n```\n- a\n- "),  # a closed one above does not stop it
    ],
)
async def test_enter_at_the_end_of_an_item_opens_the_next(fake: Fake, text: str, expected: str) -> None:
    """040-AC11 / AC14: Enter at the end of an item: newline, same indent, next marker, the caret after it;
    nothing sent. The new item composes as its item's sibling."""
    page, _ = _parts(fake)
    assert await _press(page, text, len(text)) == [expected, len(expected), len(expected)]
    assert _depth(expected + "Q") == _depth(text[:-1] + "Q") and _items(expected + "Q") == _items(text) + 1
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- a\n  - ", "- a\n- "),
        ("1. a\n   - ", "1. a\n2. "),
        ("* a\n  1. b\n    - ", "* a\n  1. b\n  2. "),
        ("1. a\n  - b\n    - c\n  - ", "1. a\n  - b\n    - c\n2. "),  # siblings and deeper items skipped
        ("1. a\ntext\n  - ", "1. a\ntext\n"),  # a plain line ends the run: a new top level
        ("-  ", ""),  # blank text: empty
        ("- a\n  -  ", "- a\n- "),
        ("  - ", ""),  # the first item of its run is the top level, however indented
        ("- a\n  - b\n    - c\n   - ", "- a\n  - b\n    - c\n- "),  # compose has `   - ` as b's sibling
        ("- a\n    - b\n  - ", "- a\n    - b\n"),  # compose has `  - ` at the top: the list ends
        ("1. a\n  2. b\n    - ", "1. a\n  2. b\n  3. "),
        ("- a\\\n  b\n  - ", "- a\\\n  b\n- "),  # a line break inside the parent
        ("- ", ""),
        ("x\n- a\n- ", "x\n- a\n"),
    ],
)
async def test_enter_on_an_empty_item_goes_one_level_up_or_ends_the_list(
    fake: Fake, text: str, expected: str
) -> None:
    """040-AC12: an empty indented item becomes its parent's sibling; an empty top-level one is emptied,
    no newline."""
    page, _ = _parts(fake)
    assert await _press(page, text, len(text)) == [expected, len(expected), len(expected)]
    assert _depth(expected + "Q") == _depth(text + "Q") - 1


@BOTH
async def test_continuation_matches_composes_tree_on_random_lists(fake: Fake) -> None:
    """040-AC11 / AC12 / AC18: on random indented runs with line breaks, Enter's new item composes at its
    item's level, an empty item's level up one compose level less (the JS cannot drift from compose)."""
    import random

    rnd = random.Random(40)
    cases = []
    for _ in range(400):
        lines: list[str] = []
        for _ in range(rnd.randint(1, 6)):
            lines.append(
                " " * rnd.randint(0, 6) + rnd.choice(["- ", "* ", "1. ", "7. "]) + rnd.choice(["t", " ", ""])
            )
            if rnd.random() < 0.3:  # a line break, then a line indented anywhere around the item's text
                lines[-1] += "\\"
                lines.append(" " * rnd.randint(0, 9) + rnd.choice(["u", "- u", "\\- u"]))
        head = " " * rnd.randint(0, 7) + rnd.choice(["- ", "* ", "2. "])
        last = head + rnd.choice(["", " ", "  ", "t"])
        # The caret on its item's break line (040-AC18): the next item opens at the item's level.
        broken = rnd.random() < 0.3
        if broken:
            last += "\\\n" + " " * (len(head) + rnd.randint(0, 2)) + rnd.choice(["u", "\\- u", ""])
        want = _depth("\n".join([*lines, head + "Q"]))
        cases.append(("\n".join([*lines, last]), want if broken or last.endswith("t") else want - 1))
    page, _ = _parts(fake)
    got = await page.evaluate(
        """cs => cs.map(c => { const r = document.getElementById('reply'); r.value = c; r.focus();
        r.setSelectionRange(c.length, c.length);
        r.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
        return r.value; })""",
        [text for text, _ in cases],
    )
    for (text, want), out in zip(cases, got, strict=True):
        assert _depth(out + "Q") == want, (text, out)
    await fake.settled()
    assert fake.of("send") == []


_ITEM_END_BACKSLASH = re.compile(r"\\(?:</li>|<ul>|<ol>)")


def _content(html: str) -> str:
    return re.sub(r"<[^>]+>|\s", "", html)


@BOTH
async def test_enter_and_shift_enter_anywhere_keep_line_breaks_in_their_item(fake: Fake) -> None:
    """040-AC13 / AC15 / AC18: Enter or Shift+Enter at any caret of a list with line breaks (in a marker, an
    indent, right after a `\\`), then a character typed: compose reads no new literal `\\` at an item's end
    and loses no text."""
    import random

    rnd = random.Random(41)
    cases = []
    for _ in range(400):
        lines: list[str] = []
        for n in range(rnd.randint(1, 4)):
            marker = rnd.choice(["- ", "1. "])
            head = rnd.choice(["", "", "- ", "1. "])  # item text that looks like a marker
            lines.append(" " * rnd.choice([0, 0, 2, 4]) + marker + head + f"w{n}a")
            for m in range(rnd.choice([0, 1, 1, 2])):
                lines[-1] += "\\"
                head = rnd.choice(["", "", "\\- ", "\\1. ", "- ", "1. "])  # escaped, or a new item
                lines.append(
                    " " * (len(lines[-1]) - len(lines[-1].lstrip()) + len(marker)) + head + f"w{n}b{m}"
                )
        text = "\n".join(lines)
        cases.append((text, rnd.randint(0, len(text)), rnd.random() < 0.5))
    page, _ = _parts(fake)
    got = await page.evaluate(
        """cs => cs.map(([c, at, shift]) => { const r = document.getElementById('reply');
        r.value = c; r.focus(); r.setSelectionRange(at, at);
        const free = r.dispatchEvent(new KeyboardEvent('keydown',
          { key: 'Enter', shiftKey: shift, bubbles: true, cancelable: true }));
        return [free, r.value, r.selectionStart]; })""",
        cases,
    )
    handled = 0
    for (text, at, shift), (free, out, caret) in zip(cases, got, strict=True):
        if free or not out:  # off a list: the browser's newline, or the page's Enter sent it
            continue
        handled += 1
        # A character typed where the caret's line has no text left (a new item or line), else the text as it
        # stands: typing before it would hide what a marker-like start reads as.
        rest = out[caret:]
        typed = out[:caret] + "Q" + rest if not rest.split("\n", 1)[0].strip(" \t") else out
        before, after = compose_html(text, [], {}, full=True)[0], compose_html(typed, [], {}, full=True)[0]
        case = (text, at, shift, typed)
        assert len(_ITEM_END_BACKSLASH.findall(after)) <= len(_ITEM_END_BACKSLASH.findall(before)), case
        assert _content(after).replace("Q", "") == _content(before), case  # the same text, in order
    assert handled > 300


_LIST_TAG = re.compile(r"(</?(?:ul|ol|li)>|<br>)")


def _read(text: str) -> tuple[list[list[str]], list[str]]:
    """Each `<li>`'s own parts as compose writes them (inline HTML split at `<br>`, sublists left out) in
    document order, and the HTML outside any list."""
    items: list[list[str]] = []
    outside: list[str] = []
    open_: list[int | None] = []
    for token in _LIST_TAG.split(compose_html(text, [], {}, full=True)[0]):
        if token == "<li>":
            items.append([""])
            open_.append(len(items) - 1)
        elif token in ("<ul>", "<ol>"):
            open_.append(None)
        elif token in ("</li>", "</ul>", "</ol>"):
            open_.pop()
        elif open_ and open_[-1] is not None:
            if token == "<br>":
                items[open_[-1]].append("")
            else:
                items[open_[-1]][-1] += token
        elif token and not open_:
            outside.append(token)
    return items, outside


def _shown(q: str, off: int) -> str:
    """What compose shows of an item text `q` up to `off`, an escape cut by `off` left out (the caret snaps
    before it), read with compose's own escape rule."""
    from miniteams.compose import _escape_len

    out, i = [], 0
    while i < len(q):
        n = 2 if q[i] == "\\" and _escape_len(q, i, "", False) else 1
        if i + n > off:
            break
        out.append(q[i + 1] if n == 2 else q[i])
        i += n
    return "".join(out)


def _plain(parts: list[list[str]]) -> list[list[str]]:
    return [[unescape(x) for x in it] for it in parts]


# Plain text only (no span can open): what compose shows is the text itself, compared exactly.
_ALPHABET = [
    *("x", "y", " ", "\t", "-", "- ", "* ", "12. ", "3.", " - ", "  1. ", " \\- ", "> ", ">"),
    *("\\" * k for k in range(1, 7)),
    *("\\" * k + "- " for k in range(1, 7)),
    *("\\" * k + m for k in (1, 2, 3) for m in ("1. ", "* ", "> ")),
]


def _list_case(rnd: Any) -> tuple[str, list[list[str]], list[tuple[int, int, int, str, bool]]] | None:
    """Random items with line breaks: (text, each item's parts as item text, per line (item, part, where its
    item text starts on the line, that text, ends with a separator)); None when compose reads it otherwise."""

    def text_() -> str:
        return "".join(rnd.choice(_ALPHABET) for _ in range(rnd.randint(1, 4)))

    items: list[list[str]] = []
    lines: list[str] = []
    where: list[tuple[int, int, int, str, bool]] = []
    for n in range(rnd.randint(1, 3)):
        indent, marker = rnd.choice(["", "", "  ", "    ", "\t", " "]), rnd.choice(["- ", "* ", "1. ", "9. "])
        breaks = []
        for _ in range(rnd.choice([0, 0, 1, 1, 2])):
            q = text_().lstrip(" \t") or "z"
            # Its break line: the item text, or one more backslash where a line start would read it otherwise.
            ok = [s for s in (q, "\\" + q) if _plain(_read("- a\\\n  " + s)[0]) == [["a", _shown(q, len(q))]]]
            breaks.append((q, ok[0]) if ok else ("z", "z"))
        first = text_()
        items.append([first, *(q for q, _ in breaks)])
        lines.append(indent + marker + first + ("\\" if breaks else ""))
        where.append((n, 0, len(indent) + len(marker), first, bool(breaks)))
        for p, (q, s) in enumerate(breaks, 1):
            lead = " " * (len(indent) + len(marker) + rnd.choice([0, 0, 1, 2]))
            lines.append(lead + s + ("\\" if p < len(breaks) else ""))
            where.append((n, p, len(lead) + len(s) - len(q), q, p < len(breaks)))
    text = "\n".join(lines)
    read = _read(text)
    if _plain(read[0]) != [[_shown(q, len(q)) for q in it] for it in items] or read[1]:
        return None
    return text, items, where


def _logical(text: str, where: list[tuple[int, int, int, str, bool]], o: int) -> tuple[int, int, int]:
    """Raw offset `o` → (item, part, shown offset): an indent, a marker, a separator or a break line's escape
    is at its part's start."""
    start = 0
    for line, (n, p, at, q, sep) in zip(text.split("\n"), where, strict=True):
        end = start + len(line)
        if o <= end:
            if sep and o == end:
                return n, p + 1, 0
            return n, p, len(_shown(q, min(max(o - start - at, 0), len(q))))
        start = end + 1
    raise AssertionError(o)


def _split(
    own: list[str], head: str, tail: str, k1: int, k2: int, shift: bool, typed: bool
) -> list[list[str]]:
    """The item `own` (its parts) after Enter / Shift+Enter over [part k1's `head`, part k2's `tail`]: a new
    item or a line break between, the text between gone; `typed`: a `Q` typed on the caret's blank line."""
    if shift:  # the tail starts a break line: its leading blanks would join the indent
        tail = tail.lstrip(" \t")
    tail = "Q" + tail if typed else tail
    if shift:
        return [[*own[:k1], head, tail, *own[k2 + 1 :]]]
    left = [*own[:k1], head]
    while len(left) > 1 and not left[-1].strip(" \t"):
        left.pop()
    return [left, [tail, *own[k2 + 1 :]]]


async def _keys(page: Page, cases: list[tuple[str, int, int, bool]]) -> list[Any]:
    """Each (text, selection start, end, shift) pressed: [the page left the key alone, value, caret]."""
    return list(
        await page.evaluate(
            """cs => cs.map(([c, a, b, shift]) => { const r = document.getElementById('reply');
            r.value = c; r.focus(); r.setSelectionRange(a, b);
            const free = r.dispatchEvent(new KeyboardEvent('keydown',
              { key: 'Enter', shiftKey: shift, bubbles: true, cancelable: true }));
            return [free, r.value, r.selectionStart]; })""",
            cases,
        )
    )


def _typed(out: str, caret: int) -> str:
    """`out` with a `Q` typed at the caret when its line has no text after it (else compose reads no item)."""
    return out[:caret] + "Q" + out[caret:] if not out[caret:].split("\n", 1)[0].strip(" \t") else out


@BOTH
async def test_enter_and_shift_enter_split_the_items_contents_at_the_logical_caret(fake: Fake) -> None:
    """040-AC13 / AC15 / AC18, the class guard: on random items with line breaks whose texts hold marker-like
    runs, spaces, tabs and backslash runs, a random caret or selection anywhere (indents, markers,
    separators, escapes, mid-text), Enter or Shift+Enter: compose shows exactly the items before with the
    selection gone and one split (Enter: a new item; Shift+Enter: a line break) at the logical caret,
    nothing else changed, no `\\` gained; a selection reaching another item: a plain newline (Enter) or the
    browser's."""
    import random

    rnd = random.Random(42)
    cases: list[tuple[str, list[list[str]], Any, int, int, bool]] = []
    while len(cases) < 700:
        if (made := _list_case(rnd)) is None:
            continue
        text, items, where = made
        a = rnd.randint(0, len(text))
        b = min(len(text), a + rnd.randint(1, 6)) if rnd.random() < 0.35 else a
        cases.append((text, items, where, a, b, rnd.random() < 0.5))
    page, _ = _parts(fake)
    got = await _keys(page, [(t, a, b, s) for t, _, _, a, b, s in cases])
    failures, checked = [], 0
    for (text, items, where, a, b, shift), (free, out, caret) in zip(cases, got, strict=True):
        shown = [[_shown(q, len(q)) for q in it] for it in items]
        (na, pa, xa), (nb, pb, xb) = _logical(text, where, a), _logical(text, where, b)
        case = (text, a, b, shift, out)
        if na != nb:
            if (free, out) != ((True, text) if shift else (False, text[:a] + "\n" + text[b:])):
                failures.append(case)
            continue
        if not shift and (pa, xa) == (pb, xb) and len(items[na]) == 1 and not shown[na][0].strip(" \t"):
            continue  # an empty item goes up a level: the continuation fuzz's
        checked += 1
        typed = _typed(out, caret)
        own = shown[na]
        new = _split(own, own[pa][:xa], own[pb][xb:], pa, pb, shift, typed != out)
        read = _read(typed)
        if free or (_plain(read[0]), read[1]) != ([*shown[:na], *new, *shown[na + 1 :]], []):
            failures.append(case)
    assert checked > 500
    assert failures == [], f"{len(failures)} of {checked}: {failures[:8]}"
    await fake.settled()
    assert fake.of("send") == []


# Markup that reads the same whatever text is around it, joined by spaces: what compose writes for each is
# known alone, so a split at any atom boundary is checked against the exact HTML.
_ATOMS = [
    *("word", "x2", "*em*", "**b**", "_i_", "~~s~~", "`co\\de`", "`a\\`", "`\\\\`", "[l](https://x.y/p\\q)"),
    *(
        "https://x.y/a\\b",
        "@{Ann|Ann L}",
        ":fire:",
        "\\*",
        "\\\\",
        "\\_",
        "\\`x\\`",
        "C:\\path\\",
        "- y",
        "1. z",
    ),
    *(
        "\\- w",
        "> q",
        "\\> r",
        "\\\\- v",
        "\\\\\\- u",
        "\\\\\\\\1. t",
        "**a\\*b**",
        "_x\\_y_",
        "a\\\\",
        "\\[n]",
    ),
    *("@Ann", "x\\", "\\@{A|b}", "\\\\> s"),
]


def _inline(item_text: str) -> str:
    """What compose writes for `item_text` read as an item's text."""
    items, outside = _read("- " + item_text)
    return items[0][0] if len(items) == 1 and len(items[0]) == 1 and not outside else "\x00"


def _atom_case(
    rnd: Any, markers: tuple[str, ...] = ("- ", "* ", "1. ", "9. ", "99. ")
) -> tuple[str, list[str], list[tuple[int, int, str, str]], tuple[int, int], tuple[bool, bool]] | None:
    """An item with line breaks of random atoms, maybe between two plain items: (text, its parts' HTML, every
    caret in it as (offset, part, HTML before, HTML after), its span, whether items precede and follow it);
    None when compose reads it otherwise."""
    indent, marker = rnd.choice(["", "  "]), rnd.choice(markers)
    col = len(indent) + len(marker)
    atoms = [[rnd.choice(_ATOMS) for _ in range(rnd.randint(1, 4))] for _ in range(rnd.choice([1, 1, 2, 3]))]
    texts = [" ".join(p) for p in atoms]
    shown = [[_inline(a) for a in p] for p in atoms]
    lines = [indent + marker + texts[0]]
    starts = [col]
    for q, html_ in zip(texts[1:], shown[1:], strict=True):
        ok = [s for s in (q, "\\" + q) if _read("- a\\\n  " + s) == ([["a", " ".join(html_)]], [])]
        if not ok:
            return None
        lead = " " * (col + rnd.choice([0, 0, 1]))
        lines[-1] += "\\"
        lines.append(lead + ok[0])
        starts.append(len(lead) + len(ok[0]) - len(q))
    pre = [indent + "- pre"] if rnd.random() < 0.3 else []
    post = [indent + "- post"] if rnd.random() < 0.3 else []
    text = "\n".join([*pre, *lines, *post])
    parts = [" ".join(h) for h in shown]
    if "\x00" in "".join(parts) or _read(text) != (
        [*([["pre"]] if pre else []), parts, *([["post"]] if post else [])],
        [],
    ):
        return None
    first = len(pre[0]) + 1 if pre else 0
    carets: list[tuple[int, int, str, str]] = []
    at = first
    for k, (line, start, q, h) in enumerate(zip(lines, starts, texts, shown, strict=True)):
        carets += [(at + o, k, "", parts[k]) for o in range(start + 1)]  # an indent, a marker, an escape
        for j in range(1, len(h)):
            mid = at + start + len(" ".join(atoms[k][:j]))
            carets += [
                (mid, k, " ".join(h[:j]), " " + " ".join(h[j:])),
                (mid + 1, k, " ".join(h[:j]) + " ", " ".join(h[j:])),
            ]
        end = at + start + len(q)
        carets.append((end, k, parts[k], ""))
        if k < len(lines) - 1:
            carets.append((end + 1, k + 1, "", parts[k + 1]))  # right after the separator
        at += len(line) + 1
    return text, parts, carets, (first, at - 1), (bool(pre), bool(post))


@BOTH
async def test_a_split_at_any_markup_boundary_keeps_each_parts_html(fake: Fake) -> None:
    """040-AC13 / AC15 / AC18, the class guard on markup: emphasis, code spans holding `\\`, links and URLs
    with `\\`, mentions, emoji and marker-like atoms around a caret or selection at any atom boundary, an
    indent, a marker, a separator or an escape: compose writes exactly each part's HTML before, the selection
    gone, one split; a selection reaching another item: a plain newline (Enter) or the browser's."""
    import random

    rnd = random.Random(43)
    cases: list[Any] = []
    while len(cases) < 1500:
        if (made := _atom_case(rnd)) is None:
            continue
        text, parts, carets, (first, last), around = made
        one = rnd.choice(carets)
        two = rnd.choice(carets) if rnd.random() < 0.3 else one
        if rnd.random() < 0.1 and (first or last < len(text)):  # into the item around it
            two = (rnd.choice([o for o in (0, len(text)) if o < first or o > last]), -1, "", "")
        one, two = sorted([one, two])
        cases.append((text, parts, around, one, two, rnd.random() < 0.5))
    page, _ = _parts(fake)
    got = await _keys(page, [(t, one[0], two[0], s) for t, _, _, one, two, s in cases])
    failures: list[Any] = []
    for (text, parts, (pre, post), one, two, shift), (free, out, caret) in zip(cases, got, strict=True):
        a, b = one[0], two[0]
        if one[1] < 0 or two[1] < 0:
            if (free, out) != ((True, text) if shift else (False, text[:a] + "\n" + text[b:])):
                failures.append((text, a, b, shift, out))
            continue
        typed = _typed(out, caret)
        new = _split(parts, one[2], two[3], one[1], two[1], shift, typed != out)
        want = [*([["pre"]] if pre else []), *new, *([["post"]] if post else [])]
        if free or _read(typed) != (want, []):
            failures.append((text, a, b, shift, out, want, _read(typed)))
    assert failures == [], f"{len(failures)} of {len(cases)}: {failures[:6]}"


@BOTH
async def test_picks_keep_their_people_through_any_split(fake: Fake) -> None:
    """040-AC13 / AC15 / 033, the class guard on anchors: picks typed in an item with line breaks of random
    atoms (markers growing `9.` → `10.`, `99.` → `100.`, re-indented break lines, escapes and blanks moved),
    then Enter or Shift+Enter at a random caret or selection: each pick the selection left is sent, at its
    `@`; one inside the selection is gone."""
    import random

    rnd = random.Random(44)
    page, box = _parts(fake)
    sent = 0
    for _ in range(40):
        # Mostly a marker growing a digit: every break line after it moves.
        while (made := _atom_case(rnd, ("9. ", "99. ", "- "))) is None:
            pass
        text, _, carets, (first, last), _ = made
        spots = sorted({c[0] for c in carets if c[3].startswith(" ") or c[0] == last}, reverse=True)
        await box.fill(text)
        for spot in sorted(rnd.sample(spots, min(len(spots), rnd.choice([1, 2]))), reverse=True):
            await page.evaluate(
                f"(r => r.setSelectionRange({spot}, {spot}))(document.getElementById('reply'))"
            )
            await _pick(page, box, " @ann", ["Ann Lee"])
        value = await box.input_value()
        picks = [m.start() for m in re.finditer("@Ann Lee", value)]
        inside = {o for p in picks for o in range(p + 1, p + 9)}
        free = [o for o in range(first, len(value) - (len(text) - last) + 1) if o not in inside]
        a = rnd.choice([o for o in free if o < picks[-1]] or free)  # mostly before a pick
        b = rnd.choice([o for o in free if o >= a]) if rnd.random() < 0.3 else a
        await page.evaluate(f"(r => r.setSelectionRange({a}, {b}))(document.getElementById('reply'))")
        await box.press(rnd.choice(["Enter", "Enter", "Shift+Enter"]))
        out = await box.input_value()
        await box.press("Control+Enter")
        sent += 1

        def arrived(count: int = sent) -> bool:
            return len(fake.of("send")) == count

        await wait(arrived)
        verb = fake.of("send")[-1]
        left = [p for p in picks if not a <= p < b]
        at = [m.start() for m in re.finditer("@Ann Lee", out)]
        case = (value, a, b, out)
        assert verb["text"] == out and len(at) == len(left), case
        assert verb["mentions"] == [{**ANN, "at": n} for n in at], case


@BOTH
async def test_ctrl_z_takes_back_each_press(fake: Fake) -> None:
    """040-AC12 / AC15: each Enter and Shift+Enter is one undo step."""
    page, box = _parts(fake)
    await _select(page, "- a\n  - b", 9, 9)
    steps = [
        ("Shift+Enter", "- a\n  - b\\\n    "),
        ("Enter", "- a\n  - b\n  - "),
        ("Enter", "- a\n  - b\n- "),
        ("Enter", "- a\n  - b\n"),
    ]
    for key, want in steps:
        await box.press(key)
        assert await box.input_value() == want
    for want in [w for _, w in reversed(steps[:-1])] + ["- a\n  - b"]:
        await box.press("Control+z")
        assert await box.input_value() == want


@BOTH
async def test_enter_mid_item_splits_it(fake: Fake) -> None:
    """040-AC13: the text after the caret moves into the new item."""
    page, _ = _parts(fake)
    assert await _press(page, "- abc", 3) == ["- a\n- bc", 6, 6]
    assert await _press(page, "  3. abc", 6) == ["  3. a\n  4. bc", 12, 12]
    # a caret in the indent or the marker is at the item's text
    assert await _press(page, "- a", 0) == ["- \n- a", 5, 5]
    assert await _press(page, "  - a", 3) == ["  - \n  - a", 9, 9]


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "b", "key", "expected"),
    [
        ("- abc", 3, 4, "Enter", "- a\n- c"),
        ("- ab", 2, 3, "Enter", "- \n- b"),
        ("- a\\\n  bcd", 8, 9, "Enter", "- a\\\n  b\n- d"),  # on a break's line
        ("- a\\\n  bc", 7, 8, "Enter", "- a\n- c"),  # its text's start: the separator goes too
        ("- abc", 3, 4, "Shift+Enter", "- a\\\n  c"),
        ("- a\\\n  bcd", 8, 9, "Shift+Enter", "- a\\\n  b\\\n  d"),
        ("- ab- c", 3, 4, "Shift+Enter", "- a\\\n  \\- c"),  # a marker-like start stays text
    ],
)
async def test_a_selection_within_a_lines_text_is_replaced_by_the_split(
    fake: Fake, text: str, a: int, b: int, key: str, expected: str
) -> None:
    """040-AC13 / AC15: Enter or Shift+Enter over a selection within one line's text replaces it with the
    split or the break, one undo step; nothing sent."""
    page, box = _parts(fake)
    assert (await _press(page, text, a, b, key=key))[0] == expected
    kept = compose_html(text[:a] + text[b:], [], {}, full=True)[0]
    assert _content(compose_html(expected, [], {}, full=True)[0]) == _content(kept)  # the same text, in order
    await box.press("Control+z")
    assert await box.input_value() == text
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "key", "expected", "html"),
    [
        ("- a\\\n  \\- b", 7, "Enter", "- a\n- - b", "<ul><li>a</li><li>- b</li></ul>"),  # the escape goes
        ("- a\\\n  \\- b", 8, "Enter", "- a\n- - b", "<ul><li>a</li><li>- b</li></ul>"),  # inside it
        ("- a\\\n  \\1. b", 4, "Enter", "- a\n- 1. b", "<ul><li>a</li><li>1. b</li></ul>"),
        ("- a - b", 4, "Shift+Enter", "- a \\\n  \\- b", "<ul><li>a <br>- b</li></ul>"),  # one comes
        ("1. a 2. b", 5, "Shift+Enter", "1. a \\\n   \\2. b", "<ol><li>a <br>2. b</li></ol>"),
        ("- - b", 2, "Shift+Enter", "- \\\n  \\- b", "<ul><li><br>- b</li></ul>"),
    ],
)
async def test_a_marker_like_text_keeps_its_reading_across_a_split_or_a_break(
    fake: Fake, text: str, a: int, key: str, expected: str, html: str
) -> None:
    """040-AC13 / AC15 / AC18: text that looks like a marker loses its escape on an item line and gains one at
    a break line's start, so it posts as the same text."""
    page, _ = _parts(fake)
    assert (await _press(page, text, a, key=key))[0] == expected
    assert compose_html(expected, [], {}, full=True)[0] == html


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "b", "key", "expected", "html"),
    [
        ("- a - b", 3, 3, "Shift+Enter", "- a\\\n  \\- b", "<ul><li>a<br>- b</li></ul>"),
        ("- a  - b", 3, 3, "Shift+Enter", "- a\\\n  \\- b", "<ul><li>a<br>- b</li></ul>"),
        ("1. a 2. b", 4, 4, "Shift+Enter", "1. a\\\n   \\2. b", "<ol><li>a<br>2. b</li></ol>"),
        (
            "- a\\\n  bcd",
            5,
            9,
            "Enter",
            "- a\n- d",
            "<ul><li>a</li><li>d</li></ul>",
        ),  # Shift+Home on a break line
        ("- abc", 0, 4, "Enter", "- \n- c", "<ul><li></li><li>c</li></ul>"),  # Shift+Home on the item line
        ("- ab", 1, 3, "Enter", "- \n- b", "<ul><li></li><li>b</li></ul>"),  # from the marker
        ("- a\\\n  \\- bc", 6, 11, "Enter", "- a\n- c", "<ul><li>a</li><li>c</li></ul>"),  # from the indent
    ],
)
async def test_a_separator_indent_or_marker_in_the_way_is_structure_not_text(
    fake: Fake, text: str, a: int, b: int, key: str, expected: str, html: str
) -> None:
    """040-AC13 / AC15 / AC18 (review round 3): whitespace before marker-like text never joins a break line's
    indent, and a selection from an indent or a marker counts from the item's text: no `\\` stranded, no
    marker lost."""
    page, _ = _parts(fake)
    assert (await _press(page, text, a, b, key=key))[0] == expected
    assert compose_html(expected, [], {}, full=True)[0] == html


@BOTH
@pytest.mark.parametrize("k", range(1, 7))
@pytest.mark.parametrize("marker", ["- ", "1. ", "* ", "> "])
async def test_a_backslash_run_before_a_marker_shows_the_same_on_either_line(
    fake: Fake, k: int, marker: str
) -> None:
    """040-AC13 / AC15 / AC18: 1 to 6 backslashes before marker-like text show as many after it moves from an
    item line to a break line (Shift+Enter) and back (Enter)."""
    page, _ = _parts(fake)
    text = "- a " + "\\" * k + marker + "b"
    before = compose_html(text, [], {}, full=True)[0]
    out = (await _press(page, text, 3, key="Shift+Enter"))[0]
    broken = compose_html(out, [], {}, full=True)[0]
    assert broken.replace("<br>", " ") == before, out
    split = (await _press(page, out, out.index("\n") + 1))[0]  # at the break line's start
    assert compose_html(split, [], {}, full=True)[0] == broken.replace("<br>", "</li><li>"), split


@BOTH
@pytest.mark.parametrize(
    ("a", "key", "expected"),
    [
        (4, "Enter", "- a\n- b"),  # right after the `\`
        (5, "Enter", "- a\n- b"),  # in the break line's indent
        (7, "Enter", "- a\n- b"),  # at its text
        (4, "Shift+Enter", "- a\\\n  \\\n  b"),  # an empty line between the two breaks
        (6, "Shift+Enter", "- a\\\n  \\\n  b"),
        (1, "Shift+Enter", "- \\\n  a\\\n  b"),  # in the marker: at the item's text
    ],
)
async def test_a_caret_at_a_line_break_is_at_the_next_lines_text(
    fake: Fake, a: int, key: str, expected: str
) -> None:
    """040-AC13 / AC15 / AC18: a break's `\\`, newline and indent are one separator: Enter there opens the
    next item with the next line's text, Shift+Enter puts an empty line between; no `\\` is left alone."""
    page, _ = _parts(fake)
    text = "- a\\\n  b"
    assert (await _press(page, text, a, key=key))[0] == expected
    assert "\\</li>" not in compose_html(expected, [], {}, full=True)[0]


@BOTH
async def test_a_separator_whose_next_number_is_no_item_is_kept(fake: Fake) -> None:
    """040-AC14: Enter at a break of `999999999.` opens nothing and leaves the break whole, never sends."""
    page, _ = _parts(fake)
    text = "999999999. a\\\n           b"
    assert (await _press(page, text, 13))[0] == text
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "b", "expected"),
    [
        ("999999999. a", 12, None, "999999999. a"),  # its next number is not an item: nothing
        ("- ab\n- cd", 3, 8, "- a\nd"),  # a selection across items: a newline
        ("- ab\nx", 3, 6, "- a\n"),  # one reaching plain text
    ],
)
async def test_enter_on_a_list_line_it_cannot_continue_is_a_newline(
    fake: Fake, text: str, a: int, b: int | None, expected: str
) -> None:
    """040-AC14: on a list line, an Enter that opens no item still never sends: a plain newline or
    nothing."""
    page, _ = _parts(fake)
    assert (await _press(page, text, a, b))[0] == expected
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize(
    "text",
    [
        "plain",
        "```\n- a\n```",  # inside a fence (the caret after `a` below)
        " - a",  # a no-break space is text, not indent
        "\\- a",  # escaped: literal text
        "> - a",  # a quote
        "  text",  # indented text
        "-a",
        "- a\\\n b",  # under the item's text: not its line
        "- a\\\n\n  b",  # a blank line ends the item
    ],
)
async def test_enter_outside_a_list_sends_as_before(fake: Fake, text: str) -> None:
    """040-AC14: off a list Enter sends; Shift+Enter is the browser's newline."""
    page, box = _parts(fake)
    caret = 7 if text.startswith("```") else len(text)
    assert (await _press(page, text, caret, key="Shift+Enter"))[0] == text[:caret] + "\n" + text[caret:]
    await _select(page, text, caret, caret)
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == text


@BOTH
async def test_enter_on_an_emptied_item_then_sends(fake: Fake) -> None:
    """040-AC14: Enter on an empty top-level item ends the list, the next Enter sends it."""
    page, box = _parts(fake)
    await _select(page, "- a", 3, 3)
    await box.press("Enter")
    await box.press("Enter")
    await fake.settled()
    assert fake.of("send") == []
    await box.press("Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    assert fake.of("send")[0]["text"] == "- a\n"


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "expected", "html"),
    [
        ("  - b", 5, "  - b\\\n    ", None),
        ("1. ab", 4, "1. a\\\n   b", "<ol><li>a<br>b</li></ol>"),  # the text after the caret moves down
        ("- a\\\n   b", 9, "- a\\\n   b\\\n   ", None),  # on a break's line: its own indent
        ("- a\\\n  b", 8, "- a\\\n  b\\\n  ", None),
        ("- a\\\n  ", 7, "- a\\\n  \\\n  ", None),  # on an empty break line
        ("- a\\\n  b\\\n  c", 13, "- a\\\n  b\\\n  c\\\n  ", None),
    ],
)
async def test_shift_enter_on_an_item_breaks_its_line(
    fake: Fake, text: str, a: int, expected: str, html: str | None
) -> None:
    """040-AC15 / AC16: Shift+Enter on an item or its break's line: `\\`, a new line at the item's text;
    typed on, the item composes as one `<li>` with a `<br>`."""
    page, _ = _parts(fake)
    caret = a + len(expected) - len(text)
    assert await _press(page, text, a, key="Shift+Enter") == [expected, caret, caret]
    if html:
        assert compose_html(expected, [], {}, full=True)[0] == html
    else:
        assert "<br>Q</li>" in compose_html(expected + "Q", [], {}, full=True)[0]
    await fake.settled()
    assert fake.of("send") == []


@BOTH
@pytest.mark.parametrize(
    ("text", "a", "expected", "depth"),
    [
        ("- a\\\n  b", 8, "- a\\\n  b\n- ", 1),
        ("  1. a\\\n     b", 14, "  1. a\\\n     b\n  2. ", 1),
        ("- a\\\n  bc", 8, "- a\\\n  b\n- c", 1),  # mid-line: the rest moves into the new item
        ("- a\\\n  ", 7, "- a\n- ", 1),  # an empty break line and its `\\` give way
        ("- x\n  - a\\\n    ", 15, "- x\n  - a\n  - ", 2),
    ],
)
async def test_enter_on_a_breaks_line_opens_the_next_item(
    fake: Fake, text: str, a: int, expected: str, depth: int
) -> None:
    """040-AC18: Enter on an item's break line opens the next item at the item's level."""
    page, _ = _parts(fake)
    assert (await _press(page, text, a))[0] == expected
    head, last = expected.rsplit("\n", 1)
    assert _depth(head + "\n" + re.sub(r"^([ \t]*(?:[-*]|[0-9]+\.) ).*", r"\1Q", last)) == depth
    await fake.settled()
    assert fake.of("send") == []


@BOTH
async def test_a_mention_after_the_caret_survives_a_split_a_level_up_and_a_break(fake: Fake) -> None:
    """040-AC13 / AC15 / 033: the picks after the caret move with the text, through splits, a level up and
    a line break."""
    page, box = _parts(fake)
    await box.fill("- x\n  - ab ")
    await box.press("End")
    await box.press_sequentially("@ann")
    await shows(page, ["Ann Lee"])
    await box.press("Enter")
    caret = "(r => r.setSelectionRange(9, 9))(document.getElementById('reply'))"
    await page.evaluate(caret)
    await box.press("Enter")
    assert await box.input_value() == "- x\n  - a\n  - b @Ann Lee "
    await page.evaluate(caret)
    await box.press("Enter")
    await box.press("Enter")  # the empty `  - ` just split off goes up
    assert await box.input_value() == "- x\n  - a\n- \n  - b @Ann Lee "
    await page.evaluate("(r => r.setSelectionRange(18, 18))(document.getElementById('reply'))")
    await box.press("Shift+Enter")
    text = "- x\n  - a\n- \n  - b\\\n    @Ann Lee "  # a break line's leading space would join its indent
    assert await box.input_value() == text
    await box.press("Control+Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    sent = fake.of("send")[0]
    assert sent["text"] == text and sent["mentions"] == [{**ANN, "at": text.index("@")}]
    html, _ = compose_html(text, sent["mentions"], {}, full=True)
    assert "b<br>" in html and "Ann Lee</span>" in html


# Test-only: keeps the composer's pick map (chat id → {text, picks}) when the page first writes it, so a test
# reads the raw anchors, which `mentions()` filters (an anchor off its `@` would pass unseen).
_PICKS = """(() => { const set = Map.prototype.set;
  Map.prototype.set = function (key, value) {
    if (value && Array.isArray(value.picks) && "text" in value) window.__picked = this;
    return set.call(this, key, value); }; })()"""


@pytest.fixture
async def hooked(pages: Pages) -> AsyncIterator[Fake]:
    board = Fake()
    page = await _open(pages, board, init=_PICKS)
    await page.locator("#reply").focus()
    yield board
    await pages.close()
    assert pages.external == []


async def _anchors(page: Page) -> list[int]:
    return list(await page.evaluate("[...window.__picked.values()].flatMap((s) => s.picks.map((p) => p.at))"))


async def _send(fake: Fake, box: Any) -> list[Any]:
    await box.press("Control+Enter")
    await wait(lambda: len(fake.of("send")) == 1)
    return list(fake.of("send")[0]["mentions"])


@BOTH
@pytest.mark.parametrize("key", ["Enter", "Shift+Enter"])
@pytest.mark.parametrize("start", ["- ab", "9. ab", "- x\\\n  ab", "9. x\\\n   ab"])
async def test_an_anchor_right_after_what_a_key_removes_stays_on_its_at(
    hooked: Fake, start: str, key: str
) -> None:
    """040-AC13 / AC15 / 033: the caret right before ` @Ann Lee` (Shift+Enter drops that blank: the removed
    span ends at the `@`; `9.` → `10.` moves the break lines): the pick stays on its `@`,
    raw anchor and sent."""
    page, box = _parts(hooked)
    await box.fill(start + " cd")
    await page.evaluate(
        f"(r => r.setSelectionRange({len(start)}, {len(start)}))(document.getElementById('reply'))"
    )
    await _pick(page, box, " @ann", ["Ann Lee"])
    await page.evaluate(
        f"(r => r.setSelectionRange({len(start)}, {len(start)}))(document.getElementById('reply'))"
    )
    await box.press(key)
    at = (await box.input_value()).index("@Ann Lee")
    assert await _anchors(page) == [at]
    assert await _send(hooked, box) == [{**ANN, "at": at}]


@BOTH
@pytest.mark.parametrize("key", ["Enter", "Shift+Enter"])
@pytest.mark.parametrize("start", ["- ab", "9. x\\\n   ab"])
async def test_a_pick_inside_a_removed_selection_goes(hooked: Fake, start: str, key: str) -> None:
    """040-AC13 / 033: a selection covering a whole pick, then Enter or Shift+Enter: its anchor goes, not
    moved onto other text; an `@Ann Lee` typed after is no mention."""
    page, box = _parts(hooked)
    await box.fill(start + " cd")
    await page.evaluate(
        f"(r => r.setSelectionRange({len(start)}, {len(start)}))(document.getElementById('reply'))"
    )
    await _pick(page, box, " @ann", ["Ann Lee"])
    end = (await box.input_value()).index("cd")
    await page.evaluate(f"(r => r.setSelectionRange({len(start)}, {end}))(document.getElementById('reply'))")
    await box.press(key)
    assert await _anchors(page) == []
    await box.press_sequentially("@Ann Lee ")
    await box.press("Escape")
    assert await _send(hooked, box) == []
