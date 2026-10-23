"""Composer text → Teams HTML: markdown subset, emojis, mentions (spec 012)."""

import contextlib
import gc
import html
import json
import re
import time
import unicodedata
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


def _a(url: str) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(url)}</a>'


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
    assert compose_html(":nope::fire: :fire::zap:", [], {})[0] == ":nope:🔥 🔥⚡"


@pytest.mark.parametrize("text", ["a:b:c", "status:ok: done", "fe80::1:a:b", "10:30:fire:", "C:\\x:y"])
def test_a_name_glued_to_a_word_stays(text: str) -> None:
    """Built-in names include `a`, `b`, `ok`, `x`: only a `:` after a space or a mark opens one."""
    assert compose_html(text, [], {})[0] == text


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
    ("text", "expected"),
    [
        ("see https://x.test/a:fire:b now", f"see {_a('https://x.test/a:fire:b')} now"),
        ("see https://x.test/@Ann Lee now", f"see {_a('https://x.test/@Ann')} Lee now"),
        ("https://x.test/:zap:", f"{_a('https://x.test/:zap')}:"),
        (":fire:https://x.test/:x:", f"🔥{_a('https://x.test/:x')}:"),
    ],
)
def test_no_expansion_inside_bare_urls(text: str, expected: str) -> None:
    html, props = compose_html(text, [ANN], {"x": "1"})
    assert props is None
    assert html == expected
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
    assert _md('[x](https://a.test/"onmouseover=y)', False) == (
        f"[x]({_a('https://a.test/')}&quot;onmouseover=y)"
    )


def test_a_rejected_link_leaves_its_url_bare() -> None:
    """012-AC13, 028-AC1: what is not a `[label](url)` link keeps its URL a bare one."""
    assert _md("[](https://x)", False) == f"[]({_a('https://x')})"
    assert _md("[x] (https://x)", False) == f"[x] ({_a('https://x')})"


def test_inline_code_is_escaped() -> None:
    assert _md("`<b>&`", False) == "<code>&lt;b&gt;&amp;</code>"


def test_link_url_may_hold_one_balanced_paren_pair() -> None:
    assert _md("[w](https://w.test/wiki/Foo_(bar)) x", False) == (
        '<a href="https://w.test/wiki/Foo_(bar)">w</a> x'
    )
    assert _md("[w](https://w.test/a(b)c) x", False) == '<a href="https://w.test/a(b)c">w</a> x'
    for literal, url in (
        ("[w](https://w.test/(a)(b))", "https://w.test/(a)(b)"),
        ("[w](https://w.test/((a))", "https://w.test/((a))"),
        ("[w](https://w.test/(a", "https://w.test/(a"),
    ):
        assert _md(literal, False) == literal.replace(url, _a(url))


@pytest.mark.parametrize("full", [False, True])
def test_no_markdown_inside_bare_urls(full: bool) -> None:
    assert _md("https://x.test/_a_ https://x.test/*b* *c*", full) == (
        f"{_a('https://x.test/_a')}_ {_a('https://x.test/*b')}* <i>c</i>"
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


# --- backslash escapes (spec 019) ----------------------------------------------------------------


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"\*a\*", "*a*"),
        (r"\*\*b\*\*", "**b**"),
        (r"\_c\_", "_c_"),
        (r"\~\~d\~\~", "~~d~~"),
        (r"\`e\`", "`e`"),
        (r"\[f](https://x.test)", f"[f]({_a('https://x.test')})"),
        (r"\\\*", "\\*"),
        (r"\\`x`", "\\<code>x</code>"),
        (r"\\*g*", "\\<i>g</i>"),
        (r"2\*3*4 *x\*", "2*3*4 *x*"),
        (r"*a\*b*", "<i>a*b</i>"),
        (r"_c\_ d_ **e\** f**", "<i>c_ d</i> <b>e** f</b>"),
        (r"*x\\*", "<i>x\\</i>"),
        (r"\@x", "@x"),
        (r"\\@x", "\\@x"),
    ],
)
def test_each_escape_gives_the_literal_character(text: str, expected: str, full: bool) -> None:
    r"""019-AC5, 029-AC6: `\*`, `\_`, `\~`, `` \` ``, `\[`, `\@` are the character itself, in both formats;
    `\\` is one backslash before one of those."""
    assert _md(text, full) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"\- a", "- a"),
        (r"\* a", "* a"),
        (r"\> a", "&gt; a"),
        ("\\>", "&gt;"),
        (r"\1. a", "1. a"),
        (r"\123456789. a", "123456789. a"),
        ("x\n\\- a\n\\2. b", "x<br>- a<br>2. b"),
        (r"\\- a", "\\- a"),
    ],
)
def test_a_leading_backslash_keeps_a_block_marker_literal(text: str, expected: str) -> None:
    r"""019-AC5: in full format, `\` before `- `, `* `, `> `, `1. ` at a line start makes the line text,
    no list or quote; `\\` there is one backslash before the marker."""
    assert _md(text, True) == expected


@pytest.mark.parametrize("text", [r"\- a", r"\> a", r"\1. a", r"\\- a"])
def test_inline_format_has_no_line_start_escape(text: str) -> None:
    r"""019-AC5: the widget's inline format has no block markers: a leading `\` stays as typed."""
    assert _md(text, False) == html.escape(text)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"\\srv\share\_tmp", r"\\srv\share_tmp"),
        (r"\\srv\share", r"\\srv\share"),
        (r"grep \*\.py", r"grep *\.py"),
        (r"^\d+\s\\w$", r"^\d+\s\\w$"),
        (r"C:\\", r"C:\\"),
    ],
)
def test_paths_and_regexes_keep_their_backslashes(text: str, expected: str, full: bool) -> None:
    r"""019-AC5: a UNC path or a regex posts as typed but for the listed escapes (`\_`, `\*`)."""
    assert _md(text, full) == expected


@pytest.mark.parametrize(
    "text",
    [r"\a", r"C:\path\to", "end\\", r"a \- b", r"\-a", r"\1.a", r"\:fire:", "\\\n"],
)
def test_a_backslash_before_anything_else_stays(text: str) -> None:
    """019-AC5: only the listed characters (and a leading block marker) are escapable."""
    assert _md(text, True) == html.escape(text).replace("\n", "<br>").replace("\\:fire:", "\\🔥")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (">", "&gt;"),
        ("> ", "&gt; "),
        (">\n>", "&gt;<br>&gt;"),
        ("a\n>\nb", "a<br>&gt;<br>b"),
        ("> a\n>\n> b", "<blockquote>a<br><br>b</blockquote>"),
        ("> \n> a", "<blockquote><br>a</blockquote>"),
    ],
)
def test_a_quote_with_nothing_in_it_is_the_text_typed(text: str, expected: str) -> None:
    """019-AC5: `>` alone is what the user typed, never an empty quote; a blank line inside a quote stays."""
    assert _md(text, True) == expected


def test_escapes_are_not_read_inside_code_link_labels_or_urls() -> None:
    """019-AC5: code, a link's label and a bare URL keep their backslashes."""
    assert _md(r"`a\*b` [l\*](https://x.test) https://x.test/a\_b", True) == (
        f'<code>a\\*b</code> <a href="https://x.test">l\\*</a> {_a("https://x.test/a\\_b")}'
    )


def test_escaped_list_line_ends_a_list_and_quotes_keep_their_backslash() -> None:
    r"""019-AC5: an escaped line between list items splits the list; inside a quote `\- ` is plain text."""
    assert _md("- a\n\\- b\n- c\n> \\- q", True) == (
        "<ul><li>a</li></ul>- b<ul><li>c</li></ul><blockquote>\\- q</blockquote>"
    )


def test_an_escape_keeps_a_mention_or_emoji_from_expanding_across_it() -> None:
    """019-AC5: the escaped character is never part of a `:name:` or an `@Name`."""
    html_, props = compose_html(r"@Ann\_Lee :a\_b:", [{"name": "Ann_Lee", "mri": "m"}], {"a_b": "x"})
    assert (html_, props) == ("@Ann_Lee :a_b:", None)


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


HOSTILE = [*"*_~`[\\", "\\*", "*a\\*", "**a\\**", "**a", "[x](", ":a", "@A ", "a*", "[(", "x https://h.t/"]


