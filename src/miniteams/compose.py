"""Composer text → Teams message HTML: a markdown subset, `:shortcode:` emojis, `@Name` mentions.

Pure (spec 012). No backtracking regex: closers are found with `str.find`.
"""

import functools
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
from .emoji_table import SHORTCODES
from .messages import fold, is_me, snippet
from .send import MAX_HTML_BYTES, NOTES_THREAD, too_long

MAX_TEXT = 28 * 1024  # composer characters; send.too_long caps the posted HTML bytes
_ASYNCGW_HOST: str = Settings.model_fields["asyncgw_host"].default
EMOJI_NAME_RE = re.compile(r"[A-Za-z0-9_+-]{1,64}")  # what `:name:` expands

_MENTION = "http://schema.skype.com/Mention"
_CUSTOM_EMOJI = "http://schema.skype.com/CustomEmoji"
_SPECIAL_RE = re.compile(r"[\\`\[*_~]|https?://|@\{")
ESCAPABLE = frozenset("\\*_~`[@")  # `\x` gives a literal x; `\\` only where a lone `\` would escape
_RUN_RE = re.compile(r"\*+|_+|~+")
# `'` only before a word character: `Hitchhiker's_Guide`, not `'https://a.com/x'|jq`.
_URL_RE = re.compile(r"https?://(?:[^\s<>\"`']|'(?=\w))+")
# Sentence punctuation after a URL; `-` stays (spec 028). `'"<>` never end a _URL_RE match.
_URL_TRAIL = frozenset(".,:;!?*_~…–—«»‹›“”‘’")
_URL_CLOSERS = {")": "(", "]": "[", "}": "{"}
_URL_HOST_RE = re.compile(r"https?://[^/?#']*")
_ORDERED_RE = re.compile(r"\d{1,9}\. ")
# The trailing `:` is a lookahead so an unknown name gives it back: `:nope::fire:` still finds `:fire:`;
# never a URL's `:` (`URL:https://…`), nor one glued to a word (`a:b:c`, `fe80::1:a:b`, `10:30:x:`).
_EMOJI = rf"(?<![A-Za-z0-9]):(?P<emoji>{EMOJI_NAME_RE.pattern})(?=:(?!//))"
_PREVIEW_LEN = 100  # characters of the quoted message a Reply block carries
_SPANS = (("**", "b"), ("~~", "s"), ("*", "i"), ("_", "i"))
MAX_MENTIONS = 50
MAX_NAME = 200
_FREE_LEN = MAX_FREE_NAME = 2 * MAX_NAME + 3  # `{Who|label}`, each part at most MAX_NAME
# What `plain_label` refuses (spec 032): a `{Who|label}` is read raw, markup and all, up to its `}`.
_LABEL_RULE = (
    "the label is one line of text: no | or }, no tab or other blank but the space, no control, format, "
    "private-use or unassigned character"
)
_LITERAL = " (\\@ for a literal @)"
_SCANS = 2 * MAX_MENTIONS  # `@` checked against picks or roster: the mentions plus as many that are not
# An `@` starting a word, which compose_html expands when a name follows; URLs matched first to skip theirs.
_CANDIDATE_RE = re.compile(rf"(?P<url>{_URL_RE.pattern})|(?<!\w)@")
_OPENS_RE = re.compile(r"[\w{]")  # what follows an `@` a sender resolves: a name or a `{Who|label}`
# What continues a word, so a name cannot end before it: `\w` and the marks (`re` has no \p{M}; ui.js's
# `scan` reads the same `[\p{L}\p{N}_\p{M}]`). Planes 0, 1 and 14 hold every mark.
_MARKS = "".join(
    chr(c)
    for c in itertools.chain(range(0x20000), range(0xE0000, 0xE1000))
    if unicodedata.category(chr(c))[0] == "M"
)
_JOINS = rf"[\w{_MARKS}]"
_JOINS_RE = re.compile(_JOINS)
_TRIM_RE = re.compile(rf"^[^\w{_MARKS}]+|[^\w{_MARKS}]+$")  # punctuation around a word

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
        # An escaped closer is text, and so is one inside a `@{Who|label}` the content's own reading will
        # take (`*see @{Ann|a* b}*`, `_@{Ann|a_ b}_`): look past it. Linear: tokens are walked lazily, only
        # as far as the candidate, and an unescaped candidate outside one ends the search.
        walked, token = start, (-1, -1)
        while end > start:
            while walked <= end:
                walked, *found = _atom(line, walked, end + 1, start, marker[0])
                token = (found[0], found[1]) if found[0] >= 0 else token
            if not (_escaped(line, end) or token[0] <= end <= token[1]):
                break
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
        if ch == "@":
            # `@{Who|label}` starting a word is read raw up to its `}`, before any markup (spec 032): its own
            # run, so a mention's label or a URL inside it is never split by `*`, `_`, `[`…
            if (end := _token_end(line, i)) > 0:
                out.append(on_text(line[flushed:i]))  # first: mentions are numbered in text order
                flushed = i
                hit = on_text(line[i : end + 1]), end + 1
            else:
                i += 1
                continue
        if ch == "`":
            end = find("`", i + 1)
            if end > i + 1:
                hit = f"<code>{html.escape(line[i + 1 : end])}</code>", end + 1
        elif ch == "\\":
            if _escape_len(line, i, after, lead):  # none of the escaped characters needs HTML escaping
                hit = line[i + 1], i + 2
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


