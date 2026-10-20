"""Teams message HTML → composer markdown (spec 019 phase 1): round trips, escapes, refusals."""

import html
import json
import random
import re
from typing import Any

import pytest
from test_compose import _best_ms

from miniteams import decompose as D
from miniteams.compose import compose_html, reply_quote
from miniteams.decompose import Editable, decompose
from miniteams.images import Image, images_html
from miniteams.send import MAX_HTML_BYTES

OID = "0-weu-d1-feedc0ffee"
EMOJIS = {"fiesta": OID, "fire": "0-weu-d1-shadowed"}
ANN = {"name": "Ann Lee", "mri": "8:orgid:ann"}
EVERYONE = {"name": "everyone", "mri": "19:grp@thread.v2", "everyone": True}
MENTION = "http://schema.skype.com/Mention"
AMS = "https://eu-api.asm.skype.com/v1/objects/0-weu-d1-aaaa/views/imgo"


def _msg(content: str, msgtype: str = "RichText/Html", **props: Any) -> dict[str, Any]:
    return {"messagetype": msgtype, "content": content, "properties": props}


def _form(content: str, **props: Any) -> Editable:
    form = decompose(_msg(content, **props), EMOJIS)
    assert isinstance(form, Editable), form
    return form


def _round_trip(text: str, picks: list[dict[str, Any]] | None = None) -> Editable:
    """The core invariant: what compose wrote, decompose gives back as text that composes the same."""
    composed = compose_html(text, picks or [], EMOJIS, full=True)
    form = _form(composed[0], **(composed[1] or {}))
    assert compose_html(form.text, form.picks, EMOJIS, full=True) == composed
    return form


def _span(n: int, label: str) -> str:
    return f'<span itemtype="{MENTION}" itemscope itemid="{n}">{label}</span>'


def _mentions(*entries: tuple[str, str]) -> str:
    return json.dumps(
        [{"itemid": n, "mri": mri, "mentionType": kind} for n, (mri, kind) in enumerate(entries)]
    )


# --- round trips: compose → decompose → compose (019-AC4 server half) ---


@pytest.mark.parametrize(
    "text",
    [
        "plain words",
        "**bold**",
        "_italic_",
        "*italic*",
        "~~strike~~",
        "`code <b> & *x*`",
        "[label](https://x.test/a?b=1&c=2)",
        "[wiki](https://w.test/Foo_(bar))",
        "see https://x.test/_a_/*b* end",
        "🔥 and :fire: and 😀",
        "line one\nline two\n\nafter a blank\n",
        "```\ncode <b>\n  indented :fire: **x**\n```\nafter",
        "```\n```",
        "> quoted\n> **bold** quote\n>\nthen text",
        "- a\n- **b**\n* c",
        "1. one\n2. two\n10. ten",
        "- a\n\n- split list",
        "> q1\n\n> q2",
        "- a\n1. b\n> c\n```\nd\n```\ntext",
        "**bold _it_ ~~gone~~** and _x **y**_",
        "**[link](https://x.test)** `c`",
    ],
)
def test_every_element_round_trips(text: str) -> None:
    """019-AC4: bold, italic, strike, code, links, bare URLs, emojis, line breaks, fences, quotes, lists."""
    _round_trip(text)


@pytest.mark.parametrize(
    "text",
    [
        "2*3*4 snake_case_name [draft] a * b a _ b x~y",
        "C:\\path\\to and a \\- b",
        "*",
        "_a_b c",
        "**open and `open and ~~open",
        "- not a list\n* nor this\n> nor a quote\n>\n1. nor a number\n```nor a fence",
        "\\- a literal backslash before a dash",
        "[x](https://not.a/link) [y] (z)",
        "literal \\*stars\\* and \\\\",
        "a~~~b~~",
        "\\\\srv\\share and grep \\*\\.py and ^\\d+$",
        "runs \\\\\\x \\\\\\\\ end \\",
        "a\\\\**b** `c`\\\\_d_",
    ],
)
def test_literal_text_round_trips(text: str) -> None:
    """019-AC5: characters typed literally survive an edit: what compose left as text stays text."""
    composed = compose_html(text, [], EMOJIS, full=True)
    form = _round_trip(text)
    assert compose_html(form.text, [], EMOJIS, full=True) == composed