@pytest.mark.parametrize("unit", HOSTILE)
def test_hostile_runs_stay_linear(unit: str) -> None:
    """4× a hostile input must not take ~16× the time (quadratic); linear is ~4."""

    def ratio(fn: Callable[[str], object]) -> float:
        small, big = (unit * (n // len(unit)) for n in (7_000, 28_000))
        return _best_ms(lambda: fn(big)) / max(_best_ms(lambda: fn(small)), 0.05)

    assert ratio(lambda t: _md(t, True)) < 8
    assert ratio(lambda t: _capped(t, [{"name": "A", "mri": "m"}], {"a": "x"})) < 8


def _capped(text: str, picks: list[dict[str, Any]], emojis: dict[str, str]) -> object:
    """compose_html, its MAX_MENTIONS refusal included: past it a mention-heavy text stops early."""
    try:
        return compose_html(text, picks, emojis)
    except ValueError as exc:
        return exc


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


# --- bare URLs as links (spec 028) ----------------------------------------------------------------

TRAILING = [*".,:;!?*_~'\")]}>", *"…–—«»‹›“”‘’"]


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("tail", [*TRAILING, ").", "?!…", "**.", "_~,", "’»."])
def test_a_bare_url_links_without_its_trailing_punctuation(tail: str, full: bool) -> None:
    """028-AC1: each trailing punctuation character, and runs of them, stay text after the link."""
    assert (
        _md(f"voici https://test.com/{tail}", full) == f"voici {_a('https://test.com/')}{html.escape(tail)}"
    )
    assert _md(f"https://test.com/a{tail} b", full) == f"{_a('https://test.com/a')}{html.escape(tail)} b"


@pytest.mark.parametrize("full", [False, True])
def test_bare_url_links_keep_what_is_inside_them(full: bool) -> None:
    """028-AC1: inner punctuation and a trailing `-` stay in the URL; a URL with nothing past its scheme
    stays text; formatting around a link still applies."""
    for url in ("https://test.com/a.b,c;d", "https://test.com/a-", "https://x.test/?q=1&r=2#f"):
        assert _md(f"see {url} now", full) == f"see {_a(url)} now"
    assert _md("https://. x", full) == "https://. x"
    assert _md("**see https://test.com/a**.", full) == f"<b>see {_a('https://test.com/a')}</b>."
    assert _md("_https://test.com/_", full) == f"<i>{_a('https://test.com/')}</i>"


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("https://fr.wikipedia.org/wiki/Foo_(bar)", "https://fr.wikipedia.org/wiki/Foo_(bar)"),
        ("(voir https://test.com/a)", "https://test.com/a"),
        ("(voir https://fr.wikipedia.org/wiki/Foo_(bar)).", "https://fr.wikipedia.org/wiki/Foo_(bar)"),
        ("https://x.test/a[1]]", "https://x.test/a[1]"),
        ("{https://x.test/{a}}}", "https://x.test/{a}"),
        ("https://x.test/a_(b)_", "https://x.test/a_(b)"),
        ("https://x.test/(a)(b))", "https://x.test/(a)(b)"),
        ("https://[x.test/a", None),
    ],
)
def test_a_closing_bracket_stays_when_the_url_opened_it(text: str, url: str | None) -> None:
    """028-AC2: GitHub's rule: a trailing `)`, `]`, `}` is cut only while it closes more than the URL opens;
    a URL no parser reads stays text."""
    if url is None:
        assert _md(text, True) == html.escape(text)
        return
    start = text.index("http")
    assert _md(text, True) == (html.escape(text[:start]) + _a(url) + html.escape(text[start + len(url) :]))


def test_trailing_cut_stays_linear() -> None:
    """A URL ending in a long run of closing brackets is cut in one pass."""
    big, small = ("https://h.t/" + ")" * n for n in (28_000, 7_000))
    assert _best_ms(lambda: compose.trim_url(big)) / max(_best_ms(lambda: compose.trim_url(small)), 0.05) < 8


@pytest.mark.parametrize("full", [False, True])
def test_an_apostrophe_inside_a_url_stays_in_it(full: bool) -> None:
    """028-AC1: only a trailing `'` is the sentence's: a quoted URL keeps its closing quote outside."""
    url = "https://en.wikipedia.org/wiki/Hitchhiker's_Guide"
    assert _md(f"see {url}.", full) == f"see {_a(url)}."
    assert _md("'quoted https://x.test/a'", full) == f"&#x27;quoted {_a('https://x.test/a')}&#x27;"


@pytest.mark.parametrize("full", [False, True])
def test_an_apostrophe_ends_the_host(full: bool) -> None:
    """028-AC1: `x.com's` is the host and a possessive: no `'` in a host name."""
    assert _md("see https://x.com's API", full) == f"see {_a('https://x.com')}&#x27;s API"
    assert _md("https://x.com:8080's", full) == f"{_a('https://x.com:8080')}&#x27;s"
    assert _md("https://x.com/it's", full) == _a("https://x.com/it's")


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    ("text", "urls"),
    [
        ("URL:https://x.com/a", ["https://x.com/a"]),
        ("Lien:https://x.com/a PR:https://y.com/b.", ["https://x.com/a", "https://y.com/b"]),
        ("curl 'https://a.com/x?q=1'|jq", ["https://a.com/x?q=1"]),
        ("['https://a.com/x','https://b.com/y']", ["https://a.com/x", "https://b.com/y"]),
    ],
)
def test_a_url_after_a_colon_or_inside_quotes_links_alone(text: str, urls: list[str], full: bool) -> None:
    """028-AC1: `URL:https://…` is no `:emoji:`; a `'` not followed by a letter or digit ends the URL."""
    assert re.findall(r'href="([^"]*)"', _md(text, full)) == [html.escape(u) for u in urls]


# --- mention resolution (spec 029) ---------------------------------------------------------------

GROUP = "19:grp@thread.v2"
ONE_TO_ONE = "19:a_b@unq.gbl.spaces"
ME = "8:orgid:me"
ROSTER = [
    {"mri": ME, "name": "Me Self"},
    {"mri": "8:orgid:ann", "name": "Ann Lee"},
    {"mri": "8:orgid:bob", "name": "Bob Roé"},
    {"mri": "28:bot", "name": "Bot"},
]
MOORE = {"mri": "8:orgid:moore", "name": "Ann Moore"}


def _resolved(text: str, roster: list[dict[str, Any]] = ROSTER, thread: str = GROUP) -> tuple[str, Any]:
    return compose_html(text, compose.resolve_mentions(text, roster, thread, ME), {}, full=True)


def test_a_full_name_mentions_the_member() -> None:
    """029-AC1: `@Ann Lee` → her span, itemid 0, and her MRI as a person mention."""
    html_, props = _resolved("@Ann Lee hi", [*ROSTER, MOORE])
    assert html_ == f"{_span(0, 'Ann Lee')} hi"
    assert _mentions(props) == [
        {
            "@type": "http://schema.skype.com/Mention",
            "itemid": 0,
            "mri": "8:orgid:ann",
            "mentionType": "person",
            "displayName": "Ann Lee",
        }
    ]


@pytest.mark.parametrize(
    ("text", "label"), [("@ann", "Ann"), ("@ANN LEE", "Ann Lee"), ("@bob roe", "Bob Roé"), ("@BOB", "Bob")]
)
def test_case_and_accents_are_ignored_and_the_roster_spelling_shows(text: str, label: str) -> None:
    """029-AC2: a first or full name, any case or accents, shows as the roster spells it."""
    html_, props = _resolved(f"{text}, hi")
    assert html_ == f"{_span(0, label)}, hi" and _mentions(props)[0]["displayName"] == label


def test_two_mentions_are_numbered_in_text_order() -> None:
    """029-AC2: `@Ann Lee` and `@Ann` give itemid 0 and 1, both Ann; longest match first."""
    html_, props = _resolved("@Ann Lee and @Ann, then @Bob")
    assert html_ == f"{_span(0, 'Ann Lee')} and {_span(1, 'Ann')}, then {_span(2, 'Bob')}"
    assert [(e["itemid"], e["mri"]) for e in _mentions(props)] == [
        (0, "8:orgid:ann"),
        (1, "8:orgid:ann"),
        (2, "8:orgid:bob"),
    ]


@pytest.mark.parametrize(
    ("text", "roster", "error"),
    [
        ("@Ann hi", [*ROSTER, MOORE], "ambiguous mention @Ann: Ann Lee, Ann Moore (\\@ for a literal @)"),
        ("hi @ann", [*ROSTER, MOORE], "ambiguous mention @ann: Ann Lee, Ann Moore (\\@ for a literal @)"),
        (
            "@Ann Lee",
            [*ROSTER, {"mri": "8:orgid:lee2", "name": "Ann Lee"}],
            "ambiguous mention @Ann Lee: Ann Lee, Ann Lee (\\@ for a literal @)",
        ),
        ("@Nobody here", ROSTER, "unknown mention @Nobody (\\@ for a literal @)"),
        ("@Me Self", ROSTER, "unknown mention @Me (\\@ for a literal @)"),
        ("@Bot", ROSTER, "unknown mention @Bot (\\@ for a literal @)"),
        ("@Annabel", ROSTER, "unknown mention @Annabel (\\@ for a literal @)"),
    ],
)
def test_a_name_matching_none_or_several_is_refused(
    text: str, roster: list[dict[str, Any]], error: str
) -> None:
    """029-AC3, AC4: ambiguity names every candidate; nobody, me or a bot is unknown."""
    with pytest.raises(ValueError, match=f"^{re.escape(error)}$"):
        compose.resolve_mentions(text, roster, GROUP, ME)


