"""Composer text → Teams message HTML: a markdown subset, `:shortcode:` emojis, `@Name` mentions.

Pure (spec 012). No backtracking regex: closers are found with `str.find`.
"""

import html
import itertools
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .chats import chat_kind
from .config import Settings
from .directory import humans
from .emoji_table import SHORTCODES, TEAMS_NAMES
from .messages import fold, is_me, snippet
from .send import MAX_HTML_BYTES, NOTES_THREAD, too_long

MAX_TEXT = 28 * 1024  # composer characters; send.too_long caps the posted HTML bytes
_ASYNCGW_HOST: str = Settings.model_fields["asyncgw_host"].default
EMOJI_NAME_RE = re.compile(r"[A-Za-z0-9_+-]{1,64}")  # what `:name:` expands

_MENTION = "http://schema.skype.com/Mention"
_CUSTOM_EMOJI = "http://schema.skype.com/CustomEmoji"
_SPECIAL_RE = re.compile(r"[\\`\[*_~]|https?://")
ESCAPABLE = frozenset("\\*_~`[@")  # `\x` gives a literal x; `\\` only where a lone `\` would escape
_RUN_RE = re.compile(r"\*+|_+|~+")
# `'` only before a word character: `Hitchhiker's_Guide`, not `'https://a.com/x'|jq`.
_URL_RE = re.compile(r"https?://(?:[^\s<>\"`']|'(?=\w))+")
# Sentence punctuation after a URL; `-` stays (spec 028). `'"<>` never end a _URL_RE match.
_URL_TRAIL = frozenset(".,:;!?*_~…–—«»‹›“”‘’")
_URL_CLOSERS = {")": "(", "]": "[", "}": "{"}
_URL_HOST_RE = re.compile(r"https?://[^/?#']*")
_ORDERED_RE = re.compile(r"\d{1,9}\. ")
# The trailing `:` is a lookahead so an unknown name gives it back: `10:30:fire:` still finds `:fire:`;
# never a URL's `:` (`URL:https://…`).
_EMOJI = rf":(?P<emoji>{EMOJI_NAME_RE.pattern})(?=:(?!//))"
_PREVIEW_LEN = 100  # characters of the quoted message a Reply block carries
_SPANS = (("**", "b"), ("~~", "s"), ("*", "i"), ("_", "i"))
MAX_MENTIONS = 50
MAX_NAME = 200
# An `@` compose_html could expand: starting a word, followed by one; URLs matched first to skip theirs.
_CANDIDATE_RE = re.compile(rf"(?P<url>{_URL_RE.pattern})|(?<!\w)@(?=\w)")
# What continues a word, so a name cannot end before it: `\w` and the marks (`re` has no \p{M}; ui.js's
# `scan` reads the same `[\p{L}\p{N}_\p{M}]`). Planes 0, 1 and 14 hold every mark.
_MARKS = "".join(
    chr(c)
    for c in itertools.chain(range(0x20000), range(0xE0000, 0xE1000))
    if unicodedata.category(chr(c))[0] == "M"
)
_JOINS = rf"[\w{_MARKS}]"
_JOINS_RE = re.compile(_JOINS)

type OnText = Callable[[str], str]


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def trim_url(url: str) -> str:
    """A bare URL `_URL_RE` matched, without the trailing punctuation of the sentence around it; "" when
    nothing is left past the scheme. A closing bracket stays while the URL opens as many (GitHub's rule)."""
    host = _URL_HOST_RE.match(url)
    if host and url.startswith("'", host.end()):  # no host name holds a `'`: `x.com's API`
        url = url[: host.end()]
    unclosed = {c: url.count(c) - url.count(o) for c, o in _URL_CLOSERS.items()}
    end = len(url)
    while (ch := url[end - 1]) in _URL_TRAIL or unclosed.get(ch, 0) > 0:
        if ch in unclosed:
            unclosed[ch] -= 1
        end -= 1
    url = url[:end] if url.find("//") + 2 < end else ""
    try:
        urlsplit(url)  # "https://[x": a link no reader could parse back
    except ValueError:
        return ""
    return url