def test_only_what_would_be_read_as_markdown_is_escaped() -> None:
    """019-AC5: `snake_case`, `2*3*4`, `[draft]`, `a * b` stay readable; a line that would start a list
    gets one leading backslash."""
    readable = "2*3*4 snake_case [draft] a * b"
    assert _round_trip(readable).text == readable
    assert _round_trip("\\- a\n\\1. b\n\\> c").text == "\\- a\n\\1. b\n\\> c"
    assert _form("<p>*a* [x](https://y.test)</p>").text == "\\*a\\* \\[x](https://y.test)"


def test_mentions_come_back_as_picks_in_text_order() -> None:
    """019-AC4 + 019-AC8: a person and everyone, twice, as the composer's picks; text says `@Name`."""
    form = _round_trip("@Ann Lee and @everyone, again @Ann Lee", [ANN, EVERYONE])
    assert form.text == "@Ann Lee and @everyone, again @Ann Lee"
    assert form.picks == [ANN, EVERYONE, ANN]


def test_custom_emoji_round_trips_as_its_shortcode() -> None:
    """019-AC4: a custom emoji the cache knows comes back as `:name:`; a stranger refuses."""
    assert _round_trip("party :fiesta:!").text == "party :fiesta:!"
    teams = (
        f'<img alt=":fiesta:" itemid="fiesta;{OID}" itemtype="http://schema.skype.com/CustomEmoji" src="x">'
    )
    assert _form(teams).text == ":fiesta:"
    for itemid in ("fiesta;0-other", "nope;x", "bad name;x"):
        tag = teams.replace(f"fiesta;{OID}", itemid)
        assert decompose(_msg(tag), EMOJIS) == "a custom emoji miniteams does not know", itemid


# --- what the Teams client writes ---


def test_teams_client_markup_reads_as_its_composer_equivalent() -> None:
    """019-AC4: strong/em/del, paragraphs, the animated emoji, a link whose text is its URL, and a name
    split into two mention spans."""
    emoji = ANIMATED[0]
    content = (
        f"<p>hi <strong>you</strong> <em>x</em> <del>y</del> {emoji}</p>\n"
        '<p><a href="https://x.test">https://x.test</a> <a href="https://y.test">why</a></p>'
        f"<p>{_span(0, 'Ann')} {_span(1, 'Lee')}</p>"
    )
    form = _form(content, mentions=_mentions(("8:orgid:ann", "person"), ("8:orgid:ann", "person")))
    assert form.text == "hi **you** _x_ ~~y~~ 🙂\nhttps://x.test [why](https://y.test)\n@Ann @Lee"
    assert form.picks == [{"name": "Ann", "mri": "8:orgid:ann"}, {"name": "Lee", "mri": "8:orgid:ann"}]
    assert _form("<pre><code>a\nb</code></pre>").text == "```\na\nb\n```"
    assert _form("<p>&nbsp;</p><div><div>x</div></div>").text == "\xa0\nx"
    assert _form('<div>\r\n<div itemprop="copy-paste-block"><p>pasted</p></div>\r\n</div>').text == "pasted"
    assert decompose(_msg('<div itemprop="other"><p>x</p></div>'), EMOJIS) == D.OTHER


