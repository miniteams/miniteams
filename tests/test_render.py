"""Chat view sanitiser (spec 011, 011-AC6): allowlist, emoji, mentions, archived images."""

from pathlib import Path

import pytest

from miniteams.render import media_url, sanitize

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
        out == '<blockquote class="reply"><strong>Ann Lee</strong><p>question?</p></blockquote><p>answer</p>'
    )
    assert ANN not in out


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
        '<div><div><blockquote class="reply"><strong>Ann Lee</strong><p>q?</p></blockquote>'
        "<ul><li>a</li><li><div>b</div></li></ul></div><h2>Title</h2><hr><h3>s</h3></div>"
    )
