"""Chat view sanitiser (spec 011, 011-AC6): allowlist, emoji, mentions, archived images."""

import base64
import gc
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from miniteams.render import (
    _HTML_MAX,
    _MD_MAX,
    card_from_message,
    card_html,
    card_markdown_html,
    cards_html,
    media_url,
    sanitize,
)

T = "19:aaaa@thread.v2"
ME = "8:orgid:00000000-0000-0000-0000-00000000000a"
ANN = "8:orgid:00000000-0000-0000-0000-00000000000b"
OBJ = "0-weu-d1-0123456789abcdef"
SRC = f"https://eu-api.asm.skype.com/v1/objects/{OBJ}/views/imgo"


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "media"
    path.mkdir()
    (path / f"{OBJ}.png").write_bytes(b"png")
    return path


@pytest.mark.parametrize(
    "tag",
    [
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "del",
        "code",
        "pre",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "td",
    ],
)
def test_allowlisted_tags_are_kept_without_attributes(tag: str) -> None:
    """011-AC6: allowlisted tags survive, their attributes do not."""
    assert sanitize(f'<{tag} style="x" class="y" id="z">hi</{tag}>', T) == f"<{tag}>hi</{tag}>"


def test_th_br_hr_and_plain_blockquote() -> None:
    """011-AC6"""
    assert sanitize('a<hr class="x">b<HR/>c</hr>', T) == "a<hr>b<hr>c"
    assert sanitize("<th>h</th>a<br/>b<BR>c", T) == "<th>h</th>a<br>b<br>c"
    assert sanitize("<blockquote>q</blockquote>", T) == "<blockquote>q</blockquote>"


@pytest.mark.parametrize(
    ("raw", "out"),
    [
        ("<script>alert(1)</script>ok", "ok"),
        ("<SCRIPT>alert(1)</SCRIPT>ok", "ok"),
        ("<style>p{color:red}</style>ok", "ok"),
        ('<iframe src="https://x"><p>inside</p></iframe>ok', "ok"),
        # Raw text like in a browser: the first </iframe> ends it, the inner <iframe> is text.
        ("<iframe><iframe>a</iframe>b</iframe>ok", "bok"),
        ("<iframe><script>x</script></iframe>ok", "ok"),
        ("<script>never closed <p>gone", ""),
        ("<section><font>text</font></section>", "text"),
        ("<!-- note -->a<![CDATA[b]]>", "a"),
    ],
)
def test_dropped_content_and_unknown_tags(raw: str, out: str) -> None:
    """011-AC6: script/style/iframe vanish with their content; other tags lose the tag only."""
    assert sanitize(raw, T) == out


def test_self_closing_script_hides_what_follows_like_a_browser() -> None:
    """011-AC6: `<script/>` skips CDATA mode in the parser, so tags reach the hidden-depth guards."""
    assert sanitize("<script/><b>x</b></script>y", T) == "y"
    assert sanitize("<b><script/></b>x</script>y</b>", T) == "<b>y</b>"
    assert sanitize("<style/><img src='https://x/i.png'>", T) == ""


def test_self_closing_non_void_tag_renders_empty() -> None:
    """A browser would ignore the `/` and wrap the rest; closing at once keeps the output balanced."""
    assert sanitize("<b/>x", T) == "<b></b>x"
    assert sanitize("<p/>x</p>y", T) == "<p></p>xy"


def _best_s(fn: Any) -> float:
    gc.disable()  # a collection inside the window would blur a bound meant for the parser
    try:
        best = float("inf")
        for _ in range(3):
            # CPU time: on a loaded host the long run is preempted more than the short one.
            start = time.process_time()
            fn()
            best = min(best, time.process_time() - start)
        return best
    finally:
        gc.enable()