# The animated-emoji wrappers stored Teams messages carry, verbatim in shape (attribute order, values).
ANIMATED = [
    '<span title="Smile" type="(smile)" class="animated-emoticon-50-smile" itemscope="">'
    '<img itemscope="" itemtype="http://schema.skype.com/Emoji" itemid="smile" '
    'src="https://statics.teams.cdn.office.net/evergreen-assets/personal-expressions/v2/assets/'
    'emoticons/smile/default/50_f.png?v=v4" '
    'title="Smile" alt="🙂" style="width:50px;height:50px;"></span>',
    '<span class="animated-emoticon-20-wink" title="(wink)" type="wink">'
    '<img itemscope="" itemtype="http://schema.skype.com/Emoji" itemid="wink" '
    'src="https://statics.teams.cdn.office.net//evergreen-assets/personal-expressions/v2/assets/'
    'emoticons//wink/default/100_f.png?v=v14" '
    'alt="😉" style="width:20px; height:20px"></span>',
]


@pytest.mark.parametrize("wrapper", ANIMATED)
def test_stored_animated_emoji_wrappers_convert(wrapper: str) -> None:
    """019-AC4: every animated emoji the Teams client stores reads as its character."""
    assert _form(f"<p>ok {wrapper} ok</p>").text[3:-3] in ("🙂", "😉")


def test_teams_hyperlinks_become_plain_links() -> None:
    """019-AC4: a Teams `HyperLink` reads as a link (its preview lives in properties, which Teams merges);
    a file link is not one."""
    link = (
        '<a href="https://x.test/a" rel="noreferrer noopener" target="_blank" title="https://x.test/a" '
        'itemtype="http://schema.skype.com/HyperLink" itemid="0-weu-d1-0000">{}</a>'
    )
    assert _form(f"<p>see {link.format('it')}</p>").text == "see [it](https://x.test/a)"
    assert _form(f"<p>{link.format('https://x.test/a')}</p>").text == "https://x.test/a"
    for kind in ("HyperLink/Files", "filesHyperlink", "Hyperlink"):
        files = link.replace("/HyperLink", f"/{kind}").format("it")
        assert decompose(_msg(f"<p>{files}</p>"), EMOJIS) == D.OTHER, kind


def test_a_link_title_may_differ_from_its_url_only_in_case() -> None:
    """019-AC4: Teams writes the title as typed, the href normalised."""
    link = '<a href="https://x.test/a" title="HTTPS://X.TEST/a">x</a>'
    assert _form(f"<p>{link}</p>").text == "[x](https://x.test/a)"


def test_a_no_op_font_size_span_is_transparent() -> None:
    """019-AC4: `font-size:inherit` changes nothing; any other size or colour refuses."""
    assert _form('<p>a <span style="font-size:inherit">b</span></p>').text == "a b"
    for style in ("font-size:x-large", "color:#000", "font-size:inherit; color:red"):
        assert decompose(_msg(f'<p><span style="{style}">b</span></p>'), EMOJIS) == "coloured or styled text"


def test_an_italic_inside_an_italic_takes_the_other_marker() -> None:
    """019-AC4: `<i>a <i>b</i> c</i>` comes back as `_a *b* c_`."""
    assert _form("<p><i>a <i>b</i> c</i></p>").text == "_a *b* c_"


def test_empty_paragraphs_around_reply_blocks_are_skipped() -> None:
    """019-AC6: older Teams replies put `<p></p>` before and after their Reply block."""
    block = reply_quote({"id": "1", "from": "8:orgid:ann", "messagetype": "Text", "content": "q"}, "Ann")
    form = _form(f"<p></p>\r\n{block}\r\n<p></p><p>answer</p>")
    assert (form.text, [q.key for q in form.quotes]) == ("answer", ["1"])
    assert _form(f"{block}<p>text</p><p></p>").text == "text"  # how most Teams replies end