def test_everyone_is_the_chat_on_a_group_and_refused_on_a_one_to_one() -> None:
    """029-AC5: `@everyone` → mentionType everyone, MRI = thread id, label Everyone; 1:1 or Notes refuse."""
    html_, props = _resolved("@Everyone look")
    assert html_ == f"{_span(0, 'Everyone')} look"
    assert [(e["mentionType"], e["mri"]) for e in _mentions(props)] == [("everyone", GROUP)]
    for thread in (ONE_TO_ONE, "48:notes"):
        with pytest.raises(ValueError, match="^@everyone is not available in this chat$"):
            compose.resolve_mentions("@everyone look", ROSTER, thread, ME)
    assert _resolved("@Ann", ROSTER, ONE_TO_ONE)[0] == _span(0, "Ann")


@pytest.mark.parametrize(
    "text",
    [
        "no mention",
        "mail a@b.com",
        "`@Ann`",
        "```\n@Ann\n```",
        r"\@Ann",
        "[@Ann](https://x.test)",
        "https://x.test/@Ann",
        "meet @ 5pm",
    ],
)
def test_emails_code_links_and_escapes_hold_no_candidate(text: str) -> None:
    """029-AC6: no `@` starting a word outside code, link labels and URLs: nothing to resolve."""
    assert not compose.holds_mention(text)
    assert compose.resolve_mentions(text, [], GROUP, ME) == []


@pytest.mark.parametrize("text", ["@Ann", "hi *@Ann*", "(@Ann)", "- @Ann", "> x @Ann"])
def test_an_at_starting_a_word_is_a_candidate(text: str) -> None:
    """029-AC6: inside markup or blocks an `@Name` still counts."""
    assert compose.holds_mention(text)
    assert compose.resolve_mentions(text, ROSTER, GROUP, ME)[0]["mri"] == "8:orgid:ann"


def test_rendering_matches_the_chat_page_composer() -> None:
    """029-AC11: bold, a shortcode, a bare URL and a list render as the composer's full mode."""
    html_, props = _resolved("**bold** :fire: https://x.test\n- item")
    assert html_ == f"<b>bold</b> 🔥 {_a('https://x.test')}<ul><li>item</li></ul>" and props is None


@pytest.mark.parametrize(
    ("text", "picks"),
    [
        ("@Ann " * 51, [{"name": "Ann", "mri": "8:orgid:ann"}]),
        ("@Ann @Bob " * 26, [{"name": "Ann", "mri": "8:orgid:ann"}, {"name": "Bob", "mri": "8:orgid:bob"}]),
    ],
)
def test_more_than_max_mentions_occurrences_refuse(text: str, picks: list[dict[str, Any]]) -> None:
    """029 refusals: the cap counts the mentions written, not the picks: one name typed 51 times too."""
    with pytest.raises(ValueError, match="too many mentions"):
        compose_html(text, picks, {})
    assert len(_mentions(compose_html("@Ann " * 50, picks[:1], {})[1])) == 50


@pytest.mark.parametrize("text", ["@ann " * 51, "@Ann Lee @Bob " * 26])
def test_the_resolver_refuses_more_than_max_mentions(text: str) -> None:
    """029 refusals: more than MAX_MENTIONS `@Name` refuse before any matching."""
    with pytest.raises(ValueError, match="too many mentions"):
        compose.resolve_mentions(text, ROSTER, GROUP, ME)


@pytest.mark.parametrize(
    "blank",
    ["\t", "\u2028", "\u2029", "\u00a0", "\x00", "\u200b", "\u3000", "\ue000", "\u0378"],
    ids=lambda c: f"U+{ord(c):04X}",
)
def test_a_member_without_a_name_is_skipped(blank: str) -> None:
    """029-AC1: a nameless roster entry neither breaks the match nor matches."""
    roster = [{"mri": "8:orgid:anon", "name": blank}, *ROSTER]
    assert _resolved("@ann hi", roster)[0] == f"{_span(0, 'Ann')} hi"


@pytest.mark.parametrize(
    ("text", "label", "mri"),
    [("@ann and @Ann", "Ann", "8:orgid:ann"), ("@BOB and @bob", "Bob", "8:orgid:bob")],
)
def test_two_spellings_of_one_name_give_two_entries(text: str, label: str, mri: str) -> None:
    """029-AC2: `@ann` and `@Ann` are two picks of Ann, numbered in text order."""
    html_, props = _resolved(text)
    assert html_ == f"{_span(0, label)} and {_span(1, label)}"
    assert [(e["itemid"], e["mri"]) for e in _mentions(props)] == [(0, mri), (1, mri)]


@pytest.mark.parametrize(
    ("text", "name", "label"),
    [
        ("@anna strauss hi", "Anna Strauß", "Anna Strauß"),
        ("@anna strauß hi", "Anna Strauss", "Anna Strauss"),
        ("@Zoe\u0301 hi", "Zoé Roe", "Zoé"),
        ("@ZOE\u0301 ROE hi", "Zoé Roe", "Zoé Roe"),
    ],
)
def test_folding_that_changes_length_still_matches(text: str, name: str, label: str) -> None:
    """029-AC2: `ß`/`ss` and a decomposed accent match the roster spelling, the accent kept in the span."""
    roster = [*ROSTER, {"mri": "8:orgid:x", "name": name}]
    assert _resolved(text, roster)[0] == f"{_span(0, label)} hi"


@pytest.mark.parametrize("word", ["Ze\u0301lie", "Noe\u0308l", "An\u0301na", "Ann\u0301a", "Bob\u0308by"])
def test_an_unknown_decomposed_name_is_named_whole(word: str) -> None:
    """029-AC4: the refusal names the word typed, combining accent included; a mark after `Ann` does not end
    the word, so `@Anńa` is not Ann."""
    with pytest.raises(ValueError, match=f"^{re.escape(f'unknown mention @{word} (\\@ for a literal @)')}$"):
        compose.resolve_mentions(f"@{word} hi", ROSTER, GROUP, ME)


@pytest.mark.parametrize("mark", ["́", "̈"])
def test_matching_a_name_against_combining_marks_stays_linear(mark: str) -> None:
    """029: combining marks fold to nothing, so the match must not re-fold what it has already read."""

    def ratio(fn: Callable[[str], object]) -> float:
        small, big = ("A" + mark * n for n in (7_000, 28_000))
        return _best_ms(lambda: fn(big)) / max(_best_ms(lambda: fn(small)), 0.05)

    assert ratio(lambda rest: compose._typed_len(rest, "Ann")) < 8
    assert ratio(lambda rest: _refusal(f"@{rest} hi")) < 8


def _refusal(text: str) -> object:
    try:
        return compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME)
    except ValueError as exc:
        return exc