@pytest.mark.parametrize("unit", ["<b></i>", "<span></i>", "<b><a href='x'></u>"])
def test_unmatched_end_tags_stay_linear(unit: str) -> None:
    """CWE-407: each stray end tag used to rescan the whole open stack."""
    assert _best_s(lambda: sanitize(unit * (280_000 // len(unit)), T)) < 5  # generous: CI load
    small = _best_s(lambda: sanitize(unit * (25_000 // len(unit)), T))
    assert _best_s(lambda: sanitize(unit * (100_000 // len(unit)), T)) / small < 8


def test_input_is_capped() -> None:
    assert sanitize("x" * _HTML_MAX + "<b>y</b>", T) == "x" * _HTML_MAX


@pytest.mark.parametrize(
    "attr",
    [
        'onclick="alert(1)"',
        "ONMOUSEOVER='alert(1)'",
        'onerror="x" onload="y"',
        "style=\"background:url('javascript:x')\"",
    ],
)
def test_event_attributes_are_dropped(attr: str) -> None:
    """011-AC6"""
    assert sanitize(f"<b {attr}>x</b><p {attr}>y</p>", T) == "<b>x</b><p>y</p>"


def test_http_links_get_noopener_and_new_tab() -> None:
    """011-AC6"""
    out = sanitize('<a href="https://example.org/a?b=1&amp;c=&quot;2&quot;" onclick="x">site</a>', T)
    assert out == (
        '<a href="https://example.org/a?b=1&amp;c=&quot;2&quot;" rel="noopener" target="_blank">site</a>'
    )
    assert sanitize('<A HREF="http://example.org">x</A>', T).startswith('<a href="http://example.org"')


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "jav&#x61;script:alert(1)",
        "jav&#97;script:alert(1)",
        "&#106;avascript:alert(1)",
        " javascript:alert(1)",
        "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:x",
        "//example.org/x",
        "",
    ],
)
def test_non_http_hrefs_drop_the_link_keep_the_text(href: str) -> None:
    """011-AC6: only http(s) links survive, whatever the encoding."""
    assert sanitize(f'<a href="{href}">click</a>', T) == "click"


def test_quotes_and_markup_in_text_and_attributes_are_escaped(media: Path) -> None:
    """011-AC6: nothing in a value can break out of its attribute."""
    out = sanitize(f'<img src="{SRC}" alt="&quot; onerror=&quot;x" title=\'a"b<c\'>', T, media)
    assert out == (
        f'<img src="/media/19%3Aaaaa%40thread.v2/{OBJ}.png"'
        ' alt="&quot; onerror=&quot;x" title="a&quot;b&lt;c">'
    )
    assert sanitize("1 &lt; 2 &amp;&amp; <b>3 > 2</b>", T) == "1 &lt; 2 &amp;&amp; <b>3 &gt; 2</b>"


@pytest.mark.parametrize(
    ("raw", "out"),
    [
        ("<b><i>x</b>y", "<b><i>x</i></b>y"),
        ("<p><b>open", "<p><b>open</b></p>"),
        ("</p>stray</b>", "stray"),
        ("<ul><li>a<li>b</ul>", "<ul><li>a<li>b</li></li></ul>"),
        ('<a href="https://x', ""),
        ("text <b", "text "),
        ("<p>a</P>", "<p>a</p>"),
    ],
)
def test_malformed_markup_comes_out_balanced(raw: str, out: str) -> None:
    """011-AC6: unclosed, misnested and truncated tags never leave an open element."""
    assert sanitize(raw, T) == out


def test_unicode_emoji_becomes_its_character() -> None:
    """011-AC6: Teams' emoji image and the legacy <emoji> tag both render as the character."""
    teams = (
        '<span class="animated-emoticon-50-smile" title="Smile" type="(smile)">'
        '<img itemscope itemtype="http://schema.skype.com/Emoji" itemid="smile" '
        'src="https://statics.teams.cdn.office.net/smile.png" alt="🙂" title="Smile"></span>'
    )
    assert sanitize(f"<p>hi {teams}</p>", T) == "<p>hi 🙂</p>"
    assert sanitize('<emoji id="cool" alt="😎" title="Cool"></emoji>!', T) == "😎!"
    span = '<span itemtype="http://schema.skype.com/Emoji">👍</span>'
    assert sanitize(span, T) == "👍"


def test_custom_emoji_points_at_the_archived_file(media: Path) -> None:
    """011-AC6: an archived custom emoji is an <img class="emoji"> on /media."""
    tag = (
        f'<img alt=":party:" itemid="party;{OBJ}" itemtype="http://schema.skype.com/CustomEmoji" '
        f'src="https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/{OBJ}/views/imgt2_anim">'
    )
    assert sanitize(tag, T, media) == (
        f'<img src="/media/19%3Aaaaa%40thread.v2/{OBJ}.png" class="emoji" alt=":party:">'
    )
    # Not archived: the page cannot fetch it, so its name stands in.
    assert sanitize(tag, T, None) == ":party:"
    # Not archived yet but a known custom emoji (L7): the emoji cache serves it.
    assert sanitize(tag, T, None, custom=frozenset({OBJ})) == (
        f'<img src="/emoji/{OBJ}" class="emoji" alt=":party:">'
    )
    assert sanitize(tag, T, None, custom=frozenset({"other"})) == ":party:"
    # The archived file still wins, and a plain image is never routed to /emoji.
    assert sanitize(tag, T, media, custom=frozenset({OBJ})).startswith('<img src="/media/')
    assert "/emoji/" not in sanitize(
        f'<img src="{SRC.replace(OBJ, "x1")}">', T, None, custom=frozenset({"x1"})
    )


def test_mentions_become_mention_spans() -> None:
    """011-AC6: a Mention span carries the MRI; mine gets `mention-me`."""
    raw = (
        '<p><span itemtype="http://schema.skype.com/Mention" itemscope itemid="0">Ann</span> '
        '<span itemtype="http://schema.skype.com/Mention" itemscope itemid="1">Lee</span> and '
        '<span itemtype="http://schema.skype.com/Mention" itemscope itemid="2">Me</span> '
        '<span itemtype="http://schema.skype.com/Mention" itemid="9">Ghost</span></p>'
    )
    out = sanitize(raw, T, me=ME, mentions={"0": ANN, "1": ANN, "2": ME})
    assert out == (
        f'<p><span class="mention" data-mri="{ANN}">Ann</span> '
        f'<span class="mention" data-mri="{ANN}">Lee</span> and '
        f'<span class="mention mention-me" data-mri="{ME}">Me</span> '
        '<span class="mention">Ghost</span></p>'
    )


def test_a_dropped_span_inside_a_mention_does_not_close_it() -> None:
    """011-AC6: a plain span's end tag closes the plain span, not the mention around it."""
    raw = '<span itemtype="http://schema.skype.com/Mention" itemid="0"><span>Ann</span> Lee</span>!'
    assert sanitize(raw, T, mentions={"0": ANN}) == f'<span class="mention" data-mri="{ANN}">Ann Lee</span>!'
    assert sanitize('<span style="x">plain</span>', T) == "plain"


def test_reply_quote_renders_with_its_author() -> None:
    """011-AC6: a Reply blockquote keeps the quoted author and preview, not the MRI."""
    quote = (
        '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1700000000000">'
        f'<strong itemprop="mri" itemid="{ANN}">Ann Lee</strong>'
        '<span itemprop="time" itemid="1700000000000"></span>'
        '<p itemprop="preview">question?</p></blockquote><p>answer</p>'
    )
    out = sanitize(quote, T)
    assert (
        out == '<blockquote class="reply" data-msg="1700000000000"><strong>Ann Lee</strong>'
        "<p>question?</p></blockquote><p>answer</p>"
    )
    assert ANN not in out


@pytest.mark.parametrize("itemid", ["", "17x0", "1e3", "-1", "1 2", "1&quot;x", "\u0661\u0662", "\u00b2"])
def test_reply_quote_keeps_only_a_numeric_message_id(itemid: str) -> None:
    """021-AC1: the quoted message's id survives as `data-msg` only when it is ASCII digits."""
    quote = '<blockquote itemtype="http://schema.skype.com/Reply" itemid="{}"><p>q</p></blockquote>'
    assert sanitize(quote.format(itemid), T) == '<blockquote class="reply"><p>q</p></blockquote>'
    expected = '<blockquote class="reply" data-msg="1760000000001"><p>q</p></blockquote>'
    assert sanitize(quote.format("1760000000001"), T) == expected


def test_archived_image_is_rewritten_to_media(media: Path) -> None:
    """011-AC6"""
    raw = (
        f'<img itemscope itemtype="http://schema.skype.com/AMSImage" src="{SRC}" '
        'width="250" height="120px" id="x" onerror="alert(1)">'
    )
    assert sanitize(raw, T, media) == f'<img src="/media/19%3Aaaaa%40thread.v2/{OBJ}.png" width="250">'


def test_unarchived_image_becomes_a_link() -> None:
    """011-AC6: no token on the page, so the browser only gets a link to the original."""
    raw = f'<img itemtype="http://schema.skype.com/AMSImage" src="{SRC}">'
    expected = f'<a href="{SRC}" rel="noopener" target="_blank">[image not archived]</a>'
    assert sanitize(raw, T, None) == expected
    assert sanitize('<img src="javascript:alert(1)">', T) == "[image not archived]"
    assert sanitize('<img src="data:image/png;base64,AAAA">', T) == "[image not archived]"


@pytest.mark.parametrize(
    "src",
    [
        "https://eu-api.asm.skype.com/v1/objects/../../secret/views/imgo",
        "https://eu-api.asm.skype.com/v1/objects/*/views/imgo",
        "https://eu-api.asm.skype.com/v1/objects/.hidden/views/imgo",
        "https://eu-api.asm.skype.com/v1/objects/%2e%2e/views/imgo",
        "https://example.org/no-object-here.png",
    ],
)
def test_media_lookup_never_leaves_the_object_name(media: Path, src: str) -> None:
    """011-AC6: an object id with path or glob characters finds nothing."""
    (media / "object.png").write_bytes(b"x")  # what an id-less URL would otherwise match
    (media / ".hidden.png").write_bytes(b"x")
    assert media_url(T, media, src) is None


def test_media_lookup_skips_partial_downloads(media: Path) -> None:
    """011-AC6: a `.part` file is a download in flight, not an archived image."""
    other = "0-weu-d1-fedcba9876543210"
    (media / f"{other}.part").write_bytes(b"x")
    assert media_url(T, media, SRC.replace(OBJ, other)) is None
    assert media_url(T, media / "missing", SRC) is None


def test_nested_divs_keep_lists_and_reply_quotes() -> None:
    """011-AC6: div, h1-h3 and hr are structure: a quote and a list inside divs stay in place."""
    raw = (
        '<div style="x" itemprop="copy-paste-block"><div>'
        '<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="1700000000000">'
        f'<strong itemprop="mri" itemid="{ANN}">Ann Lee</strong>'
        '<span itemprop="time" itemid="1700000000000"></span><p itemprop="preview">q?</p></blockquote>'
        '<ul><li>a</li><li><div>b</div></li></ul></div><h2 id="t">Title</h2><hr><h3 onclick="x">s</h3></div>'
    )
    assert sanitize(raw, T) == (
        '<div><div><blockquote class="reply" data-msg="1700000000000">'
        "<strong>Ann Lee</strong><p>q?</p></blockquote>"
        "<ul><li>a</li><li><div>b</div></li></ul></div><h2>Title</h2><hr><h3>s</h3></div>"
    )


# --- Adaptive Cards (011-AC24) ---


def _tb(text: str, **kw: Any) -> dict[str, Any]:
    return {"type": "TextBlock", "text": text, **kw}


def _ac(*body: dict[str, Any], actions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "AdaptiveCard", "version": "1.4", "body": list(body), "actions": actions or []}


def _inner(card: dict[str, Any], media: Any = None) -> str:
    out = card_html(card, media)
    assert out.startswith('<div class="card">') and out.endswith("</div>")
    return out[len('<div class="card">') : -len("</div>")]


def _swift(payload: object, raw: str | None = None) -> dict[str, Any]:
    b64 = raw if raw is not None else base64.b64encode(json.dumps(payload).encode()).decode()
    content = (
        '<URIObject type="SWIFT.1" url_thumbnail="x"><Title>Build report</Title>'
        f'<Swift b64="{b64}"></Swift><Description></Description></URIObject>'
    )
    return {"messagetype": "RichText/Media_Card", "content": content, "properties": {}}


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


_CARD_TAGS = {"p", "br", "div", "b", "i", "s", "a", "img", "span", "table", "tr", "td", "th"}
_CARD_TAGS |= {"ul", "ol", "li", "pre", "code", "details", "summary"}
_SNIPPET = "application/vnd.microsoft.card.codesnippet"


_CARD_CLASSES = {"card", "card-app", "card-fallback", "tb", "w-bolder", "s-large", "s-medium", "s-small"}
_CARD_CLASSES |= {"subtle", "ct", "cols", "col", "facts", "acts", "act"}


def _assert_inert(out: str) -> None:
    """The embedding contract: allowlisted tags and classes, http(s) links, /media images only.

    `javascript:` may survive as visible text, never as an attribute value."""
    parser = _Tags()
    parser.feed(out)
    assert "<script" not in out.lower()
    for tag, attrs in parser.tags:
        assert tag in _CARD_TAGS, tag
        for name, value in attrs:
            assert name in {"class", "href", "rel", "target", "src", "alt"}, (tag, name)
            if name == "href":
                assert re.match(r"https?://", value or ""), value
            if name == "src":
                assert (value or "").startswith("/media/"), value
            if name == "class":
                assert set((value or "").split()) <= _CARD_CLASSES, value
            if name == "rel":
                assert value == "noopener", value
            if name == "target":
                assert value == "_blank", value


@pytest.mark.parametrize(
    ("text", "out"),
    [
        ("plain & <b>", "plain &amp; &lt;b&gt;"),
        ("**bold** and _it_", "<b>bold</b> and <i>it</i>"),
        ("snake_case_name stays", "snake_case_name stays"),
        ("a\r\nb\nc", "a<br>b<br>c"),
        (
            "[site](https://example.org/a?b=1&c=2)",
            '<a href="https://example.org/a?b=1&amp;c=2" rel="noopener" target="_blank">site</a>',
        ),
        ("[bad](javascript:alert(1))", "[bad](javascript:alert(1))"),
        ("[bad](javascript:void)", "bad"),
        ("[x](data:text/html,1)", "x"),
        ("intro\n- one\n- **two**\nafter", "intro<ul><li>one</li><li><b>two</b></li></ul>after"),
        ("1. first\n2. second", "<ol><li>first</li><li>second</li></ol>"),
        ("- a\n1. b", "<ul><li>a</li></ul><ol><li>b</li></ol>"),
        ("**not closed", "**not closed"),
        (
            "**<i>b</i>** _<s>_ [<b>](https://example.org)",
            "<b>&lt;i&gt;b&lt;/i&gt;</b> <i>&lt;s&gt;</i> "
            '<a href="https://example.org" rel="noopener" target="_blank">&lt;b&gt;</a>',
        ),
    ],
)
def test_markdown_subset(text: str, out: str) -> None:
    """011-AC24: the card markdown subset, every text escaped."""
    assert card_markdown_html(text) == out


@pytest.mark.parametrize("unit", ["**", "[", "_a", "[a](", "h"])
def test_markdown_is_bounded_on_hostile_text(unit: str) -> None:
    """011-AC24: unclosed markers stay linear (4× the input, not ~16× the time) and huge input is capped."""
    head = "[a](" if unit == "h" else ""  # one unclosed link whose url never ends

    def bomb(size: int) -> str:
        return head + unit * (size // len(unit))

    assert len(card_markdown_html(bomb(50_000))) <= _MD_MAX * 6
    small = _best_s(lambda: card_markdown_html(bomb(_MD_MAX // 4)))
    # The floor keeps a sub-50 µs run from turning timer noise into a ratio.
    assert _best_s(lambda: card_markdown_html(bomb(_MD_MAX))) / max(small, 50e-6) < 8


def test_text_block_weight_size_subtle() -> None:
    """011-AC24: TextBlock → div.tb with its weight, size and subtle classes."""
    assert _inner(_ac(_tb("Title", weight="Bolder", size="Large"))) == (
        '<div class="tb w-bolder s-large">Title</div>'
    )
    assert _inner(_ac(_tb("x", size="Medium", isSubtle=True), _tb("y", size="small"))) == (
        '<div class="tb s-medium subtle">x</div><div class="tb s-small">y</div>'
    )
    assert _inner(_ac(_tb(""), {"type": "TextBlock"})) == ""


def test_rich_text_block_runs() -> None:
    """011-AC24: TextRun styles and selectAction links."""
    card = _ac(
        {
            "type": "RichTextBlock",
            "inlines": [
                {"type": "TextRun", "text": "B", "weight": "bolder"},
                {"type": "TextRun", "text": "I", "italic": True},
                {"type": "TextRun", "text": "S\n", "strikethrough": True},
                {
                    "type": "TextRun",
                    "text": "go",
                    "selectAction": {"type": "Action.OpenUrl", "url": "https://example.org"},
                },
                "a bare string is not a run",
            ],
        }
    )
    assert _inner(card) == (
        "<p><b>B</b><i>I</i><s>S<br></s>"
        '<a href="https://example.org" rel="noopener" target="_blank">go</a></p>'
    )


def test_container_columns_facts() -> None:
    """011-AC24: Container → div.ct, ColumnSet → div.cols/div.col, FactSet → table.facts."""
    card = _ac(
        {"type": "Container", "items": [_tb("in")]},
        {
            "type": "ColumnSet",
            "columns": [
                {"type": "Column", "width": "auto", "items": [_tb("L")]},
                {
                    "type": "Column",
                    "items": [_tb("R")],
                    "selectAction": {"type": "Action.OpenUrl", "url": "https://example.org/r"},
                },
                {"type": "Column", "items": []},
            ],
        },
        {"type": "FactSet", "facts": [{"title": "Status", "value": "**Done**"}, {"title": "Owner"}]},
        {"type": "Container", "items": []},
    )
    assert _inner(card) == (
        '<div class="ct"><div class="tb">in</div></div>'
        '<div class="cols"><div class="col"><div class="tb">L</div></div>'
        '<a href="https://example.org/r" rel="noopener" target="_blank">'
        '<div class="col"><div class="tb">R</div></div></a></div>'
        '<table class="facts"><tr><th>Status</th><td><b>Done</b></td></tr>'
        "<tr><th>Owner</th><td></td></tr></table>"
    )


def test_image_only_from_the_archive() -> None:
    """011-AC24: an archived image → /media; anything else → its alt text, never an external src."""
    url = "https://cdn.example.org/logo.png"
    local = {url: "/media/t/logo.png"}.get
    card = _ac({"type": "Image", "url": url, "altText": "Logo"})
    assert _inner(card, local) == '<img src="/media/t/logo.png" alt="Logo">'
    assert _inner(card) == "<p>Logo</p>"
    assert _inner(card, lambda u: u) == "<p>Logo</p>"  # a resolver echoing the URL is not trusted
    assert _inner(_ac({"type": "Image", "url": url})) == ""
    assert "<img" not in _inner(_ac({"type": "Image", "url": "/media/../x", "altText": "a"}), lambda u: None)


def test_actions_and_action_set() -> None:
    """011-AC24: OpenUrl → link, Submit/Execute → plain title, ShowCard → details."""
    acts = [
        {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.org/run/1"},
        {"type": "Action.OpenUrl", "title": "Evil", "url": "javascript:alert(1)"},
        {"type": "Action.Submit", "title": "Approve"},
        {"type": "Action.Execute", "title": "Run"},
        {"type": "Action.ShowCard", "title": "More", "card": _ac(_tb("hidden"))},
        {"type": "Action.Submit"},
    ]
    assert _inner(_ac(actions=acts)) == (
        '<div class="acts"><a class="act" href="https://example.org/run/1" rel="noopener" '
        'target="_blank">Open</a><span class="act">Evil</span><span class="act">Approve</span>'
        '<span class="act">Run</span><details><summary>More</summary><div class="tb">hidden</div>'
        "</details></div>"
    )
    assert _inner(_ac({"type": "ActionSet", "actions": acts[2:3]})) == (
        '<div class="acts"><span class="act">Approve</span></div>'
    )


def test_unknown_and_input_elements() -> None:
    """011-AC24: unknown elements show text/title/label/placeholder, else nothing."""
    card = _ac(
        {"type": "Media", "title": "Clip"},
        {"type": "Input.Text", "placeholder": "Type here"},
        {"type": "Input.Toggle", "title": "Agree"},
        {"type": "Future.Thing", "data": 1},
        "not a dict",  # type: ignore[arg-type]
    )
    assert _inner(card) == "<p>Clip</p><p>Type here</p><p>Agree</p>"


def test_codesnippet() -> None:
    """011-AC24: codesnippet content → title + pre/code."""
    snippet = {"title": "main.py", "language": "python", "text": "if a < b:\n    print('<x>')"}
    assert card_html(snippet, content_type=_SNIPPET) == (
        '<div class="card"><div class="tb w-bolder">main.py</div>'
        "<pre><code>if a &lt; b:\n    print(&#x27;&lt;x&gt;&#x27;)</code></pre></div>"
    )


def test_hero_and_message_cards_render_markdown() -> None:
    """011-AC24: non-Adaptive cards are not code: title, markdown text, MessageCard sections."""
    hero = {"title": "Hero <b>", "text": "**hi** [l](https://example.org)"}
    assert _inner(hero) == (
        '<div class="tb w-bolder">Hero &lt;b&gt;</div><div class="tb"><b>hi</b> '
        '<a href="https://example.org" rel="noopener" target="_blank">l</a></div>'
    )
    o365 = {
        "@type": "MessageCard",
        "summary": "Alert",
        "text": "_fired_",
        "sections": [{"activityTitle": "Disk", "text": "90%", "facts": [{"name": "Host", "value": "db1"}]}],
    }
    assert _inner(o365) == (
        '<div class="tb"><i>fired</i></div><div class="tb w-bolder">Disk</div><div class="tb">90%</div>'
        '<table class="facts"><tr><th>Host</th><td>db1</td></tr></table>'
    )
    hero_att = {"contentType": "application/vnd.microsoft.card.hero", "content": hero}
    out = cards_html({"properties": {"cards": [hero_att]}})
    assert "<pre>" not in out and "<b>hi</b>" in out
    empty = {"contentType": "application/vnd.microsoft.card.hero", "content": {"images": []}}
    assert cards_html(_swift({"summary": "Fallback", "attachments": [empty]})) == (
        '<div class="card"><p class="card-fallback">Fallback</p></div>'
    )


def test_select_action_never_nests_or_empties_links() -> None:
    """011-AC24: no empty <a> around a missing image, no <a> around a container holding links."""
    open_url = {"type": "Action.OpenUrl", "url": "https://example.org/x"}
    img = {"type": "Image", "url": "https://cdn.example.org/a.png", "selectAction": open_url}
    assert _inner(_ac(img)) == ""
    linked = {"type": "Container", "items": [_tb("[in](https://example.org/in)")], "selectAction": open_url}
    out = _inner(_ac(linked))
    assert out.count("<a ") == 1 and "example.org/x" not in out
    column = {
        "type": "Column",
        "items": [{"type": "ActionSet", "actions": [{**open_url, "title": "t"}]}],
        "selectAction": open_url,
    }
    assert _inner(_ac({"type": "ColumnSet", "columns": [column]})).count("<a ") == 1


@pytest.mark.parametrize(
    "tag",
    ['<Swift b64="{}">', "<Swift b64='{}'>", '<swift  b64 = "{}" >', '<SWIFT data="1"\nb64="{}">'],
)
def test_swift_attribute_spellings(tag: str) -> None:
    """011-AC24: the Swift lookup takes either quote, spaces around `=` and any case."""
    payload = {"attachments": [{"content": _ac(_tb("ok"))}]}
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    msg = {"content": f"<URIObject>{tag.format(b64)}</Swift></URIObject>"}
    assert cards_html(msg) == '<div class="card"><div class="tb">ok</div></div>'


def test_per_message_budget() -> None:
    """011-AC24: at most 20 cards and 50k markdown characters per message."""
    many = [{"content": _ac(_tb(f"card {i}"))} for i in range(30)]
    assert cards_html({"properties": {"cards": many}}).count('<div class="card">') == 20
    big = [{"content": _ac(_tb("x" * 10_000), _tb("y" * 10_000))} for _ in range(5)]
    out = cards_html({"properties": {"cards": big}})
    assert out.count("x") + out.count("y") == 50_000


def test_swift_carrier() -> None:
    """011-AC24: a Media_Card's base64 Swift payload renders its card."""
    payload = {
        "type": "message/card",
        "summary": "Build 42 passed",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": _ac(
                    _tb("Build 42", weight="Bolder"),
                    {"type": "FactSet", "facts": [{"title": "Branch", "value": "main"}]},
                    actions=[{"type": "Action.OpenUrl", "title": "View", "url": "https://ci.example.org/42"}],
                ),
            }
        ],
    }
    msg = _swift(payload)
    cards = card_from_message(msg)
    assert cards is not None and len(cards) == 1
    assert cards[0]["fallback"] == "Build 42 passed" and cards[0]["app"] == ""
    assert cards_html(msg) == (
        '<div class="card"><div class="tb w-bolder">Build 42</div>'
        '<table class="facts"><tr><th>Branch</th><td>main</td></tr></table>'
        '<div class="acts"><a class="act" href="https://ci.example.org/42" rel="noopener" '
        'target="_blank">View</a></div></div>'
    )


def test_swift_codesnippet_and_summary_only() -> None:
    """011-AC24: a codesnippet attachment; a payload with only a summary shows the summary."""
    snip = {
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.codesnippet",
                "content": {"text": "x = 1", "language": "python"},
            }
        ]
    }
    assert cards_html(_swift(snip)) == '<div class="card"><pre><code>x = 1</code></pre></div>'
    assert cards_html(_swift({"summary": "Just <this>"})) == (
        '<div class="card"><p class="card-fallback">Just &lt;this&gt;</p></div>'
    )


@pytest.mark.parametrize("as_string", [True, False])
def test_properties_cards_carrier(as_string: bool) -> None:
    """011-AC24: properties.cards, as a JSON string or a list, with the app name on top."""
    cards = [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "appName": "Deploy Bot",
            "cardClientId": "c1",
            "content": _ac(_tb("Deployed v1.2")),
        }
    ]
    msg = {
        "messagetype": "RichText/Html",
        "content": "<p>see card</p>",
        "properties": {"cards": json.dumps(cards) if as_string else cards},
    }
    assert cards_html(msg) == (
        '<div class="card"><div class="card-app">Deploy Bot</div><div class="tb">Deployed v1.2</div></div>'
    )


def test_unknown_only_body_shows_summary_or_fallback_text() -> None:
    """011-AC24: a card that renders nothing falls back to summary, then fallbackText."""
    body = {"type": "Future.Thing"}
    with_summary = {"summary": "Weekly digest", "attachments": [{"content": _ac(body)}]}
    assert cards_html(_swift(with_summary)) == (
        '<div class="card"><p class="card-fallback">Weekly digest</p></div>'
    )
    content = {**_ac(body), "fallbackText": "Open in Teams"}
    msg = {"messagetype": "RichText/Html", "properties": {"cards": [{"content": content}]}}
    assert cards_html(msg) == '<div class="card"><p class="card-fallback">Open in Teams</p></div>'
    assert cards_html({"properties": {"cards": [{"content": _ac(body)}]}}) == ""


@pytest.mark.parametrize(
    "msg",
    [
        {},
        {"content": None, "properties": None},
        {"content": "<p>no card here</p>", "properties": {"cards": "[]"}},
        _swift(None, raw="!!!not base64!!!"),
        _swift(None, raw=base64.b64encode(b"\xff\xfe").decode()),
        _swift(None, raw=base64.b64encode(b"{not json").decode()),
        _swift(["a", "list"]),
        _swift({"attachments": "nope"}),
        _swift({"attachments": [1, None, {"content": "str"}]}),
        {"properties": {"cards": "{broken"}},
        {"properties": {"cards": "[" * 100_000}},
        {"properties": {"cards": [None, 3, {"content": [1]}]}},
        {"properties": {"cards": {"not": "a list"}}},
    ],
)
def test_bad_carriers_render_nothing(msg: dict[str, Any]) -> None:
    """011-AC24: malformed payloads never raise and render nothing."""
    assert cards_html(msg) == ""


def test_depth_and_element_caps() -> None:
    """011-AC24: a nesting bomb and 10k elements render bounded output."""

    def nest(levels: int) -> dict[str, Any]:
        el: dict[str, Any] = _tb("bottom")
        for _ in range(levels):
            el = {"type": "Container", "items": [el]}
        return el

    assert "bottom" in _inner(_ac(nest(7)))
    assert _inner(_ac(nest(8))) == ""  # past the depth cap
    assert _inner(_ac(nest(10_000))) == ""
    wide = card_html(_ac(*[_tb(f"line {i}") for i in range(10_000)]))
    assert wide.count('class="tb"') == 300
    columns = [{"type": "Column", "items": [_tb("c")] * 100} for _ in range(100)]
    assert card_html(_ac({"type": "ColumnSet", "columns": columns})).count(">c<") < 300


_EVIL = '<script>alert(1)</script>" onerror="alert(1)'
_JS = "javascript:alert(1)"


def _hostile_card() -> dict[str, Any]:
    run = {"type": "TextRun", "text": _EVIL, "selectAction": {"type": "Action.OpenUrl", "url": _JS}}
    return _ac(
        _tb(f"{_EVIL} [click]({_JS}) [ok](https://example.org/{_EVIL})", weight=_EVIL, size=_EVIL),
        {"type": "RichTextBlock", "inlines": [run]},
        {"type": "Container", "items": [_tb(_EVIL)], "selectAction": {"type": "Action.OpenUrl", "url": _JS}},
        {"type": "FactSet", "facts": [{"title": _EVIL, "value": _EVIL}]},
        {"type": "Image", "url": "https://evil.example.org/x.png", "altText": _EVIL},
        {"type": "Image", "url": _JS, "altText": "a"},
        {"type": _EVIL, "text": _EVIL},
        actions=[
            {"type": "Action.OpenUrl", "title": _EVIL, "url": _JS},
            {"type": "Action.OpenUrl", "title": "ok", "url": f"https://example.org/{_EVIL}"},
            {"type": "Action.ShowCard", "title": _EVIL, "card": _ac(_tb(_EVIL))},
            {"type": "Action.Submit", "title": _EVIL},
        ],
    )


def test_hostile_strings_stay_inert() -> None:
    """011-AC24: no script, no on* attribute, no javascript: URL, no external src survives."""
    out = card_html(_hostile_card(), lambda u: u)  # even a resolver passing URLs through
    _assert_inert(out)
    assert "<img" not in out and "evil.example.org" not in out
    for content_type in ("", _SNIPPET):
        _assert_inert(card_html({"title": _EVIL, "text": _EVIL}, content_type=content_type))


def test_card_html_embedding_contract() -> None:
    """011-AC24: every element kind, carrier and fallback emits only the contract's tags, classes
    and attributes, so chat_record embeds card HTML as is (sanitize() would strip its classes)."""
    run = {"type": "TextRun", "text": "r\nr", "weight": "bolder", "italic": True, "strikethrough": True}
    open_url = {"type": "Action.OpenUrl", "url": "https://example.org"}
    full = _ac(
        _tb("**b** _i_ [l](https://example.org)\n- x\n1. y", weight="Bolder", size="Large", isSubtle=True),
        _tb("m", size="Medium"),
        _tb("s", size="Small"),
        {"type": "RichTextBlock", "inlines": [run]},
        {"type": "ColumnSet", "columns": [{"type": "Column", "items": [_tb("c")]}]},
        {"type": "Container", "items": [_tb("t")], "selectAction": open_url},
        {"type": "FactSet", "facts": [{"title": "k", "value": "v"}]},
        {"type": "Image", "url": "https://cdn.example.org/a.png", "altText": "a"},
        {"type": "Input.Text", "placeholder": "p"},
        actions=[
            {**open_url, "title": "o"},
            {"type": "Action.Submit", "title": "s"},
            {"type": "Action.ShowCard", "title": "more", "card": _ac(_tb("h"))},
        ],
    )
    message_card = {"@type": "MessageCard", "title": "t", "sections": [{"facts": [{"name": "n"}]}]}
    outs = [
        card_html(full, lambda u: "/media/t/a.png"),
        card_html(_hostile_card(), lambda u: u),
        card_html({"title": "f.py", "text": "x"}, content_type=_SNIPPET),
        card_html(message_card),
        cards_html({"properties": {"cards": [{"appName": "Bot", "content": full}]}}),
        cards_html(_swift({"summary": "only"})),
    ]
    for out in outs:
        _assert_inert(out)
    # The fixture covers the whole contract, so a new tag cannot slip in untested.
    assert {tag for out in outs for tag in re.findall(r"<([a-z]+)", out)} == _CARD_TAGS
    assert {
        c for out in outs for m in re.findall(r'class="([^"]*)"', out) for c in m.split()
    } == _CARD_CLASSES


def test_spent_budget_leaves_no_empty_blocks_and_keeps_the_fallback() -> None:
    """011-AC24: once the markdown budget is gone, later text blocks vanish instead of leaving
    empty divs, and a card with nothing left to show falls back to its summary."""
    big = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "x" * 10_000}] * 6}
    small = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "late"}]}
    cards = [{"contentType": "application/vnd.microsoft.card.adaptive", "content": big}] * 5
    cards.append({"contentType": "application/vnd.microsoft.card.adaptive", "content": small})
    msg = {"messagetype": "RichText/Html", "content": "", "properties": {"cards": cards}}
    out = cards_html(msg)
    assert '<div class="tb"></div>' not in out
    assert "late" not in out
    msg["properties"]["cards"][-1]["content"]["fallbackText"] = "late summary"
    assert "late summary" in cards_html(msg)