@pytest.mark.parametrize(
    ("content", "text"),
    [
        ("<p>a</p><p></p>", "a"),
        ("<p></p><p>x</p>", "x"),
        ("<p>a</p><p></p><p>b</p>", "a\nb"),
        ("<p>a<br></p><p>b</p>", "a\nb"),
        ("<p>a</p><p><br></p><p>b</p>", "a\n\nb"),
        ("<p>a</p><p>&nbsp;</p><p>b</p>", "a\n\xa0\nb"),
    ],
)
def test_an_empty_paragraph_is_no_line(content: str, text: str) -> None:
    """019-AC4: `<p></p>` (or a paragraph's last `<br>`) renders nothing, so it is no line; a blank line
    Teams shows is `<p><br></p>` or `<p>&nbsp;</p>`, and stays one."""
    assert _form(content).text == text


def test_an_escaped_marker_inside_an_italic() -> None:
    """019-AC4: an italic holding a `*` and a non-inert `_` converts: the escaped one no longer closes."""
    form = _form("<p><i>a*b</i> <i>c_ d</i></p>")
    assert compose_html(form.text, [], EMOJIS, full=True)[0] == "<i>a*b</i> <i>c_ d</i>"


# Typed text the escapes must give back as typed, in both formats: line-start backslashes before a
# marker, a backslash cut from its marker by a URL, escapes next to a URL or an unfinished one.
TYPED = [
    r"\\- x",
    r"\\>",
    r"\\1. z",
    r"\\\- x",
    r"\- x",
    r"\\\\- x",
    r"\\\\\\> deep",
    r"\>http://#",
    r"\\>http://#",
    "http://`",
    "http:///`",
    "http://``",
    "see https://x.test/`a",
    ">",
    "> ",
    "a\n>\nb",
]


def _typed_round_trips(text: str, full: bool) -> None:
    composed = compose_html(text, [], {}, full=full)
    form = decompose(_msg(composed[0], **(composed[1] or {})), {})
    assert isinstance(form, Editable), (text, form)
    assert compose_html(form.text, [], {}, full=True) == composed, (text, form.text)


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("text", TYPED)
def test_typed_text_comes_back_as_typed(text: str, full: bool) -> None:
    """019-AC5: what the composer wrote from typed text (either format) is never refused nor altered."""
    _typed_round_trips(text, full)


_PIECES = [
    "\\",
    "\\",
    "-",
    ">",
    "1.",
    "*",
    "_",
    "~",
    "`",
    "[",
    "](https://x.test)",
    " ",
    "a",
    "\n",
    "https://h.t/",
    "#",
    "**",
    "~~",
    ":fire:",
]


@pytest.mark.parametrize("seed", range(4))
def test_random_backslash_and_marker_mixes_round_trip(seed: int) -> None:
    """019-AC5: bounded deterministic fuzz of backslashes, markers and URLs, in both formats.

    Lines starting with whitespace are left out: their leading whitespace is dropped (waived)."""
    rng = random.Random(seed)
    for _ in range(400):
        text = "\n".join(
            line.lstrip() for line in "".join(rng.choices(_PIECES, k=rng.randint(1, 12))).split("\n")
        )
        if not text.strip() or re.search(r"(?m)^([-*] |\d{1,9}\. |> ?)\s", text):
            continue
        for full in (False, True):
            _typed_round_trips(text, full)


def test_a_text_message_is_its_own_lines() -> None:
    """019-AC4: a `Text` message (no HTML) converts its lines, markup characters escaped."""
    form = decompose({"messagetype": "Text", "content": "a <b> *x*\n- y"}, {})
    assert isinstance(form, Editable)
    assert form.text == "a <b> \\*x\\*\n\\- y"