def _escaped(line: str, i: int) -> bool:
    """Whether `line[i]` follows an odd run of backslashes."""
    back = i
    while back and line[back - 1] == "\\":
        back -= 1
    return (i - back) % 2 == 1


def _inline(line: str, on_text: OnText, lead: bool = False, after: str = "") -> str:
    """One line of inline markup; `on_text` renders the plain runs between spans. `lead`: a full-mode
    text line, where `\\` before a block marker is one backslash; `after`: what follows a span's content.

    Recursion stays shallow: a span's content cannot hold its own unescaped marker (the nearest one
    closes it).
    """
    find = line.find
    # Past the last `]` or `)` a link cannot close: an unclosed `[`/`(` run must not re-scan to the end.
    last_close, last_paren = line.rfind("]"), line.rfind(")")

    def closer(i: int, marker: str) -> int:
        # Intra-word or space-padded markers stay literal: `2*3*4`, `snake_case_name`, `a * b`.
        start = i + len(marker)
        if (i and _word(line[i - 1])) or start >= len(line) or line[start].isspace():
            return -1
        end = find(marker, start)
        # An escaped closer is text: look past it (`*a\*b*`). Linear: an unescaped candidate ends the
        # search, so each escaped one costs only its own backslash run.
        while end > start and _escaped(line, end):
            end = find(marker, end + 1)
        after = end + len(marker)
        if end <= start or line[end - 1].isspace() or (after < len(line) and _word(line[after])):
            return -1
        return end

    out: list[str] = []
    bad_link = -1  # every `[` before the same `](…)` shares its verdict: check the URL once
    i = flushed = 0
    while (m := _SPECIAL_RE.search(line, i)) is not None:
        i = m.start()
        hit: tuple[str, int] | None = None
        ch = line[i]
        if ch == "h":  # a bare URL is no markup, markers and all: on_text links it
            i = url_m.end() if (url_m := _URL_RE.match(line, i)) else i + 1
            continue
        if ch == "`":
            end = find("`", i + 1)
            if end > i + 1:
                hit = f"<code>{html.escape(line[i + 1 : end])}</code>", end + 1
        elif ch == "\\":
            nxt = line[i + 1 : i + 2]
            # `\\srv\share` stays as typed: a backslash pair collapses only before what it would escape.
            if nxt == "\\":
                if (line[i + 2 : i + 3] or after) in ESCAPABLE or (
                    lead and not i and _literal(line[1:]) != line[1:]
                ):
                    hit = nxt, i + 2
            elif nxt in ESCAPABLE:  # none of them needs HTML escaping
                hit = nxt, i + 2
        elif ch == "[":
            close = find("]", i + 1) if i < last_close else -1
            if (
                close > i + 1
                and close != bad_link
                and close + 2 <= last_paren
                and line.startswith("(", close + 1)
            ):
                end = find(")", close + 2)
                url = line[close + 2 : end]
                if end > 0 and url.count("(") == 1:  # one balanced pair: wiki-style `Foo_(bar)`
                    end = find(")", end + 1)
                    url = line[close + 2 : end]
                if end > 0 and url.count("(") == url.count(")") <= 1 and _URL_RE.fullmatch(url):
                    label = html.escape(line[i + 1 : close])
                    hit = f'<a href="{html.escape(url)}">{label}</a>', end + 1
                else:
                    bad_link = close
        else:
            for marker, tag in _SPANS:
                if line.startswith(marker, i) and (end := closer(i, marker)) >= 0:
                    # Flushed before the span's content: mentions are numbered and paired in text order.
                    out.append(on_text(line[flushed:i]))
                    flushed = i
                    inner = _inline(line[i + len(marker) : end], on_text, after=marker[0])
                    hit = f"<{tag}>{inner}</{tag}>", end + len(marker)
                    break
            else:
                # A rejected run stays literal whole, or `2**3**4` would open on its second `*`.
                run = _RUN_RE.match(line, i)
                i = run.end() - 1 if run else i
        if hit is None:
            i += 1
            continue
        out += (on_text(line[flushed:i]), hit[0])
        i = flushed = hit[1]
    out.append(on_text(line[flushed:]))
    return "".join(out)