@pytest.mark.parametrize("unit", ["@a ", "(@a)"])
def test_candidates_keep_bounded_text(unit: str) -> None:
    """029: each `@` keeps at most a name's length of the text after it: memory stays linear."""
    import tracemalloc

    def peak(n: int) -> int:
        tracemalloc.start()
        compose.holds_mention(unit * (n // len(unit)))
        _refusal(unit * (n // len(unit)))
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return peak

    assert peak(28_000) / peak(7_000) < 8


@pytest.mark.parametrize("word", ["a" * 1000, "Ann" + "x" * 5000])
def test_a_refusal_names_at_most_max_name_characters(word: str) -> None:
    """029-AC4: the refusal echoes the word typed, cut at MAX_NAME."""
    with pytest.raises(
        ValueError,
        match=f"^{re.escape(f'unknown mention @{word[: compose.MAX_NAME]} (\\@ for a literal @)')}$",
    ):
        compose.resolve_mentions(f"@{word} hi", ROSTER, GROUP, ME)


@pytest.mark.parametrize(
    ("text", "roster", "mris"),
    [
        (
            "@Ann Lee Ma hi, @Ann Lee Máx",
            [{"mri": "a", "name": "Ann Lee"}, {"mri": "b", "name": "Ann Lee Ma"}],
            ["b", "a"],
        ),
        (
            "@ann lee ma\u0308x @ann lee ma",
            [{"mri": "a", "name": "Ann Lee"}, {"mri": "b", "name": "Ann Lee Ma"}],
            ["a", "b"],
        ),
        (
            "@Ann Lee Maः @Ann Lee Ma",
            [{"mri": "a", "name": "Ann Lee"}, {"mri": "b", "name": "Ann Lee Ma"}],
            ["a", "b"],
        ),
    ],
    ids=["acute", "diaeresis", "spacing-mark"],
)
def test_compose_ends_a_name_where_the_resolver_does(
    text: str, roster: list[dict[str, str]], mris: list[str]
) -> None:
    """029-AC2: a mark continues the word for both, so each `@` notifies the member the resolver read."""
    html_, props = _resolved(text, roster)
    assert [e["mri"] for e in _mentions(props)] == mris
    assert html_.count("<span") == len(mris)


def test_the_mark_class_holds_every_mark() -> None:
    """029: `_MARKS` scans planes 0, 1 and 14 only; every Unicode mark must still be in it."""
    import sys

    marks = {chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c))[0] == "M"}
    assert set(compose._MARKS) == marks


def test_custom_emojis_win_their_name() -> None:
    """An org custom emoji keeps its image over a built-in name; the built-in sends otherwise."""
    assert 'alt="party"' in compose_html(":party:", [], {"party": "x"})[0]
    assert 'alt="fire"' in compose_html(":fire:", [], {"fire": "x"})[0]
    assert compose_html(":party: :salute: :smile:", [], {})[0] == "🥳 🫡 🙂"


# --- name forms and free labels (spec 033) --------------------------------------------------------

BOB_LEE = {"mri": "8:orgid:boblee", "name": "Bob Lee"}
SALES = {"mri": "8:orgid:john", "name": "John Doe | Sales"}


@pytest.mark.parametrize(
    ("text", "label"), [("@Lee", "Lee"), ("@lee", "Lee"), ("@ann", "Ann"), ("@Ann Lee", "Ann Lee")]
)
def test_a_first_last_or_full_name_mentions_with_the_roster_spelling(text: str, label: str) -> None:
    """033-AC1: full name, first word or last word, any case, shows as the roster spells that form."""
    html_, props = _resolved(f"{text} hi")
    assert html_ == f"{_span(0, label)} hi"
    assert [(e["mri"], e["displayName"]) for e in _mentions(props)] == [("8:orgid:ann", label)]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("@Lee hi", "ambiguous mention @Lee: Ann Lee, Bob Lee (\\@ for a literal @)"),
        ("hi @lee", "ambiguous mention @lee: Ann Lee, Bob Lee (\\@ for a literal @)"),
        ("@{Lee|x}", "ambiguous mention @{Lee|x}: Ann Lee, Bob Lee (\\@ for a literal @)"),
        ("@Leeds", "unknown mention @Leeds (\\@ for a literal @)"),
    ],
)
def test_a_shared_last_name_is_refused_with_every_full_name(text: str, error: str) -> None:
    """033-AC2: two members named Lee: `@Lee`, in any case or as a free label's Who, names both."""
    with pytest.raises(ValueError, match=f"^{re.escape(error)}$"):
        compose.resolve_mentions(text, [*ROSTER, BOB_LEE], GROUP, ME)


def test_a_free_label_disambiguates_with_the_full_name() -> None:
    """033-AC2: `@{Ann Lee|Lee}` mentions Ann Lee labelled `Lee`, Bob Lee in the chat."""
    html_, props = _resolved("@{Ann Lee|Lee} hi", [*ROSTER, BOB_LEE])
    assert html_ == f"{_span(0, 'Lee')} hi"
    assert [(e["mri"], e["displayName"]) for e in _mentions(props)] == [("8:orgid:ann", "Lee")]


@pytest.mark.parametrize(
    ("text", "mri", "label"),
    [
        ("@{Ann Lee|the boss}, hi", "8:orgid:ann", "the boss"),
        ("@{ann|the boss}, hi", "8:orgid:ann", "the boss"),
        ("@{LEE|the boss}, hi", "8:orgid:ann", "the boss"),
        ("@{John Doe | Sales|the boss}, hi", "8:orgid:john", "the boss"),
        ("@{Sales|the boss}, hi", "8:orgid:john", "the boss"),
        ("@{ann|}, hi", "8:orgid:ann", "Ann Lee"),  # no label: the full name
        ("@{Sales|   }, hi", "8:orgid:john", "John Doe | Sales"),
    ],
)
def test_a_free_label_mentions_its_member_under_the_label(text: str, mri: str, label: str) -> None:
    """033-AC3: the span shows the label, the entry carries the member's MRI and the label as displayName;
    Who is any form of the name, and the last `|` splits, so Who may hold one."""
    html_, props = _resolved(text, [*ROSTER, SALES])
    assert html_ == f"{_span(0, label)}, hi"
    assert [(e["mri"], e["mentionType"], e["displayName"]) for e in _mentions(props)] == [
        (mri, "person", label)
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("@{Ann Lee|a|b} hi", "unknown mention @{Ann Lee|a|b} (\\@ for a literal @)"),
        (
            "@{Ann Lee|the\nboss}",
            "unclosed mention @{Ann Lee|the: write @{Who|label}, "
            f"{compose._LABEL_RULE} (\\@ for a literal @)",
        ),
        (
            "@{Ann Lee|the boss hi",
            "unclosed mention @{Ann Lee|the boss hi: write @{Who|label}, "
            f"{compose._LABEL_RULE} (\\@ for a literal @)",
        ),
        ("@{Ann Lee} hi", "mention @{Ann Lee} has no label: write @{Who|label} (\\@ for a literal @)"),
        ("@{Ann Lee|" + "x" * 201 + "}", "mention label too long (> 200 characters) (\\@ for a literal @)"),
        ("@{Ann Leeds|x}", "unknown mention @{Ann Leeds|x} (\\@ for a literal @)"),
        ("@{An|x}", "unknown mention @{An|x} (\\@ for a literal @)"),
        ("@{|x}", "unknown mention @{|x} (\\@ for a literal @)"),
        ("@{everyone|x}", "unknown mention @{everyone|x} (\\@ for a literal @)"),
        ("@{Me Self|x}", "unknown mention @{Me Self|x} (\\@ for a literal @)"),
    ],
)
def test_a_malformed_free_label_is_refused(text: str, error: str) -> None:
    """033-AC3: an empty label, one holding `|` (Who then names nobody) or a line break (unclosed), one past
    MAX_NAME, or a Who that is not one member's form whole (a start of it, everyone, me) is refused."""
    with pytest.raises(ValueError, match=f"^{re.escape(error)}$"):
        compose.resolve_mentions(text, ROSTER, GROUP, ME)


def test_a_label_of_max_name_characters_is_taken() -> None:
    """033-AC3: MAX_NAME is the label's cap, not one under it; Who and label each have their own."""
    label = "x" * compose.MAX_NAME
    html_, props = _resolved(f"@{{John Doe | Sales|{label}}}", [*ROSTER, SALES])
    assert html_ == _span(0, label) and _mentions(props)[0]["displayName"] == label


@pytest.mark.parametrize("text", [r"\@{Ann Lee|x}", "`@{Ann Lee|x}`", "a@{Ann Lee|x}"])
def test_an_escaped_free_label_stays_literal(text: str) -> None:
    """033-AC3: `\\@`, code or an `@` inside a word keep the brace text as typed: no candidate."""
    assert not compose.holds_mention(text)
    html_, props = _resolved(text)
    assert "<span" not in html_ and "{Ann Lee|x}" in html_ and props is None


@pytest.mark.parametrize("text", ["{a|b} c", "x | y {z}", "- {a|b}\n> |c|"])
def test_braces_and_bars_are_no_markup(text: str) -> None:
    """033-AC3: `{` and `|` mean nothing to compose outside a mention."""
    assert html.unescape(re.sub(r"<[^>]+>", "", compose_html(text, [], {})[0])) == text.replace("\n", "")


def test_a_one_word_name_has_one_form() -> None:
    """033: `Cher` is full, first and last name at once: one form, never ambiguous with itself."""
    assert compose.forms("Cher") == ["Cher"]
    assert compose.forms("Ann Lee") == ["Ann Lee", "Ann", "Lee"]
    assert compose.forms("  ") == []
    html_, props = _resolved("@cher", [*ROSTER, {"mri": "8:orgid:cher", "name": "Cher"}])
    assert html_ == _span(0, "Cher") and len(_mentions(props)) == 1


def test_a_free_label_pick_shows_its_label_without_one() -> None:
    """033-AC8: compose_html shows a `{Who|label}` pick's label when the pick carries none (a kept pick)."""
    picks = [{"name": "{John Doe | Sales|the boss}", "mri": "8:orgid:john"}]
    html_, props = compose_html("@{John Doe | Sales|the boss} hi", picks, {})
    assert html_ == f"{_span(0, 'the boss')} hi" and _mentions(props)[0]["displayName"] == "the boss"


def test_an_at_a_pick_names_is_left_to_it() -> None:
    """033-AC7: an `@` a pick is anchored at is that pick's, so namesakes keep their own recipients; the
    others still resolve on the roster: one pick per `@`, in text order."""
    text = "@Ann, @Moore and @Bob"
    picks = _anchored(text, ("8:orgid:ann", "Ann Lee"), ("8:orgid:moore", "Ann Moore"))
    assert compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=picks) == [
        {"name": "Ann", "mri": "8:orgid:ann", "label": "Ann"},
        {"name": "Moore", "mri": "8:orgid:moore", "label": "Moore"},
        {"name": "Bob", "mri": "8:orgid:bob", "label": "Bob"},
    ]