def test_reply_quotes_and_trailing_images_are_split_off_verbatim() -> None:
    """019-AC6 + 019-AC7 (server half): leading Reply blocks and the images after the text, in order,
    with what the page shows of them; the HTML stays on the server."""
    first = reply_quote(
        {"id": "1", "from": "8:orgid:ann", "messagetype": "RichText/Html", "content": "<p>q &lt;1&gt;</p>"},
        "Ann <Lee>",
    )
    second = reply_quote(
        {"id": "2", "from": "8:orgid:bo", "messagetype": "RichText/Html", "content": "<p>q2</p>"}, "Bo"
    )
    pictures = images_html(
        "gw.test", [("0-weu-d1-aaaa", Image("png", 3, 2)), ("0-weu-d1-bbbb", Image("gif", 5, 4))]
    )
    form = _form(f"{first}\n{second}<p>answer <b>here</b></p>{pictures}")
    assert form.text == "answer **here**"
    assert form.quotes == [D.Quote(first, "1", "Ann <Lee>", "q <1>"), D.Quote(second, "2", "Bo", "q2")]
    tags = pictures.removeprefix("<p>").removesuffix("</p>").replace("><", ">\0<").split("\0")
    assert [image.html for image in form.images] == tags
    assert [(image.key, image.src, image.alt) for image in form.images] == [
        ("0-weu-d1-aaaa", "https://gw.test/v1/objects/0-weu-d1-aaaa/views/imgo", "image"),
        ("0-weu-d1-bbbb", "https://gw.test/v1/objects/0-weu-d1-bbbb/views/imgo", "image"),
    ]
    assert _form(pictures).text == ""  # images alone, as miniteams sends them
    # A `> ` quote of my own is text, not a Reply block.
    assert _form("<blockquote>mine</blockquote>x").text == "> mine\nx"


# --- refusals (019-AC9 server half) ---


