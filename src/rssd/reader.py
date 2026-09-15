"""Reading the store.

Everything the `rss` CLI and the TUI need in order to present the tree, and
nothing that writes to it. This module opens no file for writing, ever -- that
is the whole point of the reader being a separate program from the daemon.

Two levels of detail, deliberately:

  * `EntryRef` is built from the *filename alone*. Listing a thousand entries
    costs one readdir and no file reads, because the timestamp, the id prefix
    and the slug are all encoded in the name (SPEC §4.2).
  * `EntryDoc` is the parsed document, loaded only when something is actually
    displayed.
"""

from __future__ import annotations

import re
import textwrap
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree

from .config import NS, Config

NSMAP = {"r": NS}


class ReaderError(Exception):
    pass


# ── lightweight listing ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryRef:
    """An entry identified from its filename, without opening it."""

    feed: str
    path: Path
    id8: str
    slug: str
    published: datetime

    @property
    def title_hint(self) -> str:
        """A readable stand-in for the title until the document is loaded."""
        return self.slug.replace("-", " ").strip() or "untitled"


@dataclass(frozen=True, slots=True)
class FeedSummary:
    name: str
    title: str | None
    url: str | None
    health: str
    entries: int
    last_poll: datetime | None
    last_error: str | None
    state: str


_STAMP_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<id8>[0-9a-f]{8})-(?P<slug>.*?)(?:\.r(?P<rev>\d+))?\.xml$"
)


def _parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def iter_entry_refs(config: Config, feed: str) -> list[EntryRef]:
    """Every current entry in a feed, newest first.

    Only the symlinks are listed: one per logical entry, each pointing at its
    newest revision. The `.rN` files behind them are history, reachable via
    `revisions()`.
    """
    entries_dir = config.entries_dir(feed)
    if not entries_dir.is_dir():
        return []
    refs: list[EntryRef] = []
    for path in entries_dir.iterdir():
        if not path.is_symlink():
            continue
        match = _STAMP_RE.match(path.name)
        if match is None or match.group("rev"):
            continue
        try:
            published = _parse_stamp(match.group("stamp"))
        except ValueError:
            continue
        refs.append(
            EntryRef(
                feed=feed,
                path=path,
                id8=match.group("id8"),
                slug=match.group("slug"),
                published=published,
            )
        )
    refs.sort(key=lambda r: (r.published, r.id8), reverse=True)
    return refs


def feed_names(config: Config) -> list[str]:
    if not config.store.is_dir():
        return []
    return sorted(p.name for p in config.store.iterdir() if p.is_dir())


def _first_text(path: Path, tag: str) -> str | None:
    """Pull one element's text out of feed.xml / status.xml.

    A regex rather than an XML parse, because these two files are rewritten
    under the reader's feet. The store renames atomically so a torn read is
    impossible, but a *stale* read is normal and fine -- and a regex degrades
    to None instead of raising when it loses the race.
    """
    try:
        data = path.read_text(errors="replace")
    except OSError:
        return None
    match = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", data)
    if match is None:
        return None
    return match.group(1).strip() or None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def feed_summary(config: Config, name: str) -> FeedSummary:
    status = config.status_xml(name)
    meta = config.feed_xml(name)
    entries_dir = config.entries_dir(name)
    count = 0
    if entries_dir.is_dir():
        count = sum(1 for p in entries_dir.iterdir() if p.is_symlink())
    return FeedSummary(
        name=name,
        title=_first_text(meta, "title"),
        url=_first_text(meta, "url"),
        health=_first_text(status, "health") or "pending",
        entries=count,
        last_poll=_parse_iso(_first_text(status, "last-poll-at")),
        last_error=_first_text(status, "last-error"),
        state=_first_text(meta, "state") or "active",
    )


def list_feeds(config: Config) -> list[FeedSummary]:
    return [feed_summary(config, name) for name in feed_names(config)]


# ── the parsed document ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryDoc:
    path: Path
    id: str
    id8: str
    revision: int
    feed: str
    feed_title: str | None
    feed_url: str | None
    title: str
    author: str | None
    link: str | None
    guid: str | None
    published: datetime | None
    published_origin: str
    first_seen: datetime | None
    updated: datetime | None
    categories: tuple[str, ...]
    content_origin: str
    content_hash: str | None
    content: etree._Element | None


