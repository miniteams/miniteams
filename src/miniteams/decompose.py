"""Teams message HTML → the composer's markdown (spec 019): the inverse of `compose_html`, or why not.

Pure. A conversion is only offered once its markdown composes back to the same message, so an element
read wrong refuses the edit instead of altering the message.
"""

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from .attachments import object_id
from .compose import _URL_RE, EMOJI_NAME_RE, ESCAPABLE, _literal, _word, compose_html
from .images import OBJECT_ID_RE
from .messages import TEXT_TYPES, json_list
from .send import MAX_HTML_BYTES

_DEPTH = 32  # open elements: compose nests a few spans, never more
_REPLY = "schema.skype.com/Reply"
_IGNORED = " \t\n\r\f"  # HTML layout whitespace; a no-break space is text
_MARKS = {"b": "b", "strong": "b", "i": "i", "em": "i", "s": "s", "strike": "s", "del": "s"}
_MARKERS = {"b": "**", "i": "_", "s": "~~"}
_ITALICS = ("_", "*")  # `_x_` reads best; `*x*` where an underscore cannot close (`_snake_case_`)
_QUOTE_KEY_RE = re.compile(r"[0-9]{1,20}")  # a Reply block names the quoted message id
# Attributes an element may carry: one outside its set may hold meaning compose would drop.
_EMOJI_IMG = frozenset("itemscope itemtype itemid src alt title style width height".split())
_EMOJI_TAG = frozenset(("id", "alt", "title"))
_EMOJI_SPAN = frozenset(("itemscope", "itemtype", "itemid", "title"))
_ANIMATED = frozenset(("class", "itemscope", "title", "type"))  # Teams' animated emoji around its <img>
_MENTION_SPAN = frozenset(("itemscope", "itemtype", "itemid"))
_LINK = frozenset(("href", "rel", "target", "title", "itemtype", "itemid"))
_HYPERLINK = "http://schema.skype.com/HyperLink"  # a Teams link (its preview lives in properties)
_BLOCKED = {
    **dict.fromkeys(("table", "thead", "tbody", "tr", "td", "th"), "a table"),
    **dict.fromkeys(("h1", "h2", "h3", "h4", "h5", "h6"), "a heading"),
    "u": "underline",
    "ins": "underline",
    "font": "coloured or styled text",
}
OTHER = "formatting miniteams cannot write"
IMAGE_IN_TEXT = "an image inside the text"

# ("text", s) | ("b" | "i" | "s", tokens) | ("code", s) | ("a", href, label)
# | ("mention", everyone, mri, label) | ("emoji", name, object id)
type Token = tuple[Any, ...]
type Block = tuple[str, Any]  # ("line", tokens) | ("blockquote" | "ul" | "ol", lines) | ("pre", code)


class Quote(NamedTuple):
    html: str  # verbatim from the message: kept as is on save
    key: str  # the quoted message id: names it across edits made elsewhere
    name: str
    line: str


class KeptImage(NamedTuple):
    html: str  # the verbatim <img> tag
    key: str  # its AMS object id
    src: str
    alt: str


@dataclass(frozen=True, slots=True)
class Editable:
    text: str
    picks: list[dict[str, Any]]  # the composer's picked mentions, one per occurrence in text order
    quotes: list[Quote]
    images: list[KeptImage]


class _Refused(Exception):
    pass


@dataclass(slots=True)
class _Frame:
    tag: str
    kind: str  # lines | mark | raw | list | container | skip | emoji (no content) | animated
    items: list[Any] = field(default_factory=list)  # lines: blocks; mark: tokens; list: items
    cur: list[Token] | None = None  # lines: the open text line
    attrs: dict[str, str] = field(default_factory=dict)


def _only(attrs: dict[str, str], allowed: frozenset[str]) -> None:
    if not attrs.keys() <= allowed:
        raise _Refused(OTHER)


