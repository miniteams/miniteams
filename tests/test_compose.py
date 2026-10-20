"""Composer text → Teams HTML: markdown subset, emojis, mentions (spec 012)."""

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
        ("@Ann hi", [*ROSTER, MOORE], "ambiguous mention @Ann: Ann Lee, Ann Moore"),
        ("hi @ann", [*ROSTER, MOORE], "ambiguous mention @ann: Ann Lee, Ann Moore"),
        (
            "@Ann Lee",
            [*ROSTER, {"mri": "8:orgid:lee2", "name": "Ann Lee"}],
            "ambiguous mention @Ann Lee: Ann Lee, Ann Lee",
        ),
        ("@Nobody here", ROSTER, "unknown mention @Nobody"),
        ("@Me Self", ROSTER, "unknown mention @Me"),
        ("@Bot", ROSTER, "unknown mention @Bot"),
        ("@Annabel", ROSTER, "unknown mention @Annabel"),
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


@pytest.mark.parametrize("blank", ["", "   "])
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
    with pytest.raises(ValueError, match=f"^unknown mention @{word}$"):
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
    with pytest.raises(ValueError, match=f"^unknown mention @{word[: compose.MAX_NAME]}$"):
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