@pytest.mark.parametrize(("text", "picked"), [("@Ann then @Ann Moore", 0), ("@Ann Moore then @Ann", 1)])
def test_an_unpicked_longer_name_beside_a_pick_is_the_rosters(text: str, picked: int) -> None:
    """033: Ann Lee picked and shortened to `@Ann`, then `@Ann Moore` typed, in either order: the anchored
    `@Ann` stays Ann Lee's, the typed `@Ann Moore` is Ann Moore's."""
    people = [("8:orgid:ann", "Ann Lee") if n == picked else None for n in range(2)]
    resolved = compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=_anchored(text, *people))
    _, props = compose_html(text, resolved, {}, full=True)
    got = {e["displayName"]: e["mri"] for e in _mentions(props)}
    assert got == {"Ann": "8:orgid:ann", "Ann Moore": "8:orgid:moore"}


@pytest.mark.parametrize(("typed", "shown"), [("ann", "Ann"), ("ANN", "Ann"), ("{ann lee|x}", "x")])
def test_a_pick_names_its_at_whatever_the_case(typed: str, shown: str) -> None:
    """033: `@ann` where Ann Lee was picked (or a free label in another case) is the pick's, not ambiguous
    between the namesakes: compose_html, case-sensitive, gets a pick spelled as typed."""
    text = f"@{typed} hi"
    picks = _anchored(text, ("8:orgid:ann", "Ann Lee"))
    resolved = compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=picks)
    assert resolved == [{"name": typed, "mri": "8:orgid:ann", "label": shown}]
    html_, _ = compose_html(text, resolved, {}, full=True)
    assert html_ == f"{_span(0, shown)} hi"


@pytest.mark.parametrize("text", ["@{Ann Lee|@boss} hi", "@{Ann Lee|ask @Bob} hi", "@{Ann Lee|@Nobody} hi"])
def test_an_at_inside_a_free_label_is_the_labels(text: str) -> None:
    """033-AC3: the label is plain text: an `@` in it is neither another mention nor a refusal."""
    html_, props = _resolved(text)
    label = text[text.index("|") + 1 : text.index("}")]
    assert html_ == f"{_span(0, label)} hi"
    assert [(e["mri"], e["displayName"]) for e in _mentions(props)] == [("8:orgid:ann", label)]


@pytest.mark.parametrize(
    "label",
    ["the *boss*", "a * b", "snake_case", "~~x~~", "a~b", "`x`", "a`b", "[x](https://y.test)", "[x]", "a\\b"],
)
def test_a_free_label_is_read_raw_markup_and_all(label: str) -> None:
    """033 user decision: `@{Who|label}` is read before markdown, so `* _ ~ ` [ \\` in a label stay as typed:
    the span shows them, escaped, and the entry carries them."""
    html_, props = _resolved(f"say @{{Ann Lee|{label}}} hi")
    assert html_ == f"say {_span(0, html.escape(label))} hi"
    assert _mentions(props)[0]["displayName"] == label


@pytest.mark.parametrize(("text", "after"), [("@{Ann Lee|a}b} hi", "b} hi"), ("@{Ann Lee|a}x", "x")])
def test_a_free_label_ends_at_its_first_closing_brace(text: str, after: str) -> None:
    """033 user decision: `}` closes the token; what follows is text, glued or not."""
    html_, props = _resolved(text)
    assert html_ == f"{_span(0, 'a')}{html.escape(after)}" and _mentions(props)[0]["displayName"] == "a"


@pytest.mark.parametrize(
    ("who", "mri"), [("Ann *Lee*", None), ("John Doe | Sales", "8:orgid:john"), ("Sales", "8:orgid:john")]
)
def test_who_is_read_raw_too(who: str, mri: str | None) -> None:
    """033 user decision: Who is part of the opaque token: markup in it is not markup (`*Lee*` names no
    one), a `|` in it is kept (the last one splits)."""
    roster = [*ROSTER, {"mri": "8:orgid:john", "name": "John Doe | Sales"}]
    text = f"@{{{who}|boss}} hi"
    if mri is None:
        with pytest.raises(ValueError, match=re.escape(f"unknown mention @{{{who}|boss}}")):
            compose.resolve_mentions(text, roster, GROUP, ME)
    else:
        assert [p["mri"] for p in compose.resolve_mentions(text, roster, GROUP, ME)] == [mri]


@pytest.mark.parametrize(
    ("name", "limit"),
    [("x" * 201, 200), ("{" + "x" * 201 + "|" + "y" * 200 + "}", 403), ("{Ann|" + "y" * 400 + "}", 403)],
)
def test_a_pick_name_past_its_limit_is_refused_with_that_limit(name: str, limit: int) -> None:
    """033 review: a `{Who|label}` name may reach 2 * MAX_NAME + 3, a plain one MAX_NAME; the refusal says
    which."""
    with pytest.raises(ValueError, match=re.escape(f"mention name too long (> {limit} characters)")):
        compose_html(f"@{name}", [{"name": name, "mri": "8:orgid:ann"}], {})


@pytest.mark.parametrize("label", ["@boss", "x @team", "hi @Bob"])
def test_an_at_inside_a_picked_label_is_that_picks(label: str) -> None:
    """033 review: a kept pick whose label holds an `@word` covers it: no new candidate, no refusal, no
    spurious mention, and no roster needed."""
    text = f"hi @{{Gone|{label}}} there"
    picks = _anchored(text, ("8:orgid:gone", f"{{Gone|{label}}}"))
    resolved = compose.resolve_mentions(text, ROSTER, GROUP, ME, picks=picks)
    assert resolved == [{"name": f"{{Gone|{label}}}", "mri": "8:orgid:gone", "label": label}]
    _, props = compose_html(text, resolved, {}, full=True)
    assert [(e["mri"], e["displayName"]) for e in _mentions(props)] == [("8:orgid:gone", label)]


@pytest.mark.parametrize("typed", ["BOSS", "bôss"])
def test_a_free_label_typed_in_another_case_shows_as_typed(typed: str) -> None:
    """033 review: a kept `{Ann Lee|boss}` covers `@{Ann Lee|BOSS}`, but the label shows as typed."""
    text = f"@{{Ann Lee|{typed}}} hi"
    picks = _anchored(text, ("8:orgid:ann", "{Ann Lee|boss}"))
    resolved = compose.resolve_mentions(text, ROSTER, GROUP, ME, picks=picks)
    assert resolved == [{"name": f"{{Ann Lee|{typed}}}", "mri": "8:orgid:ann", "label": typed}]
    html_, props = compose_html(text, resolved, {}, full=True)
    assert html_ == f"{_span(0, typed)} hi" and _mentions(props)[0]["displayName"] == typed


@pytest.mark.parametrize("alias", [{"name": "p0", "label": "P0"}, {"name": "P0"}])
def test_the_mention_cap_counts_distinct_mentions(alias: dict[str, str]) -> None:
    """033 review: a pick spelled another way for the same mention does not count twice; 51 distinct do."""
    picks = [{"name": f"P{n}", "mri": f"8:orgid:{n}", "label": f"P{n}"} for n in range(compose.MAX_MENTIONS)]
    compose_html("@P0 @p0", [*picks, {**alias, "mri": "8:orgid:0"}], {})
    with pytest.raises(ValueError, match=re.escape("too many mentions (51 > 50)")):
        compose_html("@P0", [*picks, {"name": "P50", "mri": "8:orgid:50"}], {})
    with pytest.raises(ValueError, match=re.escape("too many mentions (51 > 50)")):
        compose_html("@P0", [*picks, {"name": "P0", "mri": "8:orgid:other"}], {})


@pytest.mark.parametrize("blank", ["\t", " ", " ", " ", "\x00", "​", "　"], ids=lambda c: f"U+{ord(c):04X}")
def test_a_free_label_holding_a_blank_but_the_space_or_a_control_is_refused(blank: str) -> None:
    """033 review: the label rule (plain_label): tab, line/paragraph separators, other blanks, control,
    format, private-use (U+E000) and unassigned (U+0378) characters are refused."""
    with pytest.raises(ValueError, match=re.escape(f"{compose._LABEL_RULE} (\\@ for a literal @)")):
        compose.resolve_mentions(f"@{{Ann Lee|a{blank}b}} hi", ROSTER, GROUP, ME)


CROWD = [*ROSTER, *({"mri": f"8:orgid:{n:04x}", "name": f"Person{n} Name{n}"} for n in range(250))]