def _add(tokens: list[Token], token: Token) -> None:
    if token[0] == "text" and tokens and tokens[-1][0] == "text":
        tokens[-1] = ("text", tokens[-1][1] + token[1])
    elif token != ("text", ""):
        tokens.append(token)


class _Reader(HTMLParser):
    """Teams HTML → blocks in compose's own model; `_Refused` on anything compose cannot write."""

    def __init__(self, mentions: dict[str, dict[str, Any]], emojis: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.mentions, self.emojis = mentions, emojis
        self.stack = [_Frame("", "lines")]
        self.images: list[KeptImage] = []

    @property
    def root(self) -> _Frame:
        return self.stack[0]

    def _lines(self) -> _Frame:
        return next(f for f in reversed(self.stack) if f.kind == "lines")

    def _no_images_yet(self) -> None:
        if self.images:
            raise _Refused(IMAGE_IN_TEXT)

    def _target(self) -> _Frame:
        """Where inline content lands: the innermost span or lines, through transparent elements."""
        return next(f for f in reversed(self.stack) if f.kind not in ("skip", "container", "animated"))

    def _inline(self, token: Token) -> None:
        self._no_images_yet()
        target = self._target()
        if target.kind == "mark":
            _add(target.items, token)
            return
        if target.kind != "lines":
            raise _Refused(OTHER)
        if target.cur is None:
            target.cur = []
        _add(target.cur, token)

    def _flush(self, lines: _Frame) -> None:
        if lines.cur is not None:
            lines.items.append(("line", tuple(lines.cur)))
            lines.cur = None

    def _push(self, tag: str, kind: str, attrs: dict[str, str] | None = None) -> None:
        if len(self.stack) > _DEPTH:
            raise _Refused(OTHER)
        self.stack.append(_Frame(tag, kind, attrs=attrs or {}))

    def _block(self, tag: str, kind: str) -> None:
        """A block opens only between lines of the message itself: not in a quote, a list or a span."""
        self._no_images_yet()
        if any(f.kind != "container" for f in self.stack[1:]):
            raise _Refused("a nested list" if tag in ("ul", "ol") and self._in("li") else OTHER)
        self._flush(self.root)
        self._push(tag, kind)

    def _in(self, kind: str) -> bool:
        return any(f.tag == kind for f in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        itemtype = a.get("itemtype", "")
        top = self.stack[-1]
        if top.kind == "raw":
            if top.tag == "pre" and tag == "code" and not top.items and not a:
                self._push(tag, "skip")
            elif top.tag == "pre" and tag == "br" and not a:
                top.items.append("\n")
            elif top.tag == "span" and tag == "span" and not a:
                self._push(tag, "skip")
            else:
                raise _Refused(OTHER)
            return
        if top.kind == "emoji" or (
            top.kind == "animated" and not (tag == "img" and itemtype.endswith("/Emoji"))
        ):
            raise _Refused(OTHER)
        if top.kind == "list" and tag != "li":
            raise _Refused("a nested list" if tag in ("ul", "ol") else OTHER)
        if tag in _BLOCKED:
            raise _Refused(_BLOCKED[tag])
        if (
            tag == "span"
            and a.keys() == {"style"}
            and a["style"].replace(" ", "").rstrip(";") == "font-size:inherit"
        ):
            self._push(tag, "skip")  # the Teams client's no-op wrapper: the size it already has
            return
        if a.get("style") and tag != "img":
            raise _Refused("coloured or styled text")
        if "Forward" in itemtype:
            raise _Refused("a forwarded message")
        if a == {"itemprop": "copy-paste-block"} and tag == "div":
            a = {}  # what the Teams client wraps pasted text in: a paragraph like any other
        if a and tag not in ("img", "emoji", "span", "a"):
            raise _Refused(OTHER)  # compose writes these bare
        if tag == "img":
            self._img(a, itemtype)
        elif tag == "emoji":
            _only(a, _EMOJI_TAG)
            self._inline(("text", a.get("alt", "")))
            self._push(tag, "emoji")
        elif tag == "span":
            self._span(a, itemtype)
        elif tag == "a":
            _only(a, _LINK)
            href = a.get("href", "")
            if (
                a.get("title", href).casefold() != href.casefold()
                or a.get("itemtype", _HYPERLINK) != _HYPERLINK
            ):
                raise _Refused(OTHER)
            self._no_images_yet()
            self._push(tag, "raw", a)
        elif tag in _MARKS:
            self._no_images_yet()
            self._push(tag, "mark")
        elif tag == "code":
            self._no_images_yet()
            self._push(tag, "raw")
        elif tag == "br":
            self._no_images_yet()
            if (lines := self._target()).kind != "lines":
                raise _Refused(OTHER)
            lines.items.append(("line", tuple(lines.cur or ())))
            lines.cur = []
        elif tag in ("p", "div"):
            if top.kind not in ("lines", "container"):
                raise _Refused(OTHER)
            lines = self._lines()
            if not self.images:
                if lines.cur:
                    self._flush(lines)
                lines.cur = []
            self._push(tag, "container")
        elif tag == "blockquote":
            self._block(tag, "lines")
        elif tag in ("ul", "ol"):
            self._block(tag, "list")
        elif tag == "li":
            if top.kind != "list":
                raise _Refused(OTHER)
            self._push(tag, "lines")
        elif tag == "pre":
            self._block(tag, "raw")
        else:
            raise _Refused(OTHER)

    def _span(self, a: dict[str, str], itemtype: str) -> None:
        if itemtype.endswith("/Mention"):
            _only(a, _MENTION_SPAN)
            self._push("span", "raw", a)
        elif itemtype.endswith("/Emoji"):
            _only(a, _EMOJI_SPAN)
            self._push("span", "skip")  # its text is the character
        elif a.get("class", "").startswith("animated-emoticon"):
            _only(a, _ANIMATED)
            self._push("span", "animated")
        elif a:
            raise _Refused(OTHER)
        else:
            self._push("span", "skip")

    def _img(self, a: dict[str, str], itemtype: str) -> None:
        if itemtype.endswith("/Emoji"):
            _only(a, _EMOJI_IMG)
            self._inline(("text", a.get("alt", "")))
        elif itemtype.endswith("/CustomEmoji"):
            _only(a, _EMOJI_IMG)
            name, _, oid = a.get("itemid", "").partition(";")
            if not EMOJI_NAME_RE.fullmatch(name) or self.emojis.get(name) != oid:
                raise _Refused("a custom emoji miniteams does not know")
            self._inline(("emoji", name, oid))
        elif itemtype.endswith("/AMSImage"):
            # Only after the text, alone on its own lines: how miniteams posts them.
            src, lines = a.get("src", ""), self._lines()
            if lines is not self.root or lines.cur or any(f.kind != "container" for f in self.stack[1:]):
                raise _Refused(IMAGE_IN_TEXT)
            key = object_id(src) if "/objects/" in src else ""  # object_id() has a placeholder for none
            if urlsplit(src).scheme not in ("http", "https") or not OBJECT_ID_RE.fullmatch(key):
                raise _Refused(OTHER)
            lines.cur = None
            self.images.append(KeptImage(self.get_starttag_text() or "", key, src, a.get("alt", "")))
        else:
            raise _Refused(IMAGE_IN_TEXT)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in ("br", "img"):
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("br", "img"):
            return
        frame = self.stack[-1]
        if frame.tag != tag or len(self.stack) == 1:
            raise _Refused(OTHER)  # misnested or stray: not markup compose writes
        self.stack.pop()
        text = "".join(frame.items) if frame.kind == "raw" else ""
        if frame.kind == "mark":
            self._inline((_MARKS[tag], tuple(frame.items)))
        elif frame.kind == "container":
            lines = self._lines()
            if lines.cur == []:
                lines.cur = None  # an empty paragraph renders nothing, nor does a block's last <br>
            elif not self.images:
                self._flush(lines)
        elif tag == "code" and frame.kind == "raw":
            self._inline(("code", text))
        elif tag == "a":
            href = frame.attrs.get("href", "")
            if urlsplit(href).scheme not in ("http", "https"):
                raise _Refused(OTHER)
            self._inline(("text", href) if text == href else ("a", href, text))
        elif tag == "span" and frame.kind == "raw":
            self._mention(frame.attrs.get("itemid", ""), text)
        elif tag == "pre":
            self.root.items.append(("pre", text))
        elif tag == "blockquote":
            self._flush(frame)
            self.root.items.append((tag, tuple(line for _, line in frame.items)))
        elif tag == "li":
            self._flush(frame)
            if len(frame.items) > 1:
                raise _Refused(OTHER)
            self.stack[-1].items.append(frame.items[0][1] if frame.items else ())
        elif tag in ("ul", "ol"):
            self.root.items.append((tag, tuple(frame.items)))

    def _mention(self, itemid: str, label: str) -> None:
        entry = self.mentions.get(itemid)
        kind = str(entry.get("mentionType") or "") if entry else ""
        if entry is None or kind not in ("person", "everyone"):
            raise _Refused("a tag or team mention" if entry else OTHER)
        self._inline(("mention", kind == "everyone", str(entry.get("mri") or ""), label))

    def handle_data(self, data: str) -> None:
        top = self.stack[-1]
        if top.kind == "raw":
            top.items.append(data if top.tag == "pre" else data.replace("\n", " "))
            return
        if top.kind in ("emoji", "animated"):
            if data.strip(_IGNORED):
                raise _Refused(OTHER)  # an emoji is its alt: text inside it would be lost
            return
        if top.kind == "skip" and (raw := self.stack[-2]).kind == "raw":
            raw.items.append(data if raw.tag == "pre" else data.replace("\n", " "))
            return
        target = self._target()
        # Layout whitespace: between blocks, or leading a line (where HTML renders none).
        if not data.strip(_IGNORED) and (
            target.kind == "list" or self.images or target.kind == "lines" and not target.cur
        ):
            return
        self._inline(("text", data.replace("\r", "").replace("\n", " ")))

    def blocks(self) -> list[Block]:
        self.close()
        if len(self.stack) > 1:
            raise _Refused(OTHER)
        self._flush(self.root)
        blocks: list[Block] = self.root.items
        return blocks


def _read(content: str, props: dict[str, Any], emojis: dict[str, str]) -> tuple[list[Block], list[KeptImage]]:
    entries = json_list(props.get("mentions"))
    mentions = {str(e.get("itemid")): e for e in entries if isinstance(e, dict)}
    reader = _Reader(mentions, emojis)
    reader.feed(content)
    return reader.blocks(), reader.images


def _escape(seg: str, wide: bool, line_start: bool, following: str, after_url: bool = False) -> str:
    """`seg` (plain text, no URL, `following` the markdown after it) as composer text: a character
    compose would read as markup gets a backslash; `wide` escapes every one of them."""
    out: list[str] = []
    i, n = 0, len(seg)
    link_end = seg.rfind("](")  # past it a `[` cannot open a link
    while i < n:
        ch = seg[i]
        if (after_url and not i) or seg.endswith(("http://", "https://"), 0, i):
            # A backslash here would extend the URL compose reads (`http://\``): the run stays bare.
            j = i + 1
            while ch == "`" and j < n and seg[j] == "`":
                j += 1
            out.append(seg[i:j])
            i = j
            continue
        if ch in "*_~":
            j = i
            while j < n and seg[j] == ch:
                j += 1
            prev, nxt = seg[i - 1 : i], seg[j : j + 1]
            # Intra-word or space-padded runs never open or close a span: `snake_case`, `a * b`.
            inert = bool(prev and nxt) and (
                (_word(prev) and _word(nxt))
                or (prev.isspace() and nxt.isspace())
                or (ch == "~" and j - i == 1)
            )
            out.append(seg[i:j] if inert and not wide else "".join("\\" + c for c in seg[i:j]))
            i = j
            continue
        if ch == "`" or (ch == "[" and (wide or i < link_end)):
            out.append("\\" + ch)
        elif ch == "\\":
            j = i
            while j < n and seg[j] == "\\":
                j += 1
            k = j - i
            if (seg[j : j + 1] or following) in ESCAPABLE:
                out.append("\\" * 2 * k)
            elif line_start and not i and _literal(rest := "\\" + seg[j:] + following) != rest:
                # Before a line-start marker compose strips a lone backslash, and collapses each pair
                # followed by another backslash: two read as one, m >= 3 as m - (m - 1) // 2.
                out.append("\\" * max(2 * k - 1, 2))
            else:  # compose collapses a pair only before an escapable character: the last two stay bare
                out.append("\\" * (2 * max(k - 2, 0) + min(k, 2)))
            i = j
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _text_md(text: str, wide: bool, line_start: bool, following: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _URL_RE.finditer(text):  # compose reads a bare URL whole, backslashes included
        out += (_escape(text[pos : m.start()], wide, line_start and pos == 0, "h", pos > 0), m.group())
        pos = m.end()
    out.append(_escape(text[pos:], wide, line_start and pos == 0, following, pos > 0))
    return "".join(out)


def _inline_md(
    tokens: tuple[Token, ...], wide: bool, italic: str, line_start: bool = False, following: str = ""
) -> str:
    """Right to left: a text's last backslash depends on the markdown that follows it."""
    markers = {**_MARKERS, "i": italic}
    out: list[str] = []
    for n in range(len(tokens) - 1, -1, -1):
        token, after = tokens[n], out[-1][:1] if out else following
        kind = token[0]
        if kind == "text":
            out.append(_text_md(token[1], wide, line_start and n == 0, after))
        elif kind in markers:
            mark = markers[kind]
            # An italic inside an italic takes the other marker, or the inner one would close the outer.
            inner = _ITALICS[_ITALICS.index(italic) - 1] if kind == "i" else italic
            out.append(mark + _inline_md(token[1], wide, inner, following=mark[0]) + mark)
        elif kind == "code":
            out.append(f"`{token[1]}`")
        elif kind == "a":
            out.append(f"[{token[2]}]({token[1]})")
        elif kind == "mention":
            out.append("@everyone" if token[1] else f"@{token[3]}")
        else:
            out.append(f":{token[1]}:")
    return "".join(reversed(out))


def _markdown(blocks: list[Block], wide: bool, italic: str) -> str:
    out: list[str] = []
    prev = ""
    for kind, body in blocks:
        if kind == prev and kind != "line" and kind != "pre":
            out.append("")  # two lists (or quotes) in a row would read as one
        if kind == "line":
            line = _inline_md(body, wide, italic, line_start=True)
            # A leading `- `, `> `, `1. ` would start a block: the backslash keeps the line text.
            out.append("\\" + line if _literal("\\" + line) != "\\" + line else line)
        elif kind == "blockquote":
            out += (f"> {_inline_md(item, wide, italic)}" for item in body)
        elif kind == "ul":
            out += (f"- {_inline_md(item, wide, italic)}" for item in body)
        elif kind == "ol":
            out += (f"{n}. {_inline_md(item, wide, italic)}" for n, item in enumerate(body, 1))
        else:
            out.append(f"```\n{body}\n```")
        prev = kind
    return "\n".join(out)


def _picks(blocks: list[Block]) -> list[dict[str, Any]]:
    picks: list[dict[str, Any]] = []

    def walk(tokens: tuple[Token, ...]) -> None:
        for token in tokens:
            if token[0] == "mention":
                everyone, mri, label = token[1:]
                picks.append(
                    {"name": "everyone", "mri": mri, "everyone": True}
                    if everyone
                    else {"name": label, "mri": mri}
                )
            elif token[0] in _MARKERS:
                walk(token[1])

    for kind, body in blocks:
        if kind == "line":
            walk(body)
        elif kind != "pre":
            for item in body:
                walk(item)
    return picks


def _skip(content: str, pos: int) -> int:
    """Past whitespace and the empty paragraphs older Teams clients put before a Reply block."""
    while True:
        while pos < len(content) and content[pos].isspace():
            pos += 1
        if not content.startswith("<p></p>", pos):
            return pos
        pos += len("<p></p>")


def _quotes(content: str) -> tuple[list[Quote], int]:
    """The Reply blocks leading `content`, verbatim, and where its own text starts."""
    quotes: list[Quote] = []
    pos = 0
    while True:
        start = _skip(content, pos)
        if not content.startswith("<blockquote", start):
            break
        head_end = content.find(">", start)
        close = content.find("</blockquote>", head_end)
        if head_end < 0 or close < 0 or _REPLY not in content[start:head_end]:
            break
        pos = close + len("</blockquote>")
        block = content[start:pos]
        key, name, line = _QuoteText.read(block)
        if not _QUOTE_KEY_RE.fullmatch(key):
            raise _Refused(OTHER)
        quotes.append(Quote(block, key, name, line))
    return quotes, pos


class _QuoteText(HTMLParser):
    """A Reply block's author and preview line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: dict[str, list[str]] = {"mri": [], "preview": []}
        self.into: tuple[str, list[str]] | None = None
        self.key: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.key is None:  # the block's own tag
            self.key = dict(attrs).get("itemid") or ""
        prop = dict(attrs).get("itemprop") or ""
        if prop in self.parts:
            self.into = (tag, self.parts[prop])

    def handle_endtag(self, tag: str) -> None:
        if self.into and self.into[0] == tag:
            self.into = None

    def handle_data(self, data: str) -> None:
        if self.into:
            self.into[1].append(data)

    @classmethod
    def read(cls, block: str) -> tuple[str, str, str]:
        """(quoted message id, author, preview)."""
        parser = cls()
        parser.feed(block)
        parser.close()
        name, line = (" ".join("".join(parser.parts[k]).split()) for k in ("mri", "preview"))
        return parser.key or "", name, line


def decompose(message: dict[str, Any], emojis: dict[str, str]) -> Editable | str:
    """One of my messages as the editor opens it, or what keeps it from being edited here.

    `emojis`: custom name → AMS object id, as `compose_html` takes it."""
    props = message.get("properties")
    props = props if isinstance(props, dict) else {}
    msgtype, content = str(message.get("messagetype") or ""), str(message.get("content") or "")
    if msgtype == "RichText/Media_Card" or json_list(props.get("cards")):
        return "a card"
    if json_list(props.get("files")):
        return "an attached file"
    if msgtype not in TEXT_TYPES:
        return OTHER
    if msgtype == "Text":
        content = html.escape(content).replace("\n", "<br>")
    if len(content.encode()) > MAX_HTML_BYTES:  # more than Teams takes in a send: not one miniteams wrote
        return "too long"
    try:
        quotes, start = _quotes(content)
        blocks, images = _read(content[start:], props, emojis)
        picks = _picks(blocks)
        for wide in (False, True):
            for italic in _ITALICS:
                text = _markdown(blocks, wide, italic)
                try:
                    composed, composed_props = compose_html(text, picks, emojis, full=True)
                except ValueError:
                    return "too long"
                if _read(composed, composed_props or {}, emojis) == (blocks, []):
                    return Editable(text, picks, quotes, images)
    except _Refused as exc:
        return str(exc)
    return OTHER