@pytest.mark.parametrize(
    ("content", "props", "reason"),
    [
        ("<table><tr><td>x</td></tr></table>", {}, "a table"),
        ("<h2>title</h2>", {}, "a heading"),
        ("<ul><li>a<ul><li>b</li></ul></li></ul>", {}, "a nested list"),
        ("<ul><ul><li>b</li></ul></ul>", {}, "a nested list"),
        ("<p><u>x</u></p>", {}, "underline"),
        ('<p><span style="color:red">x</span></p>', {}, "coloured or styled text"),
        ('<p><font color="red">x</font></p>', {}, "coloured or styled text"),
        ('<p><b style="color:red">x</b></p>', {}, "coloured or styled text"),
        ("<p>x</p>", {"cards": '[{"contentType": "x"}]'}, "a card"),
        ("<p>x</p>", {"files": '[{"fileName": "a.pdf"}]'}, "an attached file"),
        (f'<p>a <img itemtype="http://schema.skype.com/AMSImage" src="{AMS}"> b</p>', {}, D.IMAGE_IN_TEXT),
        (
            f'<p><img itemtype="http://schema.skype.com/AMSImage" src="{AMS}"></p><p>after</p>',
            {},
            D.IMAGE_IN_TEXT,
        ),
        (f'<p><b><img itemtype="http://schema.skype.com/AMSImage" src="{AMS}"></b></p>', {}, D.IMAGE_IN_TEXT),
        ('<p>x <img src="https://x.test/a.png"></p>', {}, D.IMAGE_IN_TEXT),
        ('<p><img itemtype="http://schema.skype.com/AMSImage" src="https://x.test/a.png"></p>', {}, D.OTHER),
        (
            f'<p>after text <img itemtype="http://schema.skype.com/AMSImage" src="{AMS}"></p>',
            {},
            D.IMAGE_IN_TEXT,
        ),
        (f"<p>{_span(0, 'Team')}</p>", {"mentions": _mentions(("t", "tag"))}, "a tag or team mention"),
        (f"<p>{_span(0, 'All')}</p>", {"mentions": _mentions(("t", "channel"))}, "a tag or team mention"),
        (f"<p>{_span(3, 'Ghost')}</p>", {}, D.OTHER),
        ('<blockquote itemtype="http://schema.skype.com/Forward">x</blockquote>', {}, "a forwarded message"),
        ("<hr>", {}, D.OTHER),
        ("<script>x</script>", {}, D.OTHER),
        ("<p><b>misnested</p></b>", {}, D.OTHER),
        # Markup carrying meaning compose cannot write (M2): unknown itemtypes, extra attributes.
        ('<p>a <span itemtype="http://schema.skype.com/Foo" itemid="1">z</span></p>', {}, D.OTHER),
        ('<p>a <span title="x" class="y">b</span></p>', {}, D.OTHER),
        ('<p><emoji alt="x">inner</emoji></p>', {}, D.OTHER),
        ('<p><emoji alt="x" data-k="1"></emoji></p>', {}, D.OTHER),
        ('<p><span class="animated-emoticon-50-x">text</span></p>', {}, D.OTHER),
        (
            '<p><span class="animated-emoticon-50-x" data-k="1">'
            '<img itemtype="http://schema.skype.com/Emoji" alt="🙂"></span></p>',
            {},
            D.OTHER,
        ),
        ('<p><img itemtype="http://schema.skype.com/Emoji" alt="🙂" onclick="x"></p>', {}, D.OTHER),
        ('<p><a href="https://x.test" title="elsewhere">x</a></p>', {}, D.OTHER),
        ('<p><a href="https://x.test" class="k">x</a></p>', {}, D.OTHER),
        ('<p><b class="k">x</b></p>', {}, D.OTHER),
        ('<p dir="rtl">x</p>', {}, D.OTHER),
        ("<pre class='language-py'><code>a</code></pre>", {}, D.OTHER),
        ('<blockquote itemtype="http://schema.skype.com/Foo">x</blockquote>', {}, D.OTHER),
        (f"<p>first</p>{reply_quote({'id': '1', 'from': 'x'}, 'Bo')}", {}, D.OTHER),
        (
            '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="x1">q</blockquote>',
            {},
            D.OTHER,
        ),
        (
            f'<p><span itemtype="{MENTION}" itemscope itemid="0" title="t">Ann</span></p>',
            {"mentions": _mentions(("8:orgid:ann", "person"))},
            D.OTHER,
        ),
        ("<p><b>unclosed</p>", {}, D.OTHER),
        ('<a href="javascript:x">j</a>', {}, D.OTHER),
        ('<a href="https://x.test"><b>formatted label</b></a>', {}, D.OTHER),
        ("<i>x</i>y", {}, D.OTHER),  # compose cannot close an italic before a letter
        ("<p>:fire: typed in Teams</p>", {}, D.OTHER),  # would expand on save
        ("<b>" * 40 + "x" + "</b>" * 40, {}, D.OTHER),
    ],
)
def test_what_compose_cannot_write_is_refused_with_its_reason(
    content: str, props: dict[str, Any], reason: str
) -> None:
    """019-AC9: a table, a heading, a nested list, underline, colour, a card, an image inside the text,
    a tag mention, or anything else compose cannot write back: refused, naming what blocks it."""
    assert decompose(_msg(content, **props), EMOJIS) == reason


def test_the_same_quote_or_image_twice_is_refused() -> None:
    """019-AC6 + 019-AC7: the page keeps quotes and images by key; two under one key could not be removed one
    at a time, so such a message is not editable here."""
    quote = reply_quote(
        {"id": "1", "from": "8:orgid:ann", "messagetype": "RichText/Html", "content": "<p>q</p>"}, "Ann"
    )
    other = reply_quote(
        {"id": "2", "from": "8:orgid:ann", "messagetype": "RichText/Html", "content": "<p>q</p>"}, "Ann"
    )
    image = Image("png", 3, 2)
    assert decompose(_msg(f"{quote}{quote}<p>x</p>"), EMOJIS) == "the same quote twice"
    twice = images_html("gw.test", [("0-weu-d1-aaaa", image), ("0-weu-d1-aaaa", image)])
    assert decompose(_msg(f"<p>x</p>{twice}"), EMOJIS) == "the same image twice"
    distinct = images_html("gw.test", [("0-weu-d1-aaaa", image), ("0-weu-d1-bbbb", image)])
    assert isinstance(decompose(_msg(f"{quote}{other}<p>x</p>{distinct}"), EMOJIS), Editable)