def _literal(line: str) -> str:
    """`line` without the `\\` that keeps its leading block marker (`- `, `* `, `> `, `1. `) literal."""
    rest = line[1:]
    if line.startswith("\\") and (
        rest.startswith(("- ", "* ", "> ")) or rest == ">" or _ORDERED_RE.match(rest)
    ):
        return rest
    return line


def _blocks(text: str, on_text: OnText) -> str:
    """Full mode: fenced code, `> ` quotes, `- `/`* `/`1. ` lists; other lines inline, joined by <br>."""
    lines = text.split("\n")
    fences = [n for n, line in enumerate(lines) if line.startswith("```")]
    fence_end = dict(zip(fences, fences[1:], strict=False))
    kinds: list[tuple[str, Callable[[str], int]]] = [
        ("blockquote", lambda s: 2 if s.startswith("> ") else 1 if s == ">" else 0),
        ("ul", lambda s: 2 if s.startswith(("- ", "* ")) else 0),
        ("ol", lambda s: m.end() if (m := _ORDERED_RE.match(s)) else 0),
    ]
    out: list[str] = []
    after_text = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if (close := fence_end.get(i)) is not None:
            code = html.escape("\n".join(lines[i + 1 : close]))
            out.append(f"<pre><code>{code}</code></pre>")
            i, after_text = close + 1, False
            continue
        for tag, marker in kinds:
            if marker(line):
                run: list[tuple[str, int]] = []
                end = i
                while end < len(lines) and (cut := marker(lines[end])):
                    run.append((lines[end], cut))
                    end += 1
                if tag == "blockquote" and all(len(text) == cut for text, cut in run):
                    continue  # `>` alone is what was typed, not an empty quote
                items = [_inline(text[cut:], on_text) for text, cut in run]
                i = end
                if tag == "blockquote":
                    out.append(f"<blockquote>{'<br>'.join(items)}</blockquote>")
                else:
                    out.append(f"<{tag}>{''.join(f'<li>{it}</li>' for it in items)}</{tag}>")
                after_text = False
                break
        else:
            out.append(("<br>" if after_text else "") + _inline(_literal(line), on_text, lead=True))
            after_text = True
            i += 1
    return "".join(out)


def _render(text: str, full: bool, on_text: OnText) -> str:
    if len(text) > MAX_TEXT:
        raise ValueError(f"message too long ({len(text)} > {MAX_TEXT} characters)")
    if full:
        return _blocks(text, on_text)
    return "<br>".join(_inline(line, on_text) for line in text.split("\n"))


