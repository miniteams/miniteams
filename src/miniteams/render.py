"""Teams message HTML → the safe subset the chat page renders (spec 011).

Allowlist, never denylist: a tag or attribute not named here is dropped and its text kept.
"""

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

from .attachments import _existing, _object_id

_KEPT = frozenset(
    (
        "p br hr div h1 h2 h3 b strong i em u s del code pre blockquote ul ol li a img span table tr td th"
    ).split()
)
_VOID = frozenset({"br", "hr", "img"})
_DROP_CONTENT = frozenset({"script", "style", "iframe"})
_IMG_ATTRS = ("alt", "title")
_STEM_RE = re.compile(r"[\w-][\w.-]*")  # no glob characters, no leading dot
_NOT_ARCHIVED = "[image not archived]"


def _web_url(value: str) -> str | None:
    """`value` when it is an http(s) URL; entities are already decoded by the parser."""
    url = value.strip()
    return url if urlsplit(url).scheme.lower() in ("http", "https") else None


def media_url(thread_id: str, media_dir: Path | None, src: str) -> str | None:
    """`/media/<thread>/<file>` when the archive holds the object `src` points to, else None."""
    stem = _object_id(src)
    if media_dir is None or "/objects/" not in src or not _STEM_RE.fullmatch(stem):
        return None
    found = _existing(media_dir, stem)
    if found is None:
        return None
    return f"/media/{quote(thread_id, safe='')}/{quote(Path(found).name, safe='')}"


def _attr(name: str, value: str) -> str:
    return f' {name}="{html.escape(value)}"'


class _Sanitizer(HTMLParser):
    def __init__(self, thread_id: str, media_dir: Path | None, me: str, mentions: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self.thread_id, self.media_dir, self.me, self.mentions = thread_id, media_dir, me, mentions
        self.out: list[str] = []
        # (tag, emitted): dropped spans/links stay on the stack so their end tag closes nothing else.
        self.open: list[tuple[str, bool]] = []
        self.hidden = 0  # depth inside script/style/iframe

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_CONTENT:
            self.hidden += 1
            return
        if self.hidden:
            return
        a = {k: v or "" for k, v in attrs}
        itemtype = a.get("itemtype", "")
        if tag == "emoji" or itemtype.endswith("/Emoji"):
            if tag == "span":
                self.open.append((tag, False))  # its text is the character
            elif tag in ("img", "emoji"):
                self.out.append(html.escape(a.get("alt", "")))
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
        elif tag == "span":
            emitted = itemtype.endswith("/Mention")
            if emitted:
                mri = self.mentions.get(a.get("itemid", ""), "")
                cls = "mention mention-me" if mri and mri == self.me else "mention"
                extra = _attr("class", cls) + (_attr("data-mri", mri) if mri else "")
        elif tag == "blockquote" and itemtype.endswith("/Reply"):
            extra = ' class="reply"'
        self.open.append((tag, emitted))
        if emitted:
            self.out.append(f"<{tag}{extra}>")

    def _img(self, a: dict[str, str], emoji: bool) -> None:
        src = a.get("src", "")
        local = media_url(self.thread_id, self.media_dir, src)
        if local is None:
            if emoji:
                self.out.append(html.escape(a.get("alt", "")))
                return
            url = _web_url(src)
            if url is None:
                self.out.append(_NOT_ARCHIVED)
            else:
                self.out.append(
                    f'<a href="{html.escape(url)}" rel="noopener" target="_blank">{_NOT_ARCHIVED}</a>'
                )
            return
        attrs = _attr("src", local) + (' class="emoji"' if emoji else "")
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
        if self.hidden:
            return
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                while len(self.open) > i:
                    self._close()
                return

    def _close(self) -> None:
        tag, emitted = self.open.pop()
        if emitted:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.out.append(html.escape(data, quote=False))

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
) -> str:
    """Teams HTML → allowlisted HTML; `mentions` maps a Mention span's `itemid` to its MRI."""
    parser = _Sanitizer(thread_id, media_dir, me, mentions or {})
    parser.feed(content)
    return parser.result()