@pytest.mark.parametrize("msgtype", ["RichText/Media_Card", "RichText/Media_GenericFile", "Event/Call"])
def test_non_text_messages_are_refused(msgtype: str) -> None:
    """019-AC9: only text messages convert."""
    assert isinstance(decompose(_msg("<p>x</p>", msgtype), EMOJIS), str)


def test_a_literal_mention_name_is_escaped_rather_than_mentioning() -> None:
    """029-AC6: plain `@Ann Lee` next to a real mention of Ann comes back as `\\@Ann Lee`, so a save keeps
    it text; a `\\` before `@` stays one backslash."""
    content = f"<p>{_span(0, 'Ann Lee')} and @Ann Lee, a\\@b</p>"
    form = _form(content, mentions=_mentions(("8:orgid:ann", "person")))
    assert form.text == "@Ann Lee and \\@Ann Lee, a\\\\\\@b"
    assert compose_html(form.text, form.picks, EMOJIS, full=True)[0] == content[3:-4].replace(
        "itemscope ", 'itemscope="" '
    )
    assert _form("<p>mail a@b.com or @team</p>").text == "mail a@b.com or @team"


def test_a_text_that_needs_every_escape_still_converts() -> None:
    """019-AC5: when the readable escaping misreads a boundary, every markup character gets one."""
    form = _form("<p>a~<s>b</s> [<a href='https://x.test'>x</a></p>")
    assert form.text == "a\\~~~b~~ \\[[x](https://x.test)"


def test_accepted_attributes_still_convert() -> None:
    """019-AC4: what Teams and compose write around emojis, mentions and links is not refused."""
    emoji = '<emoji id="cool" alt="😎" title="Cool"></emoji>'
    link = '<a href="https://x.test" rel="noopener" target="_blank" title="https://x.test">go</a>'
    assert _form(f"<p>{emoji} {link}</p>").text == "😎 [go](https://x.test)"


def test_an_italic_an_underscore_cannot_close_uses_stars() -> None:
    """019-AC4: `<i>snake_case</i>` comes back as `*snake_case*` before any wide escaping."""
    assert _form("<p><i>snake_case</i> and _x_</p>").text == "*snake_case* and \\_x\\_"
    assert _round_trip("*snake_case*").text == "*snake_case*"


def test_a_backslash_converts_as_compose_reads_it() -> None:
    """019-AC5: a lone backslash or a pair before ordinary text stays bare; before markup it doubles."""
    assert _form("<p>\\\\srv\\share a\\<b>b</b> \\\\\\x</p>").text == "\\\\srv\\share a\\\\**b** \\\\\\\\x"


def test_the_size_cap_is_teams_send_limit() -> None:
    """A copy over the 28 KB Teams takes in a send is not one compose wrote: refused before parsing."""
    at_cap = "<p>" + "x" * (MAX_HTML_BYTES - 7) + "</p>"
    assert _form(at_cap).text == "x" * (MAX_HTML_BYTES - 7)
    assert decompose(_msg(at_cap.replace("x", "é", 1)), EMOJIS) == "too long"  # bytes, not characters


def test_long_bracket_runs_convert_in_linear_time() -> None:
    """4x a hostile text must not take ~16x the time (quadratic); linear is ~4."""

    def run(n: int) -> None:
        decompose(_msg("<p>" + "[" * n + "](" + "</p>"), {})

    assert _best_ms(lambda: run(28_000)) / max(_best_ms(lambda: run(7_000)), 0.05) < 8


# --- bare URLs as links (spec 028) ---


