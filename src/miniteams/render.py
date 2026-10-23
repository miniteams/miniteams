"""Teams message HTML → the safe subset the chat page renders (spec 011).

Allowlist, never denylist: a tag or attribute not named here is dropped and its text kept.
"""

import base64
import binascii
import html
import json
import re
from collections import Counter
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .attachments import LIVE_HOST_RE, LIVE_VIEW_RE, Giphy, existing, giphy, object_id
from .compose import _URL_RE, trim_url
from .messages import file_name, json_list, reaction_emoji

_KEPT = frozenset(
    (
        "p br hr div h1 h2 h3 b strong i em u s del code pre blockquote ul ol li a img span table tr td th"
    ).split()
)
_VOID = frozenset({"br", "hr", "img"})
_DROP_CONTENT = frozenset({"script", "style", "iframe"})
_BLOCKS = _KEPT - frozenset("a b strong i em u s del code span img".split())
_HTML_MAX = 128 * 1024  # chars, well past a real Teams message body
_IMG_ATTRS = ("alt", "title")
STEM_RE = re.compile(r"[\w-][\w.-]*")  # no glob characters, no leading dot
_NOT_ARCHIVED = "[image not archived]"
# Where Teams keeps message objects (regional asm hosts included); attachments' cookie list is narrower.
_TEAMS_MEDIA_RE = re.compile(r"(?:[\w-]+\.)*(?:asm\.skype\.com|asyncgw\.teams\.microsoft\.com)")


def _web_url(value: str) -> str | None:
    """`value` when it is an http(s) URL; entities are already decoded by the parser."""
    url = value.strip()
    try:
        scheme = urlsplit(url).scheme
    except ValueError:  # "https://[x": a bad bracket host must not sink the whole message
        return None
    return url if scheme.lower() in ("http", "https") else None


def _teams_object(url: str) -> bool:
    parts = urlsplit(url)
    return "/objects/" in parts.path and bool(_TEAMS_MEDIA_RE.fullmatch(parts.hostname or ""))


def media_url(thread_id: str, media_dir: Path | None, src: str, live: bool = True) -> str | None:
    """`/media/<thread>/<file>` when the archive holds the object `src` points to; with `live`, for an
    asyncgw object it does not hold (yet), `/media/<thread>/<id>?host=<host>`, found or fetched by the
    server on request; else None."""
    stem = object_id(src)
    if "/objects/" not in src or not STEM_RE.fullmatch(stem):
        return None
    thread = quote(thread_id, safe="")
    found = existing(media_dir, stem) if media_dir is not None else None
    if found is not None:
        return f"/media/{thread}/{quote(Path(found).name, safe='')}"
    if not live:
        return None
    try:
        parts = urlsplit(src)
    except ValueError:
        return None
    if parts.scheme != "https" or not LIVE_HOST_RE.fullmatch(parts.netloc):
        return None
    # A pasted GIF's message names its animated view: `imgo`, the server's default, is a still.
    view = parts.path.rpartition("/views/")[2]
    named = f"&view={view}" if view != "imgo" and LIVE_VIEW_RE.fullmatch(view) else ""
    return f"/media/{thread}/{quote(stem, safe='')}?host={parts.netloc}{named}"


def giphy_media_url(thread_id: str, media_dir: Path | None, gif: Giphy) -> str:
    """`/media/<thread>/<file>` of the archived copy, else `/media/<thread>/giphy-<id>?giphy=<host><path>`,
    which the web fetches from Giphy (spec 031)."""
    thread = quote(thread_id, safe="")
    found = existing(media_dir, gif.stem) if media_dir is not None else None
    if found is not None:
        return f"/media/{thread}/{quote(Path(found).name, safe='')}"
    return f"/media/{thread}/{gif.stem}?giphy={quote(gif.url.removeprefix('https://'), safe='/')}"


def _attr(name: str, value: str) -> str:
    return f' {name}="{html.escape(value)}"'


