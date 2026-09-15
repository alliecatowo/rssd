"""bytes + content-type -> ParsedFeed. PURE.

See SPEC §10.5 (feedparser sharp edges) and §7.2 (charset sniffing order).
"""

from __future__ import annotations

import re

import feedparser

from rssd.models import ContentOrigin, FeedMeta, IdBasis, ParsedEntry, ParsedFeed
from rssd.timeutil import parse_feed_date

# Module-level globals in feedparser itself -- there is no per-call way to
# pass these, so they must be set once at import time. SANITIZE_HTML=0 keeps
# feedparser from stripping/rewriting markup (semantic.py wants the original);
# RESOLVE_RELATIVE_URIS=0 keeps it from silently resolving relative hrefs
# against a base URL it guesses -- rssd does that resolution itself, using
# xml:base -> entry link -> feed link (SPEC §7.2).
feedparser.SANITIZE_HTML = 0
feedparser.RESOLVE_RELATIVE_URIS = 0


# ── charset sniffing (SPEC §7.2) ─────────────────────────────────────────────

_XML_DECL_RE = re.compile(
    rb"""<\?xml[^>]*\bencoding\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE
)
_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([^"';\s]+)""", re.IGNORECASE)


def sniff_charset(body: bytes, content_type: str | None) -> str:
    """Order: XML declaration ``encoding=`` in the first 1024 bytes -> HTTP
    ``Content-Type; charset=`` -> utf-8."""
    match = _XML_DECL_RE.search(body[:1024])
    if match:
        try:
            candidate = match.group(1).decode("ascii", "ignore").strip()
        except Exception:
            candidate = ""
        if candidate:
            return candidate
    if content_type:
        match = _CHARSET_RE.search(content_type)
        if match:
            return match.group(1).strip()
    return "utf-8"


def decode_body(body: bytes, content_type: str | None) -> str:
    """Decode bytes using the sniffed charset. Never decodes as UTF-8
    unconditionally -- falls back to utf-8 with replacement only if the
    sniffed codec itself is unusable."""
    charset = sniff_charset(body, content_type)
    try:
        return body.decode(charset, errors="replace")
    except (LookupError, TypeError):
        return body.decode("utf-8", errors="replace")


# ── content precedence (SPEC §10.5, §6) ──────────────────────────────────────


def _extract_content(entry: dict) -> tuple[str | None, ContentOrigin]:
    """Atom content -> content:encoded -> Atom summary / RSS description ->
    None. feedparser folds both atom:content and content:encoded into the
    same ``content`` list, so both collapse to origin "feed:content"; Atom
    summary and RSS description both land in ``summary``, collapsing to
    "feed:summary". Whatever the feed hands us is used as-is -- distinguishing
    "real" prose from metadata-only description text (e.g. hnrss's
    "Article URL: ... / Points: ... " footer) is not this module's job; it is
    still the feed's own content, just thin. A feed that wants better body
    text for entries like that opts into <fulltext>true</fulltext>."""
    content_list = entry.get("content")
    if content_list:
        value = content_list[0].get("value")
        if value and value.strip():
            return value, "feed:content"
    summary = entry.get("summary")
    if summary and summary.strip():
        return summary, "feed:summary"
    return None, "none"


def _entry_base_url(entry: dict) -> str | None:
    base = entry.get("base") or None
    content_list = entry.get("content")
    if content_list:
        content_base = content_list[0].get("base")
        if content_base:
            return content_base
    summary_detail = entry.get("summary_detail")
    if summary_detail and summary_detail.get("base"):
        return summary_detail["base"]
    return base or None


def _derive_basis(feed_version: str, entry: dict) -> tuple[str, IdBasis]:
    """SPEC §9.1 steps 1-2 only -- the cheap ones feedparser hands us
    directly. Steps 3-4 (link, content) are identity.py's job
    (``derive_raw_id``); until that runs we mark the entry unresolved with
    an empty raw_id and a placeholder basis that is neither "atom-id" nor
    "guid", so ``derive_raw_id`` knows to keep looking."""
    raw = entry.get("id")
    if raw:
        if feed_version.startswith("atom"):
            return raw, "atom-id"
        return raw, "guid"
    return "", "link"


# ── ttl (SPEC §10.2) ──────────────────────────────────────────────────────────

_SY_PERIOD_SECONDS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 7 * 86400,
    "monthly": 30 * 86400,
    "yearly": 365 * 86400,
}


def _ttl_seconds(feed: dict) -> int | None:
    ttl = feed.get("ttl")
    if ttl:
        try:
            return int(float(ttl)) * 60
        except (TypeError, ValueError):
            pass
    period = feed.get("sy_updateperiod")
    if period:
        base = _SY_PERIOD_SECONDS.get(period.strip().lower())
        if base:
            freq_raw = feed.get("sy_updatefrequency")
            try:
                freq = int(float(freq_raw)) if freq_raw else 1
            except (TypeError, ValueError):
                freq = 1
            freq = max(freq, 1)
            return max(base // freq, 1)
    return None


def parse_feed(body: bytes, content_type: str | None, feed_url: str) -> ParsedFeed:
    """bytes -> ParsedFeed. Never raises -- feedparser's ``bozo`` flag is
    recorded, never treated as fatal (SPEC §10.5)."""
    text = decode_body(body, content_type)
    parsed = feedparser.parse(text)

    feed = parsed.get("feed", {})
    meta = FeedMeta(
        title=feed.get("title") or None,
        link=feed.get("link") or feed_url,
        description=feed.get("subtitle") or feed.get("description") or None,
        language=feed.get("language") or None,
        ttl_seconds=_ttl_seconds(feed),
    )

    version = parsed.get("version") or ""

    entries = []
    for raw_entry in parsed.get("entries", ()):
        raw_id, basis = _derive_basis(version, raw_entry)

        published = parse_feed_date(
            raw_entry.get("published_parsed") or raw_entry.get("published")
        )
        updated = parse_feed_date(
            raw_entry.get("updated_parsed") or raw_entry.get("updated")
        )

        content_html, content_origin = _extract_content(raw_entry)

        author = None
        author_detail = raw_entry.get("author_detail")
        if author_detail and author_detail.get("name"):
            author = author_detail["name"]
        elif raw_entry.get("author"):
            author = raw_entry["author"]

        categories = tuple(
            tag["term"]
            for tag in raw_entry.get("tags", ()) or ()
            if tag.get("term")
        )

        entries.append(
            ParsedEntry(
                raw_id=raw_id,
                basis=basis,
                title=raw_entry.get("title") or "",
                link=raw_entry.get("link") or None,
                author=author,
                published=published,
                updated=updated,
                categories=categories,
                content_html=content_html,
                content_origin=content_origin,
                base_url=_entry_base_url(raw_entry),
            )
        )

    bozo = bool(parsed.get("bozo"))
    bozo_exc = parsed.get("bozo_exception")
    bozo_message = str(bozo_exc) if bozo_exc is not None else None

    return ParsedFeed(
        meta=meta,
        entries=tuple(entries),
        bozo=bozo,
        bozo_message=bozo_message,
    )