@pytest.mark.parametrize(
    "text",
    [
        "voici https://test.com/.",
        *(f"https://test.com/a{tail} b" for tail in ".,:;!?*_~)]}…–—«»‹›“”‘’"),
        "(voir https://test.com/a).",
        "see https://fr.wikipedia.org/wiki/Foo_(bar)) and https://x.test/a**",
        "**see https://test.com/a**.",
        "https://test.com/a\\_b_ and `https://c.test/`",
        "see https://en.wikipedia.org/wiki/Hitchhiker's_Guide.",
        "'quoted https://x.test/a'",
    ],
)
def test_a_bare_url_link_edits_back_to_the_text_typed(text: str) -> None:
    """028-AC3: an edit opens on the bare URL and its punctuation; saved unchanged, it posts the same HTML."""
    composed = compose_html(text, [], EMOJIS, full=True)[0]
    assert "<a href=" in composed
    assert _round_trip(text).text == text


@pytest.mark.parametrize("url", ["https://x.test/a.", "https://x.test/a?", "https://x.test/a_*"])
def test_a_link_to_its_own_address_with_trailing_punctuation_keeps_its_href(url: str) -> None:
    """028-AC3: a received link whose address ends in punctuation cannot be a bare URL: the cut would
    change its target."""
    form = _form(f'<p><a href="{url}">{url}</a></p>')
    assert form.text == f"[{url}]({url})"
    assert compose_html(form.text, [], EMOJIS, full=True)[0] == f'<a href="{url}">{url}</a>'


def test_a_link_to_its_own_address_ending_in_an_unopened_bracket_is_refused() -> None:
    """028-AC3: no markdown writes it back (`[x](…a))` ends at the first `)`): refused, not cut."""
    assert decompose(_msg('<p><a href="https://x.test/a)">https://x.test/a)</a></p>'), EMOJIS) == D.OTHER


@pytest.mark.parametrize(
    ("content", "editable"),
    [
        ('<a href="https://x.com">https://x.com</a>\'s docs', True),
        ('<a href="https://x.com/a b">https://x.com/a b</a>', False),
        ('<a href="https://x.com/a">https://x.com/a</a>(b)', True),
        ('<a href="https://x.com/a">https://x.com/a</a>b', True),
        ('<a href="https://x.com/a">https://x.com/a</a><a href="https://y.com">https://y.com</a>', True),
    ],
)
def test_a_self_titled_link_keeps_its_href_whatever_follows_it(content: str, editable: bool) -> None:
    """028-AC3: the text after a link to its own address never joins it on save; what no markdown writes
    back is refused, never altered."""
    form = decompose(_msg(f"<p>{content}</p>"), EMOJIS)
    assert isinstance(form, Editable) == editable, form
    if isinstance(form, Editable):
        composed = compose_html(form.text, form.picks, EMOJIS, full=True)[0]
        href = re.compile(r'href="([^"]*)"')
        assert href.findall(composed) == href.findall(content)
        assert D._read(composed, {}, EMOJIS) == D._read(f"<p>{content}</p>", {}, EMOJIS)


@pytest.mark.parametrize(
    "text", ["voir https://test.com/a et https://test.com/b.", "https://x.com's docs", "(https://x.com/a)"]
)
def test_a_url_sent_as_plain_text_stays_editable(text: str) -> None:
    """028-AC3: a message sent before links were written (plain-text URLs) still opens in the editor."""
    form = _form(f"<p>{html.escape(text)}</p>")
    assert form.text == text


@pytest.mark.parametrize(("html_", "md"), [("a@b.com", "a@b.com"), ("x_@y", "x\\_@y"), ("é@z", "é@z")])
def test_escape_everything_leaves_an_email_alone(html_: str, md: str) -> None:
    """029-AC6: in the escape-everything mode an `@` inside a word (an email) gets no backslash."""
    form = _form(f"<p>a~<s>b</s> [<a href='https://x.test'>x</a> {html_}</p>")
    assert form.text == f"a\\~~~b~~ \\[[x](https://x.test) {md}"