def test_link_label_with_a_bracket_pair_and_toggle_actions() -> None:
    """011-AC24: a label such as "[repo [!12]](url)" is still a link, as GitLab cards write it; a
    ToggleVisibility action has no place on a read-only card."""
    html = card_markdown_html(
        "[repo [!12]](https://x.org/mr/12) and [[a]](https://x.org/a) [[[b]]](https://x.org/b)"
    )
    assert '<a href="https://x.org/mr/12" rel="noopener" target="_blank">repo [!12]</a>' in html
    assert ">[a]</a>" in html
    assert "[[[b]]]" in html  # two levels stay literal
    card = {
        "type": "AdaptiveCard",
        "body": [
            {
                "type": "ActionSet",
                "actions": [
                    {"type": "Action.ToggleVisibility", "title": "Show details..."},
                    {"type": "Action.ToggleVisibility", "title": "Hide details..."},
                    {"type": "Action.OpenUrl", "title": "Diff", "url": "https://x.org/d"},
                ],
            }
        ],
    }
    out = card_html(card)
    assert "details..." not in out
    assert ">Diff</a>" in out
    toggle = {"type": "Action.ToggleVisibility", "targetElements": ["x"]}
    card = {
        "type": "AdaptiveCard",
        "body": [
            {
                "type": "RichTextBlock",
                "isVisible": False,
                "inlines": [
                    {"type": "TextRun", "text": "Hide details...", "selectAction": toggle},
                    {"type": "TextRun", "text": "kept", "weight": "bolder"},
                ],
            },
            {"type": "TextBlock", "text": "Show details...", "selectAction": toggle},
            {"type": "Container", "isVisible": False, "items": [{"type": "TextBlock", "text": "inside"}]},
        ],
    }
    out = card_html(card)
    assert (
        "details..." not in out and "<b>kept</b>" in out and "inside" in out
    )  # hidden content shows, toggles do not