def compose_html(
    text: str,
    mentions: list[dict[str, Any]],
    emojis: dict[str, str],
    *,
    full: bool = False,
    asyncgw_host: str = _ASYNCGW_HOST,
) -> tuple[str, dict[str, Any] | None]:
    """Composer text → (content HTML, message `properties` or None).

    `mentions`: picked `{name, mri, label?, everyone?}`, `name` as typed and `label` as shown (default
    `name`), in the order picked: the n-th `@Name` goes to the n-th pick of that name, any further one
    to its last; `emojis`: custom name → AMS object id. `@Name` matches when `@` starts a word and
    the name is followed by the end or neither a word character nor a mark; names are tried longest first,
    case-sensitive, so `@Ann Lee` wins over `@Ann`. Code, link labels and URLs are never expanded.
    A backslash makes ESCAPABLE characters literal, and in full mode a leading block marker (spec 019).

    Trusts every `mri` as given: the caller checks each against the thread roster (the thread id
    for everyone). More than MAX_MENTIONS entries or a name past MAX_NAME raises ValueError.
    """
    if len(mentions) > MAX_MENTIONS:
        raise ValueError(f"too many mentions ({len(mentions)} > {MAX_MENTIONS})")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        if name := str(mention.get("name") or ""):
            if max(len(name), len(str(mention.get("label") or ""))) > MAX_NAME:
                raise ValueError(f"mention name too long (> {MAX_NAME} characters)")
            by_name.setdefault(name, []).append(mention)
    seen: dict[str, int] = {}  # name → occurrences so far
    alternatives = "|".join(re.escape(n) for n in sorted(by_name, key=len, reverse=True))
    mention_re = rf"(?<!\w)@(?P<name>{alternatives})(?!{_JOINS})|" if alternatives else ""
    # Bare URLs go first: no emoji or mention inside a URL.
    token_re = re.compile(rf"(?P<url>{_URL_RE.pattern})|" + mention_re + _EMOJI)
    entries: list[dict[str, Any]] = []

    def token(m: re.Match[str]) -> str | None:
        if m.group("url") is not None:
            if not (url := trim_url(m.group())):
                return html.escape(m.group())
            link = html.escape(url)
            return f'<a href="{link}">{link}</a>{html.escape(m.group()[len(url) :])}'
        if (name := m.group("name") if alternatives else None) is not None:
            if len(entries) == MAX_MENTIONS:  # a pick stands for every occurrence of its name
                raise ValueError(f"too many mentions (> {MAX_MENTIONS})")
            picks = by_name[name]
            picked = picks[min(seen.get(name, 0), len(picks) - 1)]
            seen[name] = seen.get(name, 0) + 1
            everyone = bool(picked.get("everyone"))
            label = "Everyone" if everyone else str(picked.get("label") or name)
            entries.append(
                {
                    "@type": _MENTION,
                    "itemid": len(entries),
                    "mri": str(picked.get("mri") or ""),
                    "mentionType": "everyone" if everyone else "person",
                    "displayName": label,
                }
            )
            return (
                f'<span itemtype="{_MENTION}" itemscope="" itemid="{len(entries) - 1}">'
                f"{html.escape(label)}</span>"
            )
        emoji = m.group("emoji")
        if emoji in SHORTCODES:
            return SHORTCODES[emoji]
        if emoji in emojis:
            name, oid = html.escape(emoji), html.escape(emojis[emoji])
            # alt is the bare name, as in every CustomEmoji Teams wrote to the archive.
            return (
                f'<img alt="{name}" itemid="{name};{oid}" itemtype="{_CUSTOM_EMOJI}" '
                f'src="https://{asyncgw_host}/v1/objects/{oid}/views/imgt2_anim" '
                'style="width:20px; height:20px">'
            )
        return TEAMS_NAMES.get(emoji)

    def on_text(seg: str) -> str:
        out: list[str] = []
        pos = scan = 0
        while (m := token_re.search(seg, scan)) is not None:
            if (rendered := token(m)) is None:
                scan = m.end()
                continue
            out += (html.escape(seg[pos : m.start()]), rendered)
            pos = scan = m.end() + (m.group("emoji") is not None)  # consume the emoji's closing `:`
        out.append(html.escape(seg[pos:]))
        return "".join(out)

    content = _render(text, full, on_text)
    return content, ({"mentions": json.dumps(entries, ensure_ascii=False)} if entries else None)


def _candidates(text: str) -> list[str]:
    """The text after each `@` compose_html (full mode) could expand: code, link labels, URLs and `\\@`
    never reach `on_text`, or are skipped."""
    found: list[str] = []

    def on_text(seg: str) -> str:
        # A name past MAX_NAME never matches: the rest of the text is not kept per `@`.
        found.extend(
            seg[m.end() : m.end() + MAX_NAME + 1]
            for m in _CANDIDATE_RE.finditer(seg)
            if m.group("url") is None
        )
        return ""

    _render(text, True, on_text)
    return found


def holds_mention(text: str) -> bool:
    """Whether `text` holds an `@Name` to resolve: only then is the roster needed (spec 029)."""
    return bool(_candidates(text))


def _joins(ch: str) -> bool:
    """`ch` continues the word before it: a word character or a mark; compose_html's name end."""
    return _JOINS_RE.fullmatch(ch) is not None


def _typed_len(rest: str, form: str) -> int:
    """Length of the longest start of `rest` that is `form`, case and accents ignored, and ends a word; 0
    for none. Grown a character at a time, folded per character: folding changes lengths (`ß` → `ss`,
    a combining accent → "")."""
    want, got, best = fold(form), "", 0
    for k, ch in enumerate(rest, 1):
        got += fold(ch)
        if not want.startswith(got):
            break
        if got == want and not _joins(rest[k : k + 1] or " "):
            best = k
    return best


