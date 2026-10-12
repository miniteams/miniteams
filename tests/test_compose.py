"""Composer text → Teams HTML: markdown subset, emojis, mentions (spec 012)."""

import gc
import html
import json
import time
from collections.abc import Callable
from typing import Any

import pytest

from miniteams import compose, send
from miniteams.compose import compose_html

ANN = {"name": "Ann Lee", "mri": "8:orgid:ann"}
EVERYONE = {"name": "everyone", "mri": "19:grp@thread.v2", "everyone": True}


def _md(text: str, full: bool) -> str:
    return compose_html(text, [], {}, full=full)[0]


def _span(n: int, text: str) -> str:
    return f'<span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="{n}">{text}</span>'


def _mentions(props: dict[str, Any] | None) -> list[dict[str, Any]]:
    assert props is not None and set(props) == {"mentions"}
    entries: list[dict[str, Any]] = json.loads(props["mentions"])
    return entries


# --- emojis ---------------------------------------------------------------------------------------


def test_known_shortcode_expands_unknown_stays() -> None:
    """012-AC3: `ok :fire: :nope:` → `ok 🔥 :nope:`, no properties."""
    assert compose_html("ok :fire: :nope:", [], {}) == ("ok 🔥 :nope:", None)


def test_custom_emoji_becomes_the_teams_img() -> None:
    """012-AC3: a custom name → the CustomEmoji <img> with `<name>;<id>` and the asyncgw src."""
    html, _ = compose_html("go :party:!", [], {"party": "0-frc-d1-abc"}, asyncgw_host="gw.example")
    assert html == (
        'go <img alt="party" itemid="party;0-frc-d1-abc" itemtype="http://schema.skype.com/CustomEmoji"'
        ' src="https://gw.example/v1/objects/0-frc-d1-abc/views/imgt2_anim"'
        ' style="width:20px; height:20px">!'
    )
    assert compose_html(":party:", [], {"party": "x"})[0].count(compose._ASYNCGW_HOST) == 1


def test_escaping_is_unchanged_around_emojis() -> None:
    """012-AC3: `<` → `&lt;`, newline → `<br>`, as `message_html` does today."""
    assert compose_html("a<b :fire:\nc&d", [], {})[0] == "a&lt;b 🔥<br>c&amp;d"
    plain = "a<b> & 'q' \"x\"\nline two"
    assert _md(plain, False) == _md(plain, True) == send.message_html(plain, False)


@pytest.mark.parametrize("text", [":)", "at 10:30:", "a :: b", ": fire:", ":fire :", "::"])
def test_colons_that_are_not_a_known_name_stay(text: str) -> None:
    assert compose_html(text, [], {})[0] == text


def test_unknown_name_gives_its_closing_colon_back() -> None:
    assert compose_html("10:30:fire: :fire::zap:", [], {})[0] == "10:30🔥 🔥⚡"


# --- mentions -------------------------------------------------------------------------------------


def test_every_occurrence_is_a_numbered_mention() -> None:
    """012-AC4: two `@Ann Lee` → spans 0 and 1, two entries in text order."""
    html, props = compose_html("@Ann Lee and @Ann Lee", [ANN], {})
    assert html == f"{_span(0, 'Ann Lee')} and {_span(1, 'Ann Lee')}"
    assert _mentions(props) == [
        {
            "@type": "http://schema.skype.com/Mention",
            "itemid": n,
            "mri": "8:orgid:ann",
            "mentionType": "person",
            "displayName": "Ann Lee",
        }
        for n in (0, 1)
    ]


def test_namesakes_each_get_their_own_mention_in_text_order() -> None:
    """L11: two people sharing a display name: the n-th `@Name` notifies the n-th pick, extras the last."""
    one, two = ({"name": "Ann Lee", "mri": f"8:orgid:{n}"} for n in ("one", "two"))
    html, props = compose_html("@Ann Lee then **@Ann Lee** and @Ann Lee", [one, two], {})
    assert html == f"{_span(0, 'Ann Lee')} then <b>{_span(1, 'Ann Lee')}</b> and {_span(2, 'Ann Lee')}"
    assert [(e["itemid"], e["mri"]) for e in _mentions(props)] == [
        (0, "8:orgid:one"),
        (1, "8:orgid:two"),
        (2, "8:orgid:two"),
    ]
    _, props = compose_html("hi @Ann Lee, @Ann Lee", [two, one], {})
    assert [e["mri"] for e in _mentions(props)] == ["8:orgid:two", "8:orgid:one"]


