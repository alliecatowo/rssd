"""The seam between parsing and storage.

`parse.py` gives us what the feed said. `semantic.py` and `identity.py` turn
that into something stable enough to key a filename off. This module is the only
place those three meet, which keeps each of them independently testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .config import Limits
from .identity import content_hash, derive_raw_id, entry_id, id8
from .models import IdBasis, ParsedEntry, PreparedEntry
from .semantic import canonical_xml, to_semantic
from .timeutil import resolve_published


def prepare_entry(
    entry: ParsedEntry,
    *,
    feed_name: str,
    feed_link: str | None,
    first_seen: datetime,
    limits: Limits,
    identity_override: IdBasis | None = None,
) -> PreparedEntry:
    raw, basis = derive_raw_id(entry)

    # A feed caught rotating its guids gets pinned to content-derived identity
    # (SPEC §9.4). The override is sticky, so this survives restarts.
    if identity_override == "content" and basis != "content":
        raw = _content_basis_raw(entry)
        basis = "content"

    # xml:base wins over the entry link, which wins over the feed link. The
    # rust-lang feed sets xml:base per entry, so this ordering is load-bearing.
    base = entry.base_url or entry.link or feed_link
    content = to_semantic(entry.content_html, base)
    canonical = canonical_xml(content)

    eid = entry_id(feed_name, basis, raw)
    published, origin = resolve_published(entry.published, first_seen, limits.future_clamp)

    return PreparedEntry(
        parsed=entry,
        id=eid,
        id8=id8(eid),
        content=content,
        content_hash=content_hash(entry.title, entry.author, canonical),
        published=published,
        published_origin=origin,
    )


def prepare_entries(
    entries: Sequence[ParsedEntry],
    *,
    feed_name: str,
    feed_link: str | None,
    first_seen: datetime,
    limits: Limits,
    identity_override: IdBasis | None = None,
) -> list[PreparedEntry]:
    prepared: list[PreparedEntry] = []
    seen_ids: set[str] = set()
    for entry in entries:
        item = prepare_entry(
            entry,
            feed_name=feed_name,
            feed_link=feed_link,
            first_seen=first_seen,
            limits=limits,
            identity_override=identity_override,
        )
        # A feed repeating the same guid twice in one document is not rare.
        # First occurrence wins; the duplicate would otherwise race itself for
        # the same filename.
        if item.id in seen_ids:
            continue
        seen_ids.add(item.id)
        prepared.append(item)
    return prepared


def _content_basis_raw(entry: ParsedEntry) -> str:
    """Rebuild the content-derived identity input for a downgraded feed.

    Mirrors the fallback branch of identity.derive_raw_id so that a feed flipped
    to content identity produces the same IDs it would have produced had it
    never supplied a guid at all.
    """
    from .identity import content_basis_raw

    return content_basis_raw(entry)