def load_entry(path: Path) -> EntryDoc:
    try:
        tree = etree.parse(str(path))
    except (OSError, etree.XMLSyntaxError) as exc:
        raise ReaderError(f"{path}: {exc}") from exc
    root = tree.getroot()

    def one(xpath: str) -> etree._Element | None:
        found = root.xpath(xpath, namespaces=NSMAP)
        return found[0] if found else None

    def text(xpath: str) -> str | None:
        el = one(xpath)
        if el is None:
            return None
        value = "".join(el.itertext()).strip()
        return value or None

    content = one("r:content")
    source_feed = one("r:source/r:feed")

    return EntryDoc(
        path=path,
        id=root.get("id", ""),
        id8=(root.get("id", "").split(":")[-1] or "")[:8],
        revision=int(root.get("revision") or 1),
        feed=(source_feed.get("name") if source_feed is not None else "") or "",
        feed_title=(source_feed.get("title") if source_feed is not None else None),
        feed_url=(
            "".join(source_feed.itertext()).strip() if source_feed is not None else None
        ),
        title=text("r:title") or "untitled",
        author=text("r:author"),
        link=text("r:source/r:link"),
        guid=text("r:source/r:guid"),
        published=_parse_iso(text("r:published")),
        published_origin=(
            (one("r:published").get("origin") or "feed")
            if one("r:published") is not None
            else "feed"
        ),
        first_seen=_parse_iso(root.get("first-seen")),
        updated=_parse_iso(root.get("updated")),
        categories=tuple(
            "".join(c.itertext()).strip()
            for c in root.xpath("r:categories/r:category", namespaces=NSMAP)
        ),
        content_origin=(content.get("origin") if content is not None else "none")
        or "none",
        content_hash=(content.get("hash") if content is not None else None),
        content=content,
    )


def revisions(path: Path) -> list[Path]:
    """Every stored revision of an entry, oldest first."""
    match = _STAMP_RE.match(path.name)
    if match is None:
        return [path]
    base = f"{match.group('stamp')}-{match.group('id8')}-{match.group('slug')}"
    found = sorted(
        path.parent.glob(f"{base}.r*.xml"),
        key=lambda p: int(_STAMP_RE.match(p.name).group("rev") or 0),
    )
    return found or [path]


# ── resolving a user-supplied reference ─────────────────────────────────────


def resolve(config: Config, ref: str) -> Path:
    """Turn what someone typed into an entry path.

    Accepts a path, an 8-hex id prefix, a `feed:N` index (1-based, newest
    first), or a bare `feed` meaning its newest entry. Ambiguity is an error
    rather than a guess -- picking one of several matches silently is how you
    end up reading the wrong thing and not noticing.
    """
    candidate = Path(ref)
    if candidate.exists() and candidate.is_file():
        return candidate

    if ":" in ref:
        feed, _, index = ref.partition(":")
        refs = iter_entry_refs(config, feed)
        if not refs:
            raise ReaderError(f"no entries in feed {feed!r}")
        try:
            position = int(index)
        except ValueError as exc:
            raise ReaderError(f"not a number: {index!r}") from exc
        if not 1 <= position <= len(refs):
            raise ReaderError(f"{feed}:{position} out of range (1..{len(refs)})")
        return refs[position - 1].path

    if re.fullmatch(r"[0-9a-f]{2,8}", ref):
        matches = [
            entry
            for feed in feed_names(config)
            for entry in iter_entry_refs(config, feed)
            if entry.id8.startswith(ref)
        ]
        if len(matches) == 1:
            return matches[0].path
        if not matches:
            raise ReaderError(f"no entry matching id {ref!r}")
        listed = ", ".join(sorted(m.id8 for m in matches)[:6])
        raise ReaderError(f"ambiguous id {ref!r}: matches {listed}")

    if ref in feed_names(config):
        refs = iter_entry_refs(config, ref)
        if not refs:
            raise ReaderError(f"no entries in feed {ref!r}")
        return refs[0].path

    raise ReaderError(f"cannot resolve {ref!r}")


# ── rendering semantic content ──────────────────────────────────────────────

_INLINE = {"link", "emphasis", "strong", "code", "break", "image"}


def extract_links(content: etree._Element | None) -> list[tuple[str, str]]:
    """Every outbound link in an entry, as (text, href), first occurrence wins."""
    if content is None:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for el in content.iter():
        tag = etree.QName(el).localname
        href = el.get("href") if tag == "link" else el.get("src") if tag == "image" else None
        if not href or href in seen:
            continue
        seen.add(href)
        label = " ".join("".join(el.itertext()).split()) or (
            el.get("alt") or "" if tag == "image" else ""
        )
        out.append((label or href, href))
    return out