@pytest.mark.parametrize("unit", ["@", "@ ", "@!", "@@Ann "])
def test_a_flood_of_at_signs_stays_linear(unit: str) -> None:
    """033 review: an `@` no pick and no name can start costs no roster scan, and names read stop at
    MAX_MENTIONS: a 28 KB flood resolves (or refuses) in time linear in its size."""
    # The composer's cap of picks, anchored at the first `@`s, none of whose names follows them.
    people = [(f"8:orgid:p{n}", f"Pick{n} Name") for n in range(50)]

    def run(n: int) -> object:
        text = unit * (n // len(unit))
        try:
            return compose.resolve_mentions(text, CROWD, GROUP, ME, picks=_anchored(text, *people))
        except ValueError as exc:
            return exc

    start = time.process_time()  # one bounded run first: a quadratic one would take seconds
    run(28_000)
    assert time.process_time() - start < 0.25  # ~25 ms here; a scan per `@` costs 0.5 s to 15 s
    assert _best_ms(lambda: run(28_000)) / max(_best_ms(lambda: run(7_000)), 0.05) < 8


@pytest.mark.parametrize(
    ("text", "mris"),
    [
        ("@ann @ann", ["8:orgid:ann", "8:orgid:moore"]),
        ("@ANN @ann", ["8:orgid:ann", "8:orgid:moore"]),
        ("@ann @Ann", ["8:orgid:moore", "8:orgid:ann"]),
        ("@Ann @Ann @Ann", ["8:orgid:ann", "8:orgid:moore", "8:orgid:moore"]),
    ],
)
def test_re_cased_namesake_picks_stay_at_their_own_at(text: str, mris: list[str]) -> None:
    """033: namesakes Ann Lee and Ann Moore, each anchored at its own `@`, whatever its case: each `@ann`
    is the pick's anchored there, never paired by spelling."""
    people = {"8:orgid:ann": "Ann Lee", "8:orgid:moore": "Ann Moore"}
    picks = _anchored(text, *((mri, people[mri]) for mri in mris))
    resolved = compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=picks)
    _, props = compose_html(text, resolved, {}, full=True)
    assert [e["mri"] for e in _mentions(props)] == mris


@pytest.mark.parametrize("kept", [False, True])
def test_sixty_ats_inside_one_label_are_one_mention(kept: bool) -> None:
    """033 review: the cap counts names read, not every `@`: sixty `@x` in a free label, or in a kept pick's
    label as Teams wrote it, are that one mention's."""
    label = ("@x " * 60).strip()
    if kept:
        text = f"hi @{{Gone|{label}}}"
        picks = _anchored(text, ("8:orgid:gone", f"{{Gone|{label}}}"))
        resolved = compose.resolve_mentions(text, ROSTER, GROUP, ME, picks=picks)
        assert resolved == [{"name": f"{{Gone|{label}}}", "mri": "8:orgid:gone", "label": label}]
    else:
        html_, props = _resolved(f"@{{Ann Lee|{label}}} hi")
        assert len(_mentions(props)) == 1 and html_.count("<span") == 1


@pytest.mark.parametrize("text", ["@Ann " * 51, "@Ann " * 50 + "@Bob"])
def test_fifty_one_mentions_read_are_refused(text: str) -> None:
    """033 review: picked or resolved, the 51st name read refuses."""
    with pytest.raises(ValueError, match=re.escape("too many mentions (> 50)")):
        compose.resolve_mentions(text, ROSTER, GROUP, ME, picks=_anchored(text, ("8:orgid:ann", "Ann Lee")))


# --- the resolver reads the text as compose_html does (033 review) ---

BL, BM = "8:orgid:bl", "8:orgid:bm"
NAMESAKES = [
    {"mri": ME, "name": "Me Self"},
    {"mri": "8:orgid:ann", "name": "Ann Lee"},
    {"mri": "8:orgid:moore", "name": "Ann Moore"},
    {"mri": BL, "name": "Bob Lee"},
    {"mri": BM, "name": "Bob Moore"},
]


def _pick_at(at: int, mri: str, name: str) -> dict[str, Any]:
    """A page's pick anchored at `text[at]` (spec 033), as chat_verbs hands it over: read by the forms of
    `name`, or, a braced `{Who|label}` (an edit's kept mention, `kept` as given back), by Who and label."""
    if name.startswith("{"):
        _, who, label = compose.free_label(name)
        return {"at": at, "mri": mri, "forms": [], "whos": [who, label], "kept": name}
    return {"at": at, "mri": mri, "forms": compose.forms(name)}


def _anchored(text: str, *people: tuple[str, str] | None) -> list[dict[str, Any]]:
    """Picks anchored at the `@`s of `text` in order, one (mri, name) each; None leaves an `@` unpicked."""
    ats = [i for i, c in enumerate(text) if c == "@"]
    return [_pick_at(at, *person) for at, person in zip(ats, people, strict=False) if person]


def _entries(
    text: str, picks: list[dict[str, Any]], full: bool = True, roster: list[dict[str, Any]] = NAMESAKES
) -> tuple[list[Any], list[Any]]:
    """(the resolver's mentions, compose_html's from them), as (mri, label) in text order."""
    resolved = compose.resolve_mentions(text, roster, GROUP, ME, picks=picks, full=full)
    want = [(p["mri"], "Everyone" if p.get("everyone") else p.get("label") or p["name"]) for p in resolved]
    _, props = compose_html(text, resolved, {}, full=full)
    return want, [(e["mri"], e["displayName"]) for e in _mentions(props)] if props else []


@pytest.mark.parametrize(
    "text", ["@{Ann Lee|http://a.b}@Bob @Bob @Bob", "@{Ann Lee|a http://x.y/}@Bob Lee", "@{Ann Lee|x}@Bob"]
)
def test_a_url_inside_a_read_label_does_not_hide_what_follows(text: str) -> None:
    """033 review: past a name read, the resolver resumes where compose_html does: a URL starting inside the
    label cannot swallow the `@` after it, for one and not the other."""
    picks = _anchored(text, None, (BL, "Bob Lee"), (BM, "Bob Moore"), (BM, "Bob Moore"))
    want, got = _entries(text, picks)
    assert want == got and len(got) == text.count("@")


# Markup that splits or hides text, escapes, emojis, URLs, braces, namesakes and re-cased names.
_PIECES = ["@", "@", "{", "}", "|", "http://a.b", " ", " ", "Ann", "ann", "ANN", "Lee", "Moore", "Bob", "x"]
_PIECES += ["`", "```", "\n", "\n- ", "\n> ", "- ", "> ", "\\@", ":x:"]
_PIECES += [
    "*",
    "_",
    "~~",
    "[x](http://a.b)",
    "{Ann Lee|a*b}",
    "{Ann Lee|_x_}",
    "{Ann *Lee*|y}",
]  # markup in labels
# Runs `_inline` refuses to read as a token (`_a_@{…}y`, `\_@{…}é`): the `@{…}` still ends at its `}`.
_PIECES += ["\\_", "_a_", "}y", "\\\\", "a_", "__", "é", "https://x.io/@{Ann Lee|x}"]
# Whole names read longer than a pick anchored there (spec 033): the pick stands down, in both readers.
_PIECES += ["@Ann Moore", "@Ann Lee", "@Bob Moore", "@{Ann Moore|m}", "@Ann Lee Smith", "@Lee Smith"]


# Members whose names read longer than a pick of Ann Lee at the same `@`: by a third word, a last-name join.
LONGER = [
    *NAMESAKES,
    {"mri": "8:orgid:als", "name": "Ann Lee Smith"},
    {"mri": "8:orgid:ls", "name": "Lee Smith"},
]


def test_resolver_and_compose_agree_on_random_texts() -> None:
    """033 review: on random texts, in full and inline mode (the widget's quick reply), with picks anchored
    at random `@`s (namesakes, kept braces, one `@` twice), compose_html sends exactly the mentions the
    resolver read, in order, whenever the resolver does not refuse."""
    import random

    rng = random.Random(32)
    pool = [
        ("8:orgid:ann", "Ann Lee"),
        ("8:orgid:moore", "Ann Moore"),
        (BL, "Bob Lee"),
        (BM, "Bob Moore"),
        ("8:orgid:ann", "{Ann Lee|x}"),
        ("8:orgid:ann", "{Ann Lee|a*b}"),
    ]
    checked = 0
    start = time.process_time()
    for _ in range(20000):
        text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, 14)))
        ats = [i for i, c in enumerate(text) if c == "@"]
        picks = [_pick_at(at, *rng.choice(pool)) for at in ats if rng.random() < 0.6]
        if picks and rng.random() < 0.1:
            picks.append({**picks[0], "mri": BM})  # two picks on one `@`: neither stands
        full = rng.random() < 0.5
        try:
            want, got = _entries(text, picks, full, LONGER)
        except ValueError:
            continue
        assert want == got, (text, picks, full)
        checked += 1
    assert checked > 5000 and time.process_time() - start < 20