class _Sanitizer(HTMLParser):
    def __init__(
        self,
        thread_id: str,
        media_dir: Path | None,
        me: str,
        mentions: dict[str, str],
        custom: frozenset[str] = frozenset(),
        names: dict[str, str] | None = None,
        named: set[str] | None = None,
        titles: dict[str, str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.thread_id, self.media_dir, self.me, self.mentions = thread_id, media_dir, me, mentions
        self.custom, self.titles = custom, titles or {}
        self.names, self.named = names or {}, named if named is not None else set()
        # (open-stack index, output length) of a link renamed after a file: its own content is cut at its end.
        self.renamed: tuple[int, int] | None = None
        self.out: list[str] = []
        # (tag, emitted): dropped spans/links stay on the stack so their end tag closes nothing else.
        self.open: list[tuple[str, bool]] = []
        # Lets an unmatched end tag return in O(1) instead of scanning the stack (CWE-407).
        self.counts: Counter[str] = Counter()
        self.hidden = 0  # depth inside script/style/iframe

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self.hidden += 1
            return
        if self.hidden:
            return
        if tag in _BLOCKS:  # a renamed link left open must not swallow the rest of the message
            while self.renamed is not None:
                self._close()
        a = {k: v or "" for k, v in attrs}
        itemtype = a.get("itemtype", "")
        if tag == "emoji" or itemtype.endswith("/Emoji"):
            if tag == "span":
                self._push(tag, False)  # its text is the character
            elif tag in ("img", "emoji"):
                # A few come without `alt`: the id names the emoticon.
                emoticon = a.get("itemid") or a.get("id", "")
                self.out.append(html.escape(a.get("alt") or (reaction_emoji(emoticon) if emoticon else "")))
            return
        if tag == "img":
            self._img(a, emoji=itemtype.endswith("/CustomEmoji"))
            return
        if tag not in _KEPT:
            return
        if tag in _VOID:
            self.out.append(f"<{tag}>")
            return
        extra = ""
        emitted = True
        if tag == "a":
            href = _web_url(a.get("href", ""))
            emitted = href is not None
            if href is not None:
                extra = _attr("href", href) + ' rel="noopener" target="_blank"'
                key = _link_key(href) if self.names and self.renamed is None else ""
                if key and key in self.names:  # 027-AC6: a pasted file link reads as its name
                    self.named.add(key)
                    self._push(tag, True)
                    self.out.append(f"<a{extra}>{html.escape(self.names[key])}")
                    self.renamed = (len(self.open) - 1, len(self.out))
                    return
        elif tag == "span":
            emitted = itemtype.endswith("/Mention")
            if emitted:
                mri = self.mentions.get(a.get("itemid", ""), "")
                cls = "mention mention-me" if mri and mri == self.me else "mention"
                extra = _attr("class", cls) + (_attr("data-mri", mri) if mri else "")
                extra += _attr("title", self.titles[mri]) if mri in self.titles else ""
        elif tag == "blockquote" and itemtype.endswith("/Reply"):
            mid = a.get("itemid", "")
            # Digits only: the page reads the quoted message's time from its id (spec 021).
            extra = ' class="reply"' + (_attr("data-msg", mid) if mid.isascii() and mid.isdigit() else "")
        self._push(tag, emitted)
        if emitted:
            self.out.append(f"<{tag}{extra}>")

    def _img(self, a: dict[str, str], emoji: bool) -> None:
        src = a.get("src", "")
        gif = None if emoji else giphy(src)
        local = (
            giphy_media_url(self.thread_id, self.media_dir, gif)
            if gif
            else media_url(self.thread_id, self.media_dir, src, live=not emoji)
        )
        name, _, tail = a.get("itemid", "").partition(";")
        # The itemid's tail need not be the image id, but it is all a `src`-less one has.
        oid = next((o for o in (object_id(src), tail) if o in self.custom), None) if emoji else None
        if local is None and oid is not None:
            local = f"/emoji/{oid}"  # sent before the archiver fetched it: served from the emoji cache
        if local is None:
            if emoji:  # no glyph to fall back on: its name, styled apart from the text
                self.out.append(f'<span class="custom-emoji">{html.escape(name or a.get("alt", ""))}</span>')
                return
            url = _web_url(src)
            if url is None or _teams_object(url):  # Teams' copy needs the token the page lacks: no dead link
                self.out.append(_NOT_ARCHIVED)
                return
            alt = a.get("alt", "").strip()
            text = html.escape(f"[image: {alt}]" if alt else _NOT_ARCHIVED)
            # The CSP keeps external images off the page; an anchor in an anchor would split the outer one.
            self.out.append(
                text
                if ("a", True) in self.open  # a dropped anchor (javascript:) is no link to nest in
                else f'<a{_attr("href", url)} rel="noopener" target="_blank">{text}</a>'
            )
            return
        attrs = _attr("src", local) + (' class="emoji"' if emoji else "")
        attrs += _attr("data-giphy", gif.url) if gif is not None else ""  # the page's last source (031-AC4)
        attrs += "".join(_attr(k, a[k]) for k in _IMG_ATTRS if k in a)
        attrs += "".join(_attr(k, a[k]) for k in ("width", "height") if a.get(k, "").isdigit())
        self.out.append(f"<img{attrs}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and tag not in _DROP_CONTENT:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_CONTENT:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden or not self.counts[tag]:
            return
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                while len(self.open) > i:
                    self._close()
                return

    def _push(self, tag: str, emitted: bool) -> None:
        self.open.append((tag, emitted))
        self.counts[tag] += 1

    def _close(self) -> None:
        tag, emitted = self.open.pop()
        self.counts[tag] -= 1
        if self.renamed is not None and self.renamed[0] == len(self.open):
            del self.out[self.renamed[1] :]
            self.renamed = None
        if emitted:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.counts["a"] or self.counts["code"] or self.counts["pre"]:  # spec 028: no autolink in these
            self.out.append(html.escape(data, quote=False))
            return
        pos = 0
        for m in _URL_RE.finditer(data):  # spec 028: a bare URL links as compose would send it
            if url := trim_url(m.group()):
                self.out += (
                    html.escape(data[pos : m.start()], quote=False),
                    _link(url, html.escape(url, quote=False)),
                )
                pos = m.start() + len(url)
        self.out.append(html.escape(data[pos:], quote=False))

    def result(self) -> str:
        self.close()
        while self.open:
            self._close()
        return "".join(self.out)


def sanitize(
    content: str,
    thread_id: str,
    media_dir: Path | None = None,
    me: str = "",
    mentions: dict[str, str] | None = None,
    custom: frozenset[str] = frozenset(),
    names: dict[str, str] | None = None,
    named: set[str] | None = None,
    titles: dict[str, str] | None = None,
) -> str:
    """Teams HTML → allowlisted HTML; `mentions` maps a Mention span's `itemid` to its MRI, `custom`
    holds the custom emoji object ids `/emoji/<id>` serves. A link whose `_link_key` is in `names` reads as
    that file name instead of its content; its key is added to `named`. `titles`: MRI → the full name a
    mention of that person shows on hover (spec 033).

    Input past `_HTML_MAX` chars is cut off: the parser's cost must stay bounded per message."""
    parser = _Sanitizer(thread_id, media_dir, me, mentions or {}, custom, names, named, titles)
    parser.feed(content[:_HTML_MAX])
    return parser.result()


# Adaptive Cards (spec 011 "Cards"): rendered from the card JSON, every string escaped here, so
# no tag or attribute below can come from the payload.
_MD_MAX = 10_000
_CARD_DEPTH = 8
_CARD_NODES = 300
_MSG_CARDS = 20
_MSG_MD = 50_000
_CODESNIPPET = "application/vnd.microsoft.card.codesnippet"
# Bounded inner spans keep every pattern linear on hostile text (no nested quantifiers).
_MD_INLINE = re.compile(
    # Labels may hold one bracket pair, as GitLab's "[repo [!123]](url)"; each branch consumes a
    # character or a whole pair, so the scan stays linear.
    r"\[((?:[^\[\]\n]|\[[^\[\]\n]{0,100}\]){1,500})\]\(([^()\[\]\s]{1,2000})\)"
    r"|\*\*([^*\n]{1,500})\*\*"
    r"|(?<![\w_])_([^_\n]{1,500})_(?![\w_])"
)
_MD_BULLET = re.compile(r"[-*] (.*)")
_MD_NUMBER = re.compile(r"\d{1,3}\. (.*)")
_SWIFT_RE = re.compile(r'<Swift\b[^>]{0,200}?\bb64\s*=\s*(["\'])(.*?)\1', re.I | re.S)
_SIZES = {"small": "s-small", "medium": "s-medium", "large": "s-large", "extralarge": "s-large"}
_CHILDREN = {"Container": "items", "Column": "items", "ImageSet": "images"}
MediaUrl = Callable[[str], str | None]


def _link(url: object, inner: str, cls: str = "") -> str:
    """`inner` inside an http(s) link, or `inner` alone for any other URL."""
    href = _web_url(url) if isinstance(url, str) else None
    if href is None:
        return inner
    extra = _attr("class", cls) if cls else ""
    return f'<a{extra}{_attr("href", href)} rel="noopener" target="_blank">{inner}</a>'


def _md_inline(text: str) -> str:
    out, pos = [], 0
    for m in _MD_INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        label, url, bold, italic = m.groups()
        if label is not None:
            out.append(_link(url, html.escape(label)))
        elif bold is not None:
            out.append(f"<b>{html.escape(bold)}</b>")
        else:
            out.append(f"<i>{html.escape(italic)}</i>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def card_markdown_html(text: str) -> str:
    """The Teams card markdown subset: **bold**, _italic_, [text](url), `- ` and `1. ` lists."""
    out: list[str] = []
    lines: list[str] = []  # pending plain lines, joined by <br>
    list_tag = ""
    for raw in text[:_MD_MAX].splitlines():
        line = raw.strip()
        item = _MD_BULLET.fullmatch(line) or _MD_NUMBER.fullmatch(line)
        tag = ("ul" if item.re is _MD_BULLET else "ol") if item else ""
        if tag != list_tag:
            if list_tag:
                out.append(f"</{list_tag}>")
            if tag:
                out.append("<br>".join(lines))
                lines = []
                out.append(f"<{tag}>")
            list_tag = tag
        if item:
            out.append(f"<li>{_md_inline(item.group(1))}</li>")
        else:
            lines.append(_md_inline(line))
    if list_tag:
        out.append(f"</{list_tag}>")
    out.append("<br>".join(lines))
    return "".join(out)


class _CardRenderer:
    def __init__(self, media: MediaUrl | None) -> None:
        self.media = media
        self.nodes = 0
        self.header_row = False  # per card
        self.md_left = _MSG_MD  # per message

    def md(self, text: str) -> str:
        text = text[: min(_MD_MAX, self.md_left)]
        self.md_left -= len(text)
        return card_markdown_html(text)

    def tb(self, cls: str, text: str) -> str:
        """A text block, or nothing once the markdown budget is spent: an empty div would still
        count as rendered content and hide the card's fallback."""
        inner = self.md(text)
        return f'<div class="{cls}">{inner}</div>' if inner else ""

    def many(self, items: object, depth: int, fn: Callable[[dict[str, Any], int], str]) -> str:
        if not isinstance(items, list) or depth > _CARD_DEPTH:
            return ""
        out = []
        for item in items:
            if self.nodes >= _CARD_NODES:
                break
            self.nodes += 1
            if isinstance(item, dict):
                out.append(fn(item, depth))
        return "".join(out)

    def card(self, card: dict[str, Any], depth: int = 0, content_type: str = "") -> str:
        title = _str(card.get("title"))
        head = f'<div class="tb w-bolder">{html.escape(title)}</div>' if title else ""
        if content_type == _CODESNIPPET:
            code = _str(card.get("text"))[:_MD_MAX]
            return head + (f"<pre><code>{html.escape(code)}</code></pre>" if code else "")
        if card.get("type") != "AdaptiveCard" and "body" not in card:
            return self.legacy(card, head, depth)
        body = self.many(card.get("body"), depth + 1, self.element)
        acts = self.many(card.get("actions"), depth + 1, self.action)
        return body + (f'<div class="acts">{acts}</div>' if acts else "")

    def legacy(self, card: dict[str, Any], head: str, depth: int) -> str:
        """Hero/thumbnail/O365 MessageCard: title, markdown text, MessageCard sections."""
        text = _str(card.get("text"))
        out = head + (self.tb("tb", text) if text else "")
        return out + self.many(card.get("sections"), depth + 1, self.section)

    def section(self, sec: dict[str, Any], depth: int) -> str:
        out = ""
        if title := _str(sec.get("activityTitle")):
            out += self.tb("tb w-bolder", title)
        if text := _str(sec.get("text")):
            out += self.tb("tb", text)
        rows = self.many(sec.get("facts"), depth + 1, self.fact)
        return out + (f'<table class="facts">{rows}</table>' if rows else "")

    def table_row(self, row: dict[str, Any], depth: int) -> str:
        tag = "th" if self.header_row else "td"
        self.header_row = False  # only the first row of a table heads it
        cells = "".join(
            f"<{tag}>{self.many(cell.get('items'), depth + 1, self.element)}</{tag}>"
            for cell in (row.get("cells") or [])
            if isinstance(cell, dict)
        )
        return f"<tr>{cells}</tr>" if cells else ""

    def fact(self, fact: dict[str, Any], depth: int) -> str:
        title = html.escape(_str(fact.get("title")) or _str(fact.get("name")))
        return f"<tr><th>{title}</th><td>{self.md(_str(fact.get('value')))}</td></tr>"

    def element(self, el: dict[str, Any], depth: int) -> str:
        kind = el.get("type")
        if _is_toggle(el) and kind in (
            "TextBlock",
            "RichTextBlock",
        ):  # "Show details…": nothing to toggle here
            return ""
        if kind == "TextBlock":
            text = _str(el.get("text"))
            if not text:
                return ""
            cls = ["tb"]
            if _str(el.get("weight")).lower() == "bolder":
                cls.append("w-bolder")
            if size := _SIZES.get(_str(el.get("size")).lower()):
                cls.append(size)
            if el.get("isSubtle") is True:
                cls.append("subtle")
            # div, not p: the markdown may hold a list, which a <p> cannot contain.
            return self.tb(" ".join(cls), text)
        if kind == "RichTextBlock":
            runs = self.many(el.get("inlines"), depth + 1, self.run)
            return f"<p>{runs}</p>" if runs else ""
        if kind == "ColumnSet":
            cols = self.many(el.get("columns"), depth + 1, self.element)
            return f'<div class="cols">{cols}</div>' if cols else ""
        if isinstance(kind, str) and kind in _CHILDREN:
            inner = self.many(el.get(_CHILDREN[kind]), depth + 1, self.element)
            if not inner:
                return ""
            box = f'<div class="{"col" if kind == "Column" else "ct"}">{inner}</div>'
            # A link around links would nest <a>, which the browser splits apart.
            return box if "<a " in inner else _link(_open_url(el.get("selectAction")), box)
        if kind == "FactSet":
            rows = self.many(el.get("facts"), depth + 1, self.fact)
            return f'<table class="facts">{rows}</table>' if rows else ""
        if kind == "Table":  # Adaptive Cards 1.5 table: bots put their facts here nowadays
            self.header_row = el.get("firstRowAsHeaders") is True
            rows = self.many(el.get("rows"), depth + 1, self.table_row)
            return f'<table class="facts">{rows}</table>' if rows else ""
        if kind == "Image":
            img = self.image(el)
            return _link(_open_url(el.get("selectAction")), img) if img else ""
        if kind == "ActionSet":
            acts = self.many(el.get("actions"), depth + 1, self.action)
            return f'<div class="acts">{acts}</div>' if acts else ""
        for key in ("text", "title", "label", "placeholder"):
            if text := _str(el.get(key)):
                return f"<p>{html.escape(text)}</p>"
        return ""

    def run(self, run: dict[str, Any], depth: int) -> str:
        text = html.escape(_str(run.get("text"))).replace("\n", "<br>")
        if not text or _is_toggle(run):
            return ""
        if _str(run.get("weight")).lower() == "bolder":
            text = f"<b>{text}</b>"
        if run.get("italic") is True:
            text = f"<i>{text}</i>"
        if run.get("strikethrough") is True:
            text = f"<s>{text}</s>"
        return _link(_open_url(run.get("selectAction")), text)

    def image(self, el: dict[str, Any]) -> str:
        alt = _str(el.get("altText"))
        url = _str(el.get("url"))
        local = self.media(url) if self.media and url else None
        if local and local.startswith("/media/"):
            return f"<img{_attr('src', local)}{_attr('alt', alt)}>"
        return f"<p>{html.escape(alt)}</p>" if alt else ""

    def action(self, act: dict[str, Any], depth: int) -> str:
        title = html.escape(_str(act.get("title")))
        kind = act.get("type")
        if kind == "Action.ShowCard" and isinstance(act.get("card"), dict):
            inner = self.card(act["card"], depth)
            return f"<details><summary>{title}</summary>{inner}</details>" if title or inner else ""
        if not title or kind == "Action.ToggleVisibility":  # a read-only view shows all; no toggle
            return ""
        url = act.get("url") if kind == "Action.OpenUrl" else None
        return _link(url, title, "act") if _web_url(_str(url)) else f'<span class="act">{title}</span>'


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_toggle(el: dict[str, Any]) -> bool:
    action = el.get("selectAction")
    return isinstance(action, dict) and action.get("type") == "Action.ToggleVisibility"


def _open_url(action: object) -> object:
    return action.get("url") if isinstance(action, dict) and action.get("type") == "Action.OpenUrl" else None


def card_html(card: dict[str, Any], media_url: MediaUrl | None = None, content_type: str = "") -> str:
    """One card content dict (its attachment's `content_type`) → `div.card`; `media_url` maps an
    image URL to its archived `/media/...` path, anything else renders the alt text."""
    return f'<div class="card">{_CardRenderer(media_url).card(card, content_type=content_type)}</div>'


_FILE_ICONS = {
    ext: icon
    for icon, exts in (
        ("📝", "doc docx docm dot dotx odt rtf"),
        ("📊", "xls xlsx xlsm xlsb csv ods"),
        ("📽", "ppt pptx pptm pps ppsx odp"),
        ("📕", "pdf"),
        ("🖼", "png jpg jpeg gif bmp svg webp heic tif tiff"),
        ("🗜", "zip 7z rar tar gz tgz bz2 xz"),
        ("📃", "txt md log json xml yaml yml"),
        ("🎞", "mp4 mov avi mkv webm wmv m4v"),
    )
    for ext in exts.split()
}


_URL_BAD = re.compile(r"[\x00-\x20\x7f-\x9f\s]")


def _link_key(url: str) -> str:
    """A link's identity: its query sorted, without the `e=…` a pasted sharing link carries, the fragment or
    a trailing slash."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    # Only `e` goes: `Doc.aspx?sourcedoc=…`, `AllItems.aspx?id=…` name the file in their query.
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "e"))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def _file_entries(files: object) -> list[tuple[dict[str, Any], str, list[str]]]:
    """(entry, name, [shareUrl, objectUrl]) per `properties.files` entry (JSON string or list)."""
    out = []
    for entry in json_list(files):
        if isinstance(entry, dict):
            info = entry.get("fileInfo")
            share = _str(info.get("shareUrl")) if isinstance(info, dict) else ""
            name = file_name(entry)
            out.append((entry, name, [share, _str(entry.get("objectUrl"))]))
    return out


def _file_url(urls: list[str]) -> str | None:
    """The first https URL of `urls` (027-AC2: the sharing link, then objectUrl)."""
    for url in urls:
        # The raw string is what lands in href: gate it, not urlsplit's cleaned-up reading of it.
        if url[:8].lower() == "https://" and not _URL_BAD.search(url) and _web_url(url) is not None:
            return url
    return None


def file_links(files: object) -> dict[str, str]:
    """`reference` entries (pasted links, 027-AC6) as {`_link_key` of each URL: file name}, for sanitize."""
    return {
        _link_key(url): name
        for entry, name, urls in _file_entries(files)
        if entry.get("state") == "reference"
        for url in urls
        if url
    }


def file_urls(files: object) -> list[str]:
    """Every URL of the `properties.files` entries: the keys of their archived copies (027-AC7)."""
    return [url for _, _, urls in _file_entries(files) for url in urls if url]


def files_html(
    files: object,
    named: set[str] | frozenset[str] = frozenset(),
    local: Callable[[list[str]], str | None] | None = None,
) -> str:
    """`properties.files` as one card per entry, type icon then name (027-AC1), linked to the archived copy
    of its URLs `local(urls)` (027-AC5, AC7), else its sharing link, else its objectUrl, in a new tab;
    unlinked without any (027-AC2). A `reference` entry is a link named after the file instead, or nothing
    when the body already links it (a key in `named`, 027-AC6)."""
    out = []
    for entry, name, urls in _file_entries(files):
        url = _file_url(urls)
        if local and entry.get("state") != "reference":
            url = local(urls) or url
        link = f'{_attr("href", url)} rel="noopener" target="_blank"' if url else ""
        if entry.get("state") == "reference":
            if not any(u and _link_key(u) in named for u in urls):
                out.append(
                    f"<p><a{link}>{html.escape(name)}</a></p>" if url else f"<p>{html.escape(name)}</p>"
                )
            continue
        kind = (_str(entry.get("fileType")) or _str(entry.get("type")) or name.rpartition(".")[2]).lower()
        # A space, not a gap: a quote takes the card's first text line, icon and name together.
        icon = f'<span class="file-icon">{_FILE_ICONS.get(kind, "📎")}</span>'
        inner = f'{icon} <span class="file-name">{html.escape(name)}</span>'
        out.append(f'<a class="file"{link}>{inner}</a>' if url else f'<span class="file">{inner}</span>')
    return "".join(out)


def _json(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError, RecursionError:
        return None


def card_from_message(message: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The cards a message carries, as `{"content", "type", "app", "fallback"}`; None when none.

    `Media_Card` puts them base64 in `<Swift b64>`, `RichText/Html` in `properties.cards`."""
    found: list[dict[str, Any]] = []
    swift = _SWIFT_RE.search(_str(message.get("content")))
    if swift:
        try:
            data = _json(base64.b64decode(html.unescape(swift.group(2))).decode("utf-8"))
        except binascii.Error, ValueError:
            data = None
        if isinstance(data, dict):
            summary = _str(data.get("summary"))
            atts = data.get("attachments")
            found += (
                [{**a, "summary": summary} for a in atts if isinstance(a, dict)]
                if isinstance(atts, list)
                else []
            )
            if not found and summary:
                found.append({"summary": summary})
    props = message.get("properties")
    cards = _json(props.get("cards")) if isinstance(props, dict) else None
    if isinstance(cards, list):
        found += [c for c in cards if isinstance(c, dict)]
    out = []
    for att in found:
        content = att.get("content")
        content = content if isinstance(content, dict) else {}
        fallback = _str(att.get("summary")) or _str(content.get("fallbackText"))
        kind = _str(att.get("contentType"))
        out.append({"content": content, "type": kind, "app": _str(att.get("appName")), "fallback": fallback})
    return out or None


def cards_html(message: dict[str, Any], media_url: MediaUrl | None = None) -> str:
    """Every card of `message` rendered, its app name on top; a body that renders nothing shows
    the card's summary or fallbackText instead."""
    out = []
    renderer = _CardRenderer(media_url)
    for card in (card_from_message(message) or [])[:_MSG_CARDS]:
        renderer.nodes = 0
        body = renderer.card(card["content"], content_type=card["type"])
        if not body and card["fallback"]:
            body = f'<p class="card-fallback">{html.escape(card["fallback"])}</p>'
        if body:
            app = f'<div class="card-app">{html.escape(card["app"])}</div>' if card["app"] else ""
            out.append(f'<div class="card">{app}{body}</div>')
    return "".join(out)