def test_picked_mention_absent_from_text_is_dropped() -> None:
    """012-AC4: a picked mention whose `@Name` is gone yields no entry."""
    assert compose_html("hello Ann Lee", [ANN], {}) == ("hello Ann Lee", None)
    _, props = compose_html("@Bob Ray hi", [ANN, {"name": "Bob Ray", "mri": "8:orgid:bob"}], {})
    assert [e["mri"] for e in _mentions(props)] == ["8:orgid:bob"]


def test_everyone_mention() -> None:
    """012-AC5: `@everyone` → a span reading `Everyone`, mentionType everyone, mri = thread id."""
    html, props = compose_html("hi @everyone!", [EVERYONE], {})
    assert html == f"hi {_span(0, 'Everyone')}!"
    assert _mentions(props) == [
        {
            "@type": "http://schema.skype.com/Mention",
            "itemid": 0,
            "mri": "19:grp@thread.v2",
            "mentionType": "everyone",
            "displayName": "Everyone",
        }
    ]


def test_longest_name_wins_and_a_prefix_name_still_matches_alone() -> None:
    ann = {"name": "Ann", "mri": "8:orgid:a"}
    html, props = compose_html("@Ann Lee, @Ann, @Ann Leeds, @Annie", [ann, ANN], {})
    assert html == f"{_span(0, 'Ann Lee')}, {_span(1, 'Ann')}, {_span(2, 'Ann')} Leeds, @Annie"
    assert [e["mri"] for e in _mentions(props)] == ["8:orgid:ann", "8:orgid:a", "8:orgid:a"]


def test_name_with_regex_and_html_metacharacters() -> None:
    team = {"name": "A+ (team) <qa>", "mri": "8:orgid:t"}
    html, props = compose_html("@A+ (team) <qa> and @AA+ (team) <qa>", [team], {})
    assert html == f"{_span(0, 'A+ (team) &lt;qa&gt;')} and @AA+ (team) &lt;qa&gt;"
    assert len(_mentions(props)) == 1


def test_at_inside_a_word_is_not_a_mention() -> None:
    assert compose_html("mail bob@Ann Lee", [ANN], {}) == ("mail bob@Ann Lee", None)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`@Ann Lee :fire:`", "<code>@Ann Lee :fire:</code>"),
        ("[@Ann Lee :fire:](https://x.test)", '<a href="https://x.test">@Ann Lee :fire:</a>'),
    ],
)
def test_no_expansion_inside_code_or_link_labels(text: str, expected: str) -> None:
    assert compose_html(text, [ANN], {}) == (expected, None)


@pytest.mark.parametrize(
    "text",
    [
        "see https://x.test/a:fire:b now",
        "see https://x.test/@Ann Lee now",
        "https://x.test/:zap:",
        ":fire:https://x.test/:x:",
    ],
)
def test_no_expansion_inside_bare_urls(text: str) -> None:
    html, props = compose_html(text, [ANN], {"x": "1"})
    assert props is None
    assert html == text.replace(":fire:", "🔥", text.startswith(":fire:"))
    assert compose_html(text + " :fire:", [], {})[0].endswith(" 🔥")


def test_mention_fields_are_coerced_to_strings() -> None:
    _, props = compose_html("@7 hi", [{"name": 7, "mri": 42}], {})
    assert _mentions(props)[0] | {"@type": ""} == {
        "@type": "",
        "itemid": 0,
        "mri": "42",
        "mentionType": "person",
        "displayName": "7",
    }


def test_mention_list_and_names_are_capped() -> None:
    many = [{"name": f"N{n}", "mri": f"m{n}"} for n in range(compose.MAX_MENTIONS)]
    compose_html("@N0", many, {})
    with pytest.raises(ValueError, match="too many mentions"):
        compose_html("@N0", [*many, {"name": "Extra", "mri": "x"}], {})
    compose_html("hi", [{"name": "x" * compose.MAX_NAME, "mri": "m"}], {})
    with pytest.raises(ValueError, match="name too long"):
        compose_html("hi", [{"name": "x" * (compose.MAX_NAME + 1), "mri": "m"}], {})
    with pytest.raises(ValueError, match="name too long"):
        compose_html("hi", [{"name": "x", "label": "x" * (compose.MAX_NAME + 1), "mri": "m"}], {})