_ANN_X = [("8:orgid:ann", "x")]
_GONE = ("8:orgid:gone", "{Gone|q}")  # an edit's kept mention of someone who left


@pytest.mark.parametrize(
    ("text", "picks", "roster", "want"),
    [
        ("_a_@{Ann Lee|x}y", [], NAMESAKES, _ANN_X),
        ("\\_@{Ann Lee|x}y", [], NAMESAKES, _ANN_X),
        ("\\_@{Ann Lee|x}é", [], NAMESAKES, _ANN_X),
        ("x @{Ann Lee|x}_y", [], NAMESAKES, _ANN_X),
        # A kept pick of someone gone, read off the picks alone, whether the roster is up or not.
        ("hi _a_@{Gone|q}y", [_GONE], NAMESAKES, [("8:orgid:gone", "q")]),
        ("hi _a_@{Gone|q}y", [_GONE], [], [("8:orgid:gone", "q")]),
        ("hi \\_@{Gone|q}é", [_GONE], NAMESAKES, [("8:orgid:gone", "q")]),
    ],
)
@pytest.mark.parametrize("full", [True, False])
def test_a_braced_mention_ends_at_its_brace_whatever_follows(
    text: str, picks: list[tuple[str, str]], roster: list[dict[str, Any]], want: list[Any], full: bool
) -> None:
    """033 review: a `@{…}` run `_inline` does not take as a token still ends at its `}`, in the resolver
    (a free label or a kept pick) and in compose: the mention picked is sent, not left as raw text."""
    assert _entries(text, _anchored(text, *picks), full, roster) == (want, want)


_MARKERS_033 = ["*", "_", "~~", "**"]


def test_a_label_holding_the_marker_of_the_span_around_it_never_refuses() -> None:
    """033 review: random spans around a `@{Ann Lee|label}` whose label holds that span's own marker (and
    others): never refused, and compose sends exactly the mention the resolver read. Counted, so a refusal
    cannot hide among the skipped cases."""
    import random

    rng = random.Random(23)
    # What sits between the opening marker and the `@` (nothing: `_@{`, `*@{`), and after the `}`. No blank
    # against a marker: a space-padded marker opens or closes no span.
    left, right = ["", "a ", "x y ", "\\@{x} ", "\\@Ann "], ["", " b", " z w", "."]
    refused: list[str] = []
    for _ in range(3000):
        mark, other = rng.choice(_MARKERS_033), rng.choice(_MARKERS_033)
        label = (
            "".join(rng.choice(["a", "b", " ", mark, other]) for _ in range(rng.randint(1, 6))).strip() or "a"
        )
        inner = f"{rng.choice(left)}@{{Ann Lee|{label}}}{rng.choice(right)}"
        text = f"{rng.choice(['', 'go ', 'x: '])}{mark}{inner}{mark}{rng.choice(['', ' ok', '.'])}"
        try:
            want, got = _entries(text, [], rng.random() < 0.5)
        except ValueError as exc:
            refused.append(f"{text!r}: {exc}")
            continue
        assert want == got == [("8:orgid:ann", label)], text
    assert refused == []


@pytest.mark.parametrize("unit", ["-@", ".@", "@x"])
def test_a_flood_against_long_pick_names_stays_bounded(unit: str) -> None:
    """033 review: 50 picks of 200-character names anchored in a 28 KB `@` flood: each anchored `@` reads
    only its own pick's forms, the rest never meet them, so the text resolves (or refuses) fast."""
    text = "@" + unit * 14_000
    names = [unit * 99 + f"{unit[0]}{n}" for n in range(50)]
    picks = _anchored(text, *((f"8:orgid:{n}", name) for n, name in enumerate(names)))
    start = time.process_time()
    with contextlib.suppress(ValueError):
        compose.resolve_mentions(text, ROSTER, GROUP, ME, picks=picks)
    assert time.process_time() - start < 0.5


@pytest.mark.parametrize("letters", ["abcdefghijklmnopqrstuvwxyz", "ÀÉÎÕÜàéîõüŁłŻżÇç", "A"])
def test_a_large_roster_is_scanned_by_first_letter(letters: str) -> None:
    """033 review: 5000 members and 50 mentions resolve in well under a second: an `@` only tries the forms
    sharing its first word (folded: `É` and `e` are one), so 5000 names all starting with `A` cost no more."""
    roster = [{"mri": ME, "name": "Me Self"}]
    roster += [
        {
            "mri": f"8:orgid:{n}",
            "name": f"{letters[n % len(letters)]}{n} {letters[n // 7 % len(letters)]}x{n}",
        }
        for n in range(5000)
    ]
    text = " ".join(f"@{m['name']}" for m in roster[1:51])
    start = time.process_time()
    resolved = compose.resolve_mentions(text, roster, GROUP, ME)
    took = time.process_time() - start
    assert len(resolved) == 50 and took < 0.4, took


@pytest.mark.parametrize(
    ("name", "found"),
    [
        ("Doe, John", ["Doe, John", "Doe", "John"]),
        ("Ann Lee (Sales)", ["Ann Lee (Sales)", "Ann", "Sales"]),
        ("“Ann” Lee!", ["“Ann” Lee!", "Ann", "Lee"]),
        ("(x)", ["(x)", "x"]),
        ("--", ["--"]),
    ],
)
def test_first_and_last_names_drop_the_punctuation_around_them(name: str, found: list[str]) -> None:
    """033 review: `Doe, John` is `@Doe`, `Ann Lee (Sales)` is `@Sales`; nothing left is no form."""
    assert compose.forms(name) == found


@pytest.mark.parametrize(
    ("text", "label", "mri"),
    [
        ("@Doe thanks", "Doe", "8:orgid:doe"),
        ("@Doe, thanks", "Doe", "8:orgid:doe"),
        ("@sales hi", "Sales", "8:orgid:sales"),
    ],
)
def test_a_name_with_punctuation_is_reached_by_its_words(text: str, label: str, mri: str) -> None:
    """033 review: the roster's punctuation is no part of a first or last name."""
    roster = [
        *ROSTER,
        {"mri": "8:orgid:doe", "name": "Doe, John"},
        {"mri": "8:orgid:sales", "name": "Bo Roy (Sales)"},
    ]
    assert compose.resolve_mentions(text, roster, GROUP, ME) == [
        {"name": text[1 : 1 + len(label)], "mri": mri, "label": label}
    ]


@pytest.mark.parametrize("kept", ["{Ann Lee|Ann Lee}", "{Gone|boss}"])
def test_no_member_name_can_take_a_kept_brace(kept: str) -> None:
    """033 review: a member whose display name starts like a kept brace (`{Ann Lee|Ann Lee} and`) is never
    read at a `{`: a free label is not a name, so the departed person's kept mention stays theirs."""
    hostile = [*ROSTER, {"mri": "8:orgid:hostile", "name": f"{kept} and"}]
    text = f"hi @{kept} and more"
    sent = compose.resolve_mentions(text, hostile, GROUP, ME, picks=_anchored(text, ("8:orgid:gone", kept)))
    assert [(p["mri"], p["name"]) for p in sent] == [("8:orgid:gone", kept)]


@pytest.mark.parametrize("size", [65, 10_000])
def test_long_keys_are_folded_without_being_cached(size: int) -> None:
    """033 review: the fold cache keeps short keys only: a long name a page sends must not pin its memory."""
    compose._fold_cached.cache_clear()
    key = "x" * size
    assert compose._fold(key) == key
    assert compose._fold_cached.cache_info().currsize == 0
    compose._fold("short")
    assert compose._fold_cached.cache_info().currsize == 1


@pytest.mark.parametrize("token", ["{Ann Lee|Ann Lee}", "{Ann Lee|boss}"])
def test_a_member_named_like_a_free_label_never_takes_it(token: str) -> None:
    """033 review: unpicked `@{Ann Lee|boss}` with a member whose display name is exactly `{Ann Lee|boss}`:
    a `{` opens a free label, never a roster name, so Who (Ann Lee) is who it names."""
    hostile = [*ROSTER, {"mri": "8:orgid:hostile", "name": token}]
    sent = compose.resolve_mentions(f"hi @{token} there", hostile, GROUP, ME)
    assert [p["mri"] for p in sent] == ["8:orgid:ann"]