def _inline_text(el: etree._Element, *, marks: bool) -> str:
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        tag = etree.QName(child).localname
        inner = _inline_text(child, marks=marks)
        if tag == "break":
            parts.append("\n")
        elif tag == "code":
            parts.append(f"`{inner}`" if marks else inner)
        elif tag == "strong":
            parts.append(f"**{inner}**" if marks else inner)
        elif tag == "emphasis":
            parts.append(f"*{inner}*" if marks else inner)
        elif tag == "image":
            alt = child.get("alt") or "image"
            parts.append(f"[{alt}]")
        else:
            parts.append(inner)
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def to_text(
    content: etree._Element | None,
    *,
    width: int = 80,
    marks: bool = True,
    link_refs: bool = True,
) -> str:
    """Render semantic content as readable plain text.

    `link_refs` appends a numbered reference list, the way a text browser does,
    so URLs stay visible without cluttering the prose.
    """
    if content is None or len(content) == 0:
        return ""

    blocks: list[str] = []
    numbering: dict[str, int] = {}

    def link_marker(href: str) -> str:
        if not link_refs:
            return ""
        if href not in numbering:
            numbering[href] = len(numbering) + 1
        return f"[{numbering[href]}]"

    def render_inline(el: etree._Element) -> str:
        parts: list[str] = []
        if el.text:
            parts.append(el.text)
        for child in el:
            tag = etree.QName(child).localname
            if tag == "link":
                label = _inline_text(child, marks=marks) or child.get("href", "")
                parts.append(label + link_marker(child.get("href", "")))
            elif tag == "break":
                parts.append("\n")
            elif tag == "image":
                alt = child.get("alt") or "image"
                parts.append(f"[{alt}]" + link_marker(child.get("src", "")))
            elif tag == "code":
                inner = _inline_text(child, marks=marks)
                parts.append(f"`{inner}`" if marks else inner)
            elif tag == "strong":
                inner = _inline_text(child, marks=marks)
                parts.append(f"**{inner}**" if marks else inner)
            elif tag == "emphasis":
                inner = _inline_text(child, marks=marks)
                parts.append(f"*{inner}*" if marks else inner)
            else:
                parts.append(render_inline(child))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts)

    def wrap(text: str, indent: str = "", first: str | None = None) -> str:
        text = " ".join(text.split())
        if not text:
            return ""
        return textwrap.fill(
            text,
            width=max(width, 20),
            initial_indent=first if first is not None else indent,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        )

    def walk(el: etree._Element, indent: str = "") -> None:
        for child in el:
            tag = etree.QName(child).localname
            if tag == "paragraph":
                blocks.append(wrap(render_inline(child), indent))
            elif tag == "heading":
                level = child.get("level", "1")
                text = " ".join(render_inline(child).split())
                blocks.append(f"{indent}{'#' * int(level)} {text}" if marks else f"{indent}{text.upper()}")
            elif tag == "list":
                ordered = child.get("ordered") == "true"
                for n, item in enumerate(child, start=1):
                    bullet = f"{n}. " if ordered else "- "
                    if any(etree.QName(g).localname not in _INLINE for g in item):
                        blocks.append(f"{indent}{bullet}".rstrip())
                        walk(item, indent + "  ")
                    else:
                        blocks.append(
                            wrap(
                                render_inline(item),
                                indent + " " * len(bullet),
                                first=indent + bullet,
                            )
                        )
            elif tag == "quote":
                start = len(blocks)
                walk(child, indent)
                for i in range(start, len(blocks)):
                    blocks[i] = "\n".join(
                        f"{indent}> {line.lstrip()}" for line in blocks[i].splitlines()
                    )
            elif tag == "code":
                body = _inline_text(child, marks=False)
                blocks.append(
                    "\n".join(f"{indent}    {line}" for line in body.splitlines())
                )
            elif tag == "image":
                alt = child.get("alt") or "image"
                blocks.append(f"{indent}[{alt}]{link_marker(child.get('src', ''))}")
            elif tag == "separator":
                blocks.append(indent + "-" * min(width, 40))
            else:
                blocks.append(wrap(render_inline(child), indent))

    walk(content)
    body = "\n\n".join(b for b in blocks if b.strip())

    if link_refs and numbering:
        refs = "\n".join(f"  [{n}] {href}" for href, n in numbering.items())
        body = f"{body}\n\n{refs}" if body else refs
    return body


def plain_summary(content: etree._Element | None, limit: int = 200) -> str:
    """A one-line gist, for list views."""
    if content is None:
        return ""
    text = " ".join("".join(content.itertext()).split())
    return text[: limit - 1] + "…" if len(text) > limit else text