def resolve_mentions(
    text: str, members: list[dict[str, Any]], thread_id: str, me: str
) -> list[dict[str, Any]]:
    """Each `@Name` of `text` (full mode) → a compose_html pick `{name, mri, label?, everyone?}` (spec 029).

    A member's full or first name matches, case and accents ignored, longest first; `name` is the
    spelling typed, `label` the roster's. Bots, apps and me are no candidates; `@everyone` is the
    chat's on groups, meetings and channels. ValueError for a name matching nobody or several members,
    or more than MAX_MENTIONS."""
    candidates = _candidates(text)
    if len(candidates) > MAX_MENTIONS:
        raise ValueError(f"too many mentions (> {MAX_MENTIONS})")
    roster = humans(members)
    forms: list[tuple[str, dict[str, Any]]] = []  # (roster spelling, pick)
    if chat_kind(thread_id) not in ("private", "system"):
        forms.append(("everyone", {"mri": thread_id, "everyone": True}))
    for mri, name in roster.items():
        if name.strip() and not is_me(mri, me):
            forms += ((form, {"mri": mri, "label": form}) for form in dict.fromkeys((name, name.split()[0])))
    picks: dict[str, dict[str, Any]] = {}  # typed spelling → pick
    for rest in candidates:
        hits = [(n, pick) for form, pick in forms if (n := _typed_len(rest, form))]
        size = max((n for n, _ in hits), default=0)
        found = {pick["mri"]: pick for n, pick in hits if n == size}
        typed = rest[: size or next((i for i, c in enumerate(rest) if not _joins(c)), len(rest))]
        if not found:
            if fold(typed) == "everyone":
                raise ValueError("@everyone is not available in this chat")
            raise ValueError(f"unknown mention @{typed[:MAX_NAME]}")
        if len(found) > 1:
            names = sorted((roster.get(mri, "Everyone") for mri in found), key=fold)
            raise ValueError(f"ambiguous mention @{typed[:MAX_NAME]}: {', '.join(names)}")
        picks.setdefault(typed, {"name": typed, **next(iter(found.values()))})
    return list(picks.values())


def render_outgoing(
    text: str, thread_id: str, members: Callable[[], list[dict[str, Any]] | None], me: Callable[[], str]
) -> tuple[str, dict[str, Any] | None]:
    """MCP and CLI text → (HTML, properties) as the chat page's composer renders it, each `@Name` resolved
    on `members()` (spec 029). `members` and `me` are only called for a text holding an `@Name`, Notes
    to self has nobody; `members()` None is an unavailable roster. ValueError for every refusal."""
    mentions: list[dict[str, Any]] = []
    if holds_mention(text):
        roster = [] if thread_id == NOTES_THREAD else members()
        if roster is None:
            raise ValueError("chat members unavailable")
        mentions = resolve_mentions(text, roster, thread_id, me())
    # Built-in shortcodes only: the custom emoji table needs another token and fetch.
    content, props = compose_html(text, mentions, {}, full=True)
    if too_long(content, is_html=True):
        raise ValueError(f"message too long: over Teams' limit of {MAX_HTML_BYTES // 1024} KB once rendered")
    return content, props


def reply_quote(message: dict[str, Any], name: str) -> str:
    """Teams' Reply block quoting `message`, in the shape the archive holds (spec 011)."""
    msg_id = html.escape(str(message.get("id") or ""))
    mri = html.escape(str(message.get("from") or "").rsplit("/", 1)[-1])
    props = message.get("properties")
    msgtype, content = str(message.get("messagetype") or ""), str(message.get("content") or "")
    preview = snippet(msgtype, content, props if isinstance(props, dict) else None)[:_PREVIEW_LEN]
    return (
        f'<blockquote itemscope itemtype="http://schema.skype.com/Reply" itemid="{msg_id}">'
        f'<strong itemprop="mri" itemid="{mri}">{html.escape(name)}</strong>'
        f'<span itemprop="time" itemid="{msg_id}"></span>'
        f'<p itemprop="preview">{html.escape(preview)}</p></blockquote>'
    )