def test_a_malformed_url_is_dropped_not_raised() -> None:
    """011-AC24: `urlsplit` raises on "https://[x"; a bot card or a body link carrying it must only
    lose the link, never the message."""
    card = {
        "type": "AdaptiveCard",
        "body": [{"type": "TextBlock", "text": "t"}],
        "actions": [{"type": "Action.OpenUrl", "title": "Go", "url": "https://[x"}],
    }
    out = card_html(card)
    assert "<a " not in out and ">Go</span>" in out
    assert "<a " not in sanitize('<a href="https://[x">y</a>', "19:t@thread.v2", None)


def test_table_rows_render_as_a_facts_table() -> None:
    """011-AC24: an Adaptive Cards 1.5 Table (rows of cells holding elements) renders like a FactSet,
    first row as headers when the card says so; hostile cells stay escaped."""
    cell = lambda text: {"type": "TableCell", "items": [{"type": "TextBlock", "text": text}]}  # noqa: E731
    card = {
        "type": "AdaptiveCard",
        "body": [
            {
                "type": "Table",
                "firstRowAsHeaders": True,
                "rows": [
                    {"type": "TableRow", "cells": [cell("Status"), cell("Value")]},
                    {"type": "TableRow", "cells": [cell("branch"), cell("<main>")]},
                    "junk",
                ],
            }
        ],
    }
    out = card_html(card)
    assert out.count("<tr>") == 2 and "<th>" in out and "&lt;main&gt;" in out
    assert out.index("<th>") < out.index("<td>")
    plain = dict(card["body"][0])
    plain.pop("firstRowAsHeaders")
    assert "<th>" not in card_html({"type": "AdaptiveCard", "body": [plain]})