@pytest.mark.parametrize(
    ("text", "html_"),
    [
        ("*see @{Ann Lee|a* b}*", "<i>see {m}</i>"),
        ("_a @{Ann Lee|b_} c_", "<i>a {m} c</i>"),
        ("~~@{Ann Lee|x~~}~~", "<s>{m}</s>"),
        ("**@{Ann Lee|b**}**", "<b>{m}</b>"),
        ("*a @{Ann Lee|b*}*", "<i>a {m}</i>"),
        ("~~x @{Ann Lee|~~} y~~", "<s>x {m} y</s>"),
        ("*@{Ann Lee|*}*", "<i>{m}</i>"),
        ("**go @{Ann Lee|a * b**}** now", "<b>go {m}</b> now"),
        ("_@{Ann Lee|a_ b}_", "<i>{m}</i>"),
        ("*@{Ann Lee|a* b} c*", "<i>{m} c</i>"),
        ("*a _@{Ann Lee|b*}_ c*", "<i>a <i>{m}</i> c</i>"),
        ("~~x _@{Ann Lee|y~~ z}_~~", "<s>x <i>{m}</i></s>"),
        ("**go _@{Ann Lee|a**}_ now**", "<b>go <i>{m}</i> now</b>"),
    ],
)
def test_a_span_closes_past_a_marker_inside_a_free_label(text: str, html_: str) -> None:
    """033 review: an emphasis or strike opened before `@{…}` does not close on the same marker inside the
    label: the span closes after it and the label keeps its marker."""
    label = text[text.index("|") + 1 : text.index("}")]
    got, props = _resolved(text)
    assert got == html_.format(m=_span(0, html.escape(label)))
    assert _mentions(props)[0]["displayName"] == label


@pytest.mark.parametrize(
    ("text", "html_"),
    [
        ("*a `@{` b* c}", "<i>a <code>@{</code> b</i> c}"),
        ("_a `@{x` b_ y}", "<i>a <code>@{x</code> b</i> y}"),
        ("*a_@{p* b} c*", "<i>a_@{p</i> b} c*"),
        ("*x a_@{p* b} c*", "<i>x a_@{p</i> b} c*"),
        ("*a https://x.io/@{p* b} c", '<i>a <a href="https://x.io/@{p">https://x.io/@{p</a></i> b} c'),
    ],
)
def test_a_brace_the_content_reads_as_text_is_no_token_for_the_span_around_it(text: str, html_: str) -> None:
    """033 review: a `@{` inside code, a URL or a word (an intra-word `_` opens nothing) is no token, so
    the span closes where it did before 033."""
    assert _resolved(text) == (html_, None)


@pytest.mark.parametrize(
    ("text", "at"),
    [
        ("`@Ann` and @Ann", 1),  # in code
        ("\\@Ann and @Ann", 1),  # escaped
        ("[@Ann](https://x.io) and @Ann", 1),  # a link label
        ("https://x.io/@Ann and @Ann", 12),  # inside a URL
        ("x@Ann and @Ann", 1),  # no word start
        ("@Ann and @Ann", 2),  # not an `@` at all
        ("@{Ann Lee|@Ann} and @Ann", 10),  # inside a label read whole
    ],
)
def test_a_pick_at_no_mention_start_anchors_nothing(text: str, at: int) -> None:
    """033 (C1): a pick whose `@` the reader never reads is dropped, never moved to the next `@`: Ann Moore
    anchored there leaves the real `@Ann` to the roster (ambiguous here: refused, not hers)."""
    picks = [{"at": at, "mri": "8:orgid:moore", "forms": compose.forms("Ann Moore")}]
    with pytest.raises(ValueError, match="ambiguous mention @Ann:"):
        compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=picks)


@pytest.mark.parametrize("second", ["8:orgid:moore", "8:orgid:ann"])
def test_two_picks_on_one_at_anchor_nothing(second: str) -> None:
    """033: two picks sent for one `@` (a forged or stale page): neither stands, the roster decides."""
    picks = [
        {"at": 0, "mri": "8:orgid:ann", "forms": compose.forms("Ann Lee")},
        {"at": 0, "mri": second, "forms": compose.forms("Ann Moore" if "moore" in second else "Ann Lee")},
    ]
    with pytest.raises(ValueError, match="ambiguous mention @Ann"):
        compose.resolve_mentions("@Ann hi", [*ROSTER, MOORE], GROUP, ME, picks=picks)
    assert (
        compose.resolve_mentions("@Ann Lee", [*ROSTER, MOORE], GROUP, ME, picks=picks)[0]["mri"]
        == "8:orgid:ann"
    )


@pytest.mark.parametrize("text", ["@\u0301Ann and @Ann", "@Ann and @\u0301Ann", "@\u0301Ann and @\u0301Ann"])
def test_a_mark_after_an_anchored_at_reads_as_compose_html_does(text: str) -> None:
    """033 (C1 repro): a combining mark right after an `@`: the anchored pick reads there or not as the
    resolver and compose_html agree, and the other `@Ann` keeps its own pick."""
    ats = [i for i, c in enumerate(text) if c == "@"]
    picks = [
        {"at": ats[0], "mri": "8:orgid:ann", "forms": compose.forms("Ann Lee")},
        {"at": ats[1], "mri": "8:orgid:moore", "forms": compose.forms("Ann Moore")},
    ]
    resolved = compose.resolve_mentions(text, [*ROSTER, MOORE], GROUP, ME, picks=picks)
    _, props = compose_html(text, resolved, {}, full=True)
    assert (
        [p["mri"] for p in resolved]
        == [e["mri"] for e in _mentions(props)]
        == ["8:orgid:ann", "8:orgid:moore"]
    )


_ROÉ = {"mri": "8:orgid:roe", "name": "Bob Roé"}
_H1 = [*LONGER, _ROÉ, {"mri": "8:orgid:twin", "name": "Ann Lee"}]  # Ann Lee's twin: same full name


@pytest.mark.parametrize(
    ("text", "pick", "want"),
    [
        ("@Ann Moore hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:moore"),  # `Lee` retyped `Moore`
        ("@Ann Lee Smith hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:als"),  # a third word
        ("@Lee Smith hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:ls"),  # her last name, joined
        ("@Ann Mooré hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:moore"),  # with a combining mark
        ("@{Ann Moore|x} hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:moore"),  # a brace Who naming another
        (
            "@{Bob Roé|Bob} hi",
            ("8:orgid:ann", "{Ann Lee|Bob Roé}"),
            "8:orgid:roe",
        ),  # a kept brace's Who edited
        ("@Ann hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:ann"),  # namesakes reading as far: the pick decides
        ("@Ann hi", ("8:orgid:moore", "Ann Moore"), "8:orgid:moore"),
        ("@Ann Lee hi", ("8:orgid:ann", "Ann Lee"), "8:orgid:ann"),
        ("@{Ann|x} hi", ("8:orgid:moore", "Ann Moore"), "8:orgid:moore"),  # Who names both: the pick decides
        (
            "@{Ann Lee|x} hi",
            ("8:orgid:twin", "Ann Lee"),
            "8:orgid:twin",
        ),  # twins' full name: the pick decides
    ],
)
def test_a_pick_stands_down_where_another_member_reads_longer(
    text: str, pick: tuple[str, str], want: str
) -> None:
    """033 review H1: an anchored `@` is its pick's while what follows is one of the pick's forms and no other
    member reads longer there (at a brace: Who names no other member whole); else it reads as unpicked."""
    picks = _anchored(text, pick)
    if pick[1].startswith("{"):
        picks[0]["forms"] = compose.forms("Ann Lee")
    resolved = compose.resolve_mentions(text, _H1, GROUP, ME, picks=picks)
    _, props = compose_html(text, resolved, {}, full=True)
    assert [p["mri"] for p in resolved] == [e["mri"] for e in _mentions(props)] == [want]


@pytest.mark.parametrize(
    ("text", "kept"), [("@{Bob Roé|Bob Roé} hi", "{Bob Roé|Bob Roé}"), ("@{Ann Moore|x} hi", "{Ann Moore|x}")]
)
def test_a_kept_brace_given_back_unchanged_stays_the_messages_own(text: str, kept: str) -> None:
    """033 review H1: a departed person's kept `{label|label}` whose label is a member's full name, or a kept
    brace naming another member, saved unchanged: the message's own person, not the member it names."""
    picks = [_pick_at(0, "8:orgid:gone", kept)]
    assert [p["mri"] for p in compose.resolve_mentions(text, _H1, GROUP, ME, picks=picks)] == ["8:orgid:gone"]
    edited = text.replace("|", "| ", 1)  # the same Who, the brace edited: no longer the copy's own
    assert [p["mri"] for p in compose.resolve_mentions(edited, _H1, GROUP, ME, picks=picks)] != [
        "8:orgid:gone"
    ]


@pytest.mark.parametrize(
    ("text", "ats"),
    [
        ("hi @{x @{Bob Smith|Lee}", [3]),  # a valid brace is read whole: the `@` inside is its Who's
        ("@{a @b|c} @d", [0, 10]),
        ("@{x @y and @z", [0, 4, 11]),  # unclosed: no brace to skip, resolve_mentions refuses it
    ],
)
def test_mention_ats_reads_a_brace_whole_as_resolve_mentions(text: str, ats: list[int]) -> None:
    """033 review r7 L1: the `@`s anchors may sit on are those resolve_mentions reads, an `@` inside a
    `{Who|label}` not among them."""
    assert compose.mention_ats(text) == ats