_ATOM_RE = re.compile(r"https?://|[\\`]|@\{")  # what `_inline` reads whole before markup: URL, escape, code


def _token_end(line: str, i: int, lo: int = 0, opened: bool = False) -> int:
    """The `}` ending the `@{Who|label}` token `_inline` reads at `i`, else -1: `@{` at a word start (the
    run's start `lo` or after a non-word character) or, with `opened`, right after a `_` that opens a span
    there (`a _@{…}_`), closed on the line (spec 032)."""

    def starts(k: int) -> bool:
        return k <= lo or not _word(line[k - 1])

    if not line.startswith("@{", i) or not (starts(i) or opened and line[i - 1] == "_" and starts(i - 1)):
        return -1
    return line.find("}", i + 2)


def _escape_len(line: str, i: int, after: str, lead: bool) -> int:
    """2 when `_inline` reads the backslash at `i` as escaping the next character, else 0 (kept as typed).
    `\\srv\\share` stays: a backslash pair collapses only before what it would escape."""
    nxt = line[i + 1 : i + 2]
    if nxt == "\\":
        escapes = (line[i + 2 : i + 3] or after) in ESCAPABLE or (
            lead and not i and _literal(line[1:]) != line[1:]
        )
        return 2 if escapes else 0
    return 2 if nxt in ESCAPABLE else 0