def test_mention_label_is_shown_name_is_matched() -> None:
    """`name` finds `@name` in the text; `label` is what the span and displayName read."""
    html, props = compose_html("@ann lee hi", [{"name": "ann lee", "label": "Ann Lee", "mri": "m"}], {})
    assert html.endswith('itemid="0">Ann Lee</span> hi')
    assert props is not None and json.loads(props["mentions"])[0]["displayName"] == "Ann Lee"


def test_mentions_and_emojis_inside_formatting_still_expand() -> None:
    html, props = compose_html("**@Ann Lee** _:fire:_", [ANN], {})
    assert html == f"<b>{_span(0, 'Ann Lee')}</b> <i>🔥</i>"
    assert len(_mentions(props)) == 1


# --- formatting -----------------------------------------------------------------------------------


def test_inline_set() -> None:
    """012-AC11: bold, both italics, strike, code, https link."""
    assert _md("**a** *b* _c_ ~~d~~ `e` [f](https://x)", False) == (
        '<b>a</b> <i>b</i> <i>c</i> <s>d</s> <code>e</code> <a href="https://x">f</a>'
    )


def test_inline_mode_leaves_block_syntax_as_text() -> None:
    """012-AC11: with `inline`, `> q` and `- item` stay text."""
    assert _md("> q\n- item\n1. one\n```\nx\n```", False) == ("&gt; q<br>- item<br>1. one<br>```<br>x<br>```")


def test_full_fenced_block_is_escaped_and_never_expanded() -> None:
    """012-AC12: fenced block → <pre><code>, content escaped, no emoji or mention inside."""
    html, props = compose_html(
        "see:\n```py\n<a> :fire: @Ann Lee **b**\n  x\n```\nend :fire:", [ANN], {}, full=True
    )
    assert html == "see:<pre><code>&lt;a&gt; :fire: @Ann Lee **b**\n  x</code></pre>end 🔥"
    assert props is None


def test_full_quotes_and_lists() -> None:
    """012-AC12: `> ` lines → <blockquote>, `- `/`* ` runs → <ul>, `1. ` runs → <ol>."""
    text = "intro\n> one\n> **two**\n- a\n* b\n1. x\n12. y\ntail\nlast"
    assert _md(text, True) == (
        "intro<blockquote>one<br><b>two</b></blockquote><ul><li>a</li><li>b</li></ul>"
        "<ol><li>x</li><li>y</li></ol>tail<br>last"
    )


def test_unclosed_fence_stays_text() -> None:
    assert _md("```\ncode", True) == "```<br>code"


@pytest.mark.parametrize(
    "text",
    [
        "2*3*4",
        "snake_case_name",
        "**",
        "a ** b",
        "**open",
        "`open",
        "~~open",
        "_open",
        "* not italic *",
        "x*y*",
        "*a*b",
        "2**3**4",
        "a**b**",
        "**a**b",
        "_a_b c",
        "[x](ftp://host)",
        "[](https://x)",
        "[x] (https://x)",
        "*a *",
        "**b **",
        "_a _",
    ],
)
def test_unclosed_or_intra_word_markers_stay_as_typed(text: str) -> None:
    """012-AC13: unclosed, intra-word or non-http markup is left as typed."""
    assert _md(text, False) == text


def test_javascript_link_and_typed_html_are_text() -> None:
    """012-AC13: `[x](javascript:…)` stays text, `<b>` typed by hand is escaped."""
    assert _md("[x](javascript:alert(1)) <b>hi</b>", True) == (
        "[x](javascript:alert(1)) &lt;b&gt;hi&lt;/b&gt;"
    )
    assert _md('[x](https://a.test/"onmouseover=y)', False) == "[x](https://a.test/&quot;onmouseover=y)"


def test_inline_code_is_escaped() -> None:
    assert _md("`<b>&`", False) == "<code>&lt;b&gt;&amp;</code>"


def test_link_url_may_hold_one_balanced_paren_pair() -> None:
    assert _md("[w](https://w.test/wiki/Foo_(bar)) x", False) == (
        '<a href="https://w.test/wiki/Foo_(bar)">w</a> x'
    )
    assert _md("[w](https://w.test/a(b)c) x", False) == '<a href="https://w.test/a(b)c">w</a> x'
    for literal in ("[w](https://w.test/(a)(b))", "[w](https://w.test/((a))", "[w](https://w.test/(a"):
        assert _md(literal, False) == literal