def _atom(line: str, i: int, hi: int, lo: int, after: str) -> tuple[int, int, int]:
    """One step of reading a span's content `line[lo:]` as `_inline` reads it, from `i`, looking no further
    than `hi`: (where the next step starts, and the `@`/`}` of a `@{…}` token read on the way or -1, -1).
    URLs, escapes and code spans are taken whole, so nothing inside them is a token; a `_` right before a
    `@{` there opens the span, not a word."""
    if (m := _ATOM_RE.search(line, i, hi)) is None:
        return hi, -1, -1
    i, ch = m.start(), line[m.start()]
    if ch == "h":
        return (url.end() if (url := _URL_RE.match(line, i)) else i + 1), -1, -1
    if ch == "\\":
        return i + (_escape_len(line, i, after, False) or 1), -1, -1
    if ch == "`":
        end = line.find("`", i + 1)
        return (end + 1 if end > i + 1 else i + 1), -1, -1
    if (end := _token_end(line, i, lo, True)) > 0:
        return end + 1, i, end
    return i + 1, -1, -1


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
    `name`, or the label of a `{Who|label}` name, spec 032), in the order picked: the n-th `@Name` goes to
    the n-th pick of that name, any further one to its last; `emojis`: custom name → AMS object id.
    `@Name` matches when `@` starts a word and the name is followed by the end or neither a word character
    nor a mark (a `{Who|label}` name ends at its `}`, whatever follows); names are tried longest first,
    case-sensitive, so `@Ann Lee` wins over `@Ann`. Code, link labels and URLs are never expanded. A
    backslash makes ESCAPABLE characters literal, and in full mode a leading block marker (spec 019).

    Trusts every `mri` as given: the caller checks each against the thread roster (the thread id
    for everyone). More than MAX_MENTIONS entries, a label or a name past MAX_NAME (a `{Who|label}` name:
    2 * MAX_NAME + 3) raises ValueError.
    """
    # Distinct ones: a resolver's pick spelled another way (`@ann` for `Ann`) is still that one mention.
    distinct = {
        (str(m.get("mri")), shown_label(str(m.get("label") or m.get("name"))), bool(m.get("everyone")))
        for m in mentions
    }
    if len(distinct) > MAX_MENTIONS:
        raise ValueError(f"too many mentions ({len(distinct)} > {MAX_MENTIONS})")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for mention in mentions:
        if name := str(mention.get("name") or ""):
            limit = _FREE_LEN if name.startswith("{") else MAX_NAME
            if len(name) > limit or len(str(mention.get("label") or "")) > MAX_NAME:
                raise ValueError(f"mention name too long (> {limit} characters)")
            by_name.setdefault(name, []).append(mention)
    seen: dict[str, int] = {}  # name → occurrences so far
    names = sorted(by_name, key=len, reverse=True)
    braced = "|".join(re.escape(n) for n in names if n.startswith("{"))
    plain = "|".join(re.escape(n) for n in names if not n.startswith("{"))
    # A `{Who|label}` ends at its `}`, whatever follows, as free_label reads it (`_a_@{Ann|x}y`).
    alternatives = "|".join(filter(None, [braced, plain and f"(?:{plain})(?!{_JOINS})"]))
    mention_re = rf"(?<!\w)@(?P<name>{alternatives})|" if alternatives else ""
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
            label = "Everyone" if everyone else str(picked.get("label") or shown_label(name))
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
        # Custom first: `:name:` is what a custom pick inserts, a built-in pick inserts its character.
        if emoji in emojis:
            name, oid = html.escape(emoji), html.escape(emojis[emoji])
            # alt is the bare name, as in every CustomEmoji Teams wrote to the archive.
            return (
                f'<img alt="{name}" itemid="{name};{oid}" itemtype="{_CUSTOM_EMOJI}" '
                f'src="https://{asyncgw_host}/v1/objects/{oid}/views/imgt2_anim" '
                'style="width:20px; height:20px">'
            )
        return SHORTCODES.get(emoji)

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


def shown_label(name: str) -> str:
    """What a pick named `name` displays without a `label`: a well-formed `{Who|label}`'s label, else the
    name (`{x`, `{a*b}`: a label Teams wrote)."""
    try:
        source, _, label = free_label(name)
    except ValueError:
        return name
    return label if source == name else name


def _segments(text: str, full: bool = True) -> list[str]:
    """The plain runs compose_html's `on_text` gets for `text`: code, link labels and `\\@` never reach it."""
    found: list[str] = []

    def on_text(seg: str) -> str:
        found.append(seg)
        return ""

    _render(text, full, on_text)
    return found


def _next_at(seg: str, pos: int) -> int:
    """Where the text after the next `@` starting a word begins, from `pos`, a URL met on the way skipped
    whole as compose_html's `on_text` skips it; -1 for none."""
    while (m := _CANDIDATE_RE.search(seg, pos)) is not None:
        if m.group("url") is None:
            return m.end()
        pos = m.end()
    return -1


def _opens(rest: str) -> bool:
    return _OPENS_RE.match(rest) is not None


def holds_mention(text: str, full: bool = True) -> bool:
    """Whether `text` holds an `@Name` to resolve, as compose_html reads it (spec 029): without any, nothing
    needs the roster."""
    for seg in _segments(text, full):
        at = 0
        while (at := _next_at(seg, at)) >= 0:
            if _opens(seg[at : at + 1]):
                return True
    return False


def _joins(ch: str) -> bool:
    """`ch` continues the word before it: a word character or a mark; compose_html's name end."""
    return _JOINS_RE.fullmatch(ch) is not None


def forms(name: str) -> list[str]:
    """The forms a member is mentioned by (spec 032): full name, first word, last word, the last two
    without the punctuation around them (`Doe, John` → `Doe`; `(Sales)` → `Sales`)."""
    words = name.split()
    if not words:
        return []
    return list(
        dict.fromkeys((name, *(word for w in (words[0], words[-1]) if (word := _TRIM_RE.sub("", w)))))
    )


def _word_key(text: str) -> str:
    """`text`'s first word, folded: the forms an `@` can be followed by share it with the text after it."""
    return _fold(text[: next((i for i, c in enumerate(text) if not _joins(c)), len(text))])


def plain_label(label: str) -> bool:
    """Whether a `{Who|label}` can hold `label` (spec 032, _LABEL_RULE): no `|` or `}`, and nothing in
    Unicode's C or Z categories (line break, tab, blanks, control, format, private-use, unassigned) but the
    space."""
    return not any(c in "|}" or c != " " and unicodedata.category(c)[0] in "CZ" for c in label)


def free_label(rest: str) -> tuple[str, str, str]:
    """`rest` opening a `{Who|label}` → (that source, Who, label): up to the first `}`, the last `|`
    splitting (spec 032). Who may hold anything but `}` and a line break. ValueError for an unclosed one,
    or a missing, empty, too long or refused label."""
    end = rest.find("}")
    if end < 0:
        raise ValueError(
            f"unclosed mention @{rest[:MAX_NAME]}: write @{{Who|label}}, {_LABEL_RULE}{_LITERAL}"
        )
    source = rest[: end + 1]
    who, bar, label = source[1:-1].rpartition("|")
    if not bar:
        raise ValueError(f"mention @{source} has no label: write @{{Who|label}}{_LITERAL}")
    if not label.strip():
        raise ValueError(f"empty mention label @{source}{_LITERAL}")
    if not plain_label(label):
        raise ValueError(f"mention label refused @{source}: {_LABEL_RULE}{_LITERAL}")
    if len(label) > MAX_NAME:
        raise ValueError(f"mention label too long (> {MAX_NAME} characters){_LITERAL}")
    return source, who.strip(), label


def _typed_len(rest: str, form: str) -> int:
    """Length of the longest start of `rest` that is `form`, case and accents ignored, and ends a word (a
    `{Who|label}` form ends at its `}`, whatever follows, as compose_html reads it); 0 for none. Grown a
    character at a time, folded per character: folding changes lengths (`ß` → `ss`, a combining accent →
    "")."""
    want, got, best, braced = _fold(form), 0, 0, form.startswith("{")
    for k, ch in enumerate(rest, 1):
        if not want.startswith(part := _fold(ch), got):  # from `got` on: linear in what is read
            break
        got += len(part)
        if got == len(want) and (braced or not _joins(rest[k : k + 1] or " ")):
            best = k
    return best


_fold_cached = functools.lru_cache(maxsize=4096)(fold)


def _fold(text: str) -> str:
    # Bounded in count and size: characters and short names repeat; a long key would pin its memory.
    return _fold_cached(text) if len(text) <= 64 else fold(text)