@pytest.mark.parametrize("full", [False, True])
def test_no_markdown_inside_bare_urls(full: bool) -> None:
    assert _md("https://x.test/_a_ https://x.test/*b* *c*", full) == (
        "https://x.test/_a_ https://x.test/*b* <i>c</i>"
    )
    assert _md("https:// _d_", full) == "https:// <i>d</i>"


def test_link_href_is_attribute_escaped() -> None:
    assert _md("[a&b](https://x.test/?q=1&r=2)", False) == (
        '<a href="https://x.test/?q=1&amp;r=2">a&amp;b</a>'
    )


def test_nested_markers() -> None:
    assert _md("**bold _it_ ~~gone~~**", False) == "<b>bold <i>it</i> <s>gone</s></b>"
    assert _md("**a `**` b**", False) == "<b>a `</b>` b**"  # first closer wins, still linear


def test_input_is_capped() -> None:
    """The cap is Teams' 28 KB: a text at the limit composes, one character more does not."""
    assert compose.MAX_TEXT == 28 * 1024
    assert compose_html("x" * compose.MAX_TEXT, [], {})[0] == "x" * compose.MAX_TEXT
    with pytest.raises(ValueError, match="too long"):
        _md("x" * (compose.MAX_TEXT + 1), False)
    with pytest.raises(ValueError, match="too long"):
        compose_html("x" * (compose.MAX_TEXT + 1), [], {})


def _best_ms(fn: Callable[[], object]) -> float:
    gc.disable()
    try:
        runs = []
        for _ in range(7):  # best of 7: a 1 ms run is at the mercy of one scheduler hiccup
            start = time.process_time()  # CPU time: a loaded host preempts long runs more
            fn()
            runs.append(time.process_time() - start)
    finally:
        gc.enable()
    return min(runs) * 1000


HOSTILE = [*"*_~`[", "**a", "[x](", ":a", "@A ", "a*", "[(", "x https://h.t/"]


@pytest.mark.parametrize("unit", HOSTILE)
def test_hostile_runs_stay_linear(unit: str) -> None:
    """4× a hostile input must not take ~16× the time (quadratic); linear is ~4."""

    def ratio(fn: Callable[[str], object]) -> float:
        small, big = (unit * (n // len(unit)) for n in (7_000, 28_000))
        return _best_ms(lambda: fn(big)) / max(_best_ms(lambda: fn(small)), 0.05)

    assert ratio(lambda t: _md(t, True)) < 8
    assert ratio(lambda t: compose_html(t, [{"name": "A", "mri": "m"}], {"a": "x"})) < 8


@pytest.mark.parametrize("unit", ["[x](", "[("])
def test_unclosed_links_scan_each_line_once(unit: str) -> None:
    """Beyond the 28 KB cap the per-line scan is what keeps `[` linear: 4× an unclosed `[x](` or `[(` run
    re-scanning to the end of the line costs ~16×, a single scan ~4×."""
    small, big = (unit * (n // len(unit)) for n in (40_000, 160_000))
    slow = _best_ms(lambda: compose._inline(big, html.escape))
    assert slow / _best_ms(lambda: compose._inline(small, html.escape)) < 8


def test_labels_sharing_one_bad_url_stay_linear() -> None:
    def text(n: int) -> str:
        return "[" * n + "](https://" + "a" * n + " )"

    assert _best_ms(lambda: _md(text(14_000), False)) / _best_ms(lambda: _md(text(3_500), False)) < 8


def test_reply_quote_preview_skips_every_reply_block_of_the_quoted_message() -> None:
    """L6: a message that itself quotes two others previews its own words, else its full text."""

    def block(n: int) -> str:
        quoted = {"id": str(n), "messagetype": "RichText/Html", "content": f"<p>quoted {n}</p>"}
        return compose.reply_quote(quoted, "Bob")

    own = {"id": "9", "messagetype": "RichText/Html", "content": block(1) + block(2) + "<p>mine</p>"}
    assert '<p itemprop="preview">mine</p>' in compose.reply_quote(own, "Ann")
    only = {"id": "9", "messagetype": "RichText/Html", "content": block(1) + block(2)}
    assert '<p itemprop="preview">Bob quoted 1 Bob quoted 2</p>' in compose.reply_quote(only, "Ann")