def resolve_mentions(
    text: str,
    members: list[dict[str, Any]],
    thread_id: str,
    me: str,
    *,
    full: bool = True,
    picks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Each `@Name` of `text` → a compose_html pick `{name, mri, label?, everyone?}` (specs 029, 032).

    A member's full, first or last name matches, case and accents ignored, longest first; `name` is the
    spelling typed, `label` the roster's. `@{Who|label}` names the member Who matches whole, shown as
    `label`. Bots, apps and me are no candidates; `@everyone` is the chat's on groups, meetings and
    channels. An `@` a caller's pick names (case and accents ignored) is that pick's, unless a longer
    roster form is typed there; the n-th one of a spelling takes the n-th of the picks naming it (the last
    past them). One pick per `@`, in text order, named as typed: compose_html's pairing, done once, on the
    text walked as it walks it (resuming past each name read, URLs skipped whole). ValueError for a name
    matching nobody or several members, a malformed label, more than MAX_MENTIONS, or more than _SCANS
    `@` to check against the picks."""
    picks = [p for p in picks or () if p.get("name")]
    starts = {fold(str(p["name"]))[:1] for p in picks}  # an `@` no pick can start costs no pick scan
    roster = humans(members)
    people = [
        (form, {"mri": mri, "label": form})
        for mri, name in roster.items()
        if not is_me(mri, me)
        for form in forms(name)
    ]
    everyone = [("everyone", {"mri": thread_id, "everyone": True})]
    by_word: dict[str, list[tuple[str, dict[str, Any]]]] = {}  # first word → its forms: a bucket per `@`
    for form, pick in (everyone if chat_kind(thread_id) not in ("private", "system") else []) + people:
        by_word.setdefault(_word_key(form), []).append((form, pick))

    def one(typed: str, found: dict[str, dict[str, Any]]) -> dict[str, Any]:
        shown = typed if typed.startswith("{") else typed[:MAX_NAME]
        if not found:
            if fold(typed) == "everyone":
                raise ValueError("@everyone is not available in this chat")
            raise ValueError(f"unknown mention @{shown}{_LITERAL}")
        if len(found) > 1:
            names = sorted((roster.get(mri, "Everyone") for mri in found), key=fold)
            raise ValueError(f"ambiguous mention @{shown}: {', '.join(names)}{_LITERAL}")
        return next(iter(found.values()))

    out: list[dict[str, Any]] = []
    occurrences: dict[str, int] = {}  # folded spelling → `@` a pick covered so far
    scans = 0
    for seg in _segments(text, full):
        at = 0
        while (at := _next_at(seg, at)) >= 0:
            # A name past _FREE_LEN never matches: the rest of the text is not kept per `@`.
            rest = seg[at : at + _FREE_LEN + 1]
            first = fold(rest[:1])[:1]
            if not (_opens(rest) or first in starts):
                continue
            # Each `@` costs a scan of the picks or the roster: bounded whatever the text.
            if (scans := scans + 1) > _SCANS:
                raise ValueError(f"too many @ to check (> {_SCANS}){_LITERAL}")
            ties: list[dict[str, Any]] = []  # the picks covering the longest start of `rest`, in pick order
            covered = 0
            for pick in picks if not first or first in starts else ():
                if (n := _typed_len(rest, str(pick["name"]))) and n >= covered:
                    ties, covered = ([*ties, pick] if n == covered else [pick]), n
            if not ties and not _opens(rest):
                continue
            if len(out) == MAX_MENTIONS:
                raise ValueError(f"too many mentions (> {MAX_MENTIONS})")
            # Only the forms sharing the first word typed: a large roster costs a bucket, not every name. A
            # `{` opens a free label, never a name, whatever a member calls themselves.
            bucket = () if rest.startswith("{") else by_word.get(_word_key(rest), ())
            hits = [(n, pick) for form, pick in bucket if (n := _typed_len(rest, form))]
            size = max((n for n, _ in hits), default=0)
            if ties and covered >= size:
                typed = rest[:covered]
                k = occurrences.get(key := fold(typed), 0)
                occurrences[key] = k + 1
                cover = ties[min(k, len(ties) - 1)]
                # A free label shows as typed (`BOSS` for a pick `{Ann Lee|boss}`), a name as picked.
                free = shown_label(cover["name"]) != cover["name"]
                label = free_label(rest)[2] if free else cover.get("label") or cover["name"]
                found = {**cover, "label": label}
            elif rest.startswith("{"):
                typed, who, label = free_label(rest)
                # Whole, never a start of Who: `{Ann Leeds|x}` is not Ann Lee.
                forms_of = by_word.get(_word_key(who), ())
                whole = {
                    p["mri"]: p
                    for form, p in forms_of
                    if who and "everyone" not in p and _typed_len(who, form) == len(who)
                }
                found = {**one(typed, whole), "label": label}
            else:
                typed = rest[: size or next((i for i, c in enumerate(rest) if not _joins(c)), len(rest))]
                found = one(typed, {p["mri"]: p for n, p in hits if n == size})
            at += len(typed)  # compose_html resumes past the name: an `@` inside it is that name's
            out.append({**found, "name": typed})
    return out


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
