"""Shared data contracts.

Every module in rssd exchanges these types and nothing else. They are defined in
one place, deliberately, so that the pure pipeline modules can be written and
tested independently of each other.

Pipeline shape:

    bytes ──parse──> ParsedFeed ──semantic+identity──> PreparedEntry
          ──diff──> FeedDiff ──render──> bytes ──store──> disk
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from lxml import etree

# ── enumerations ────────────────────────────────────────────────────────────

#: Where an entry's publication timestamp came from. Surfaced as
#: ``<published origin="...">`` so readers can tell a real date from a guess.
DateOrigin = Literal["feed", "first-seen", "clamped"]

#: Where an entry's body came from. Surfaced as ``<content origin="...">``.
ContentOrigin = Literal["feed:content", "feed:summary", "fulltext:trafilatura", "none"]

#: Which field the stable entry ID was derived from. See SPEC §9.1.
IdBasis = Literal["atom-id", "guid", "link", "content"]

#: Feed health, surfaced in status.xml.
Health = Literal["pending", "ok", "degraded", "failing", "retired"]

#: Classification of a failed fetch, used to decide retryability.
ErrorKind = Literal["dns", "tls", "timeout", "http", "parse", "too-large", "network"]


# ── input ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Subscription:
    """One parsed file from ``feeds.d/``."""

    name: str
    url: str
    source_path: Path
    interval: int | None = None
    fulltext: bool = False


# ── parsing ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedMeta:
    """Feed-level metadata. Lands in ``feed.xml``."""

    title: str | None = None
    link: str | None = None
    description: str | None = None
    language: str | None = None
    ttl_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """One entry straight out of feedparser, before identity or normalisation.

    ``content_html`` is the raw markup as the feed supplied it -- feedparser's
    own sanitiser and relative-URI resolver are disabled so that semantic.py
    sees the original. ``base_url`` carries Atom's ``xml:base`` when present.
    """

    raw_id: str
    basis: IdBasis
    title: str
    link: str | None = None
    author: str | None = None
    published: datetime | None = None
    updated: datetime | None = None
    categories: tuple[str, ...] = ()
    content_html: str | None = None
    content_origin: ContentOrigin = "none"
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    meta: FeedMeta
    entries: tuple[ParsedEntry, ...]
    #: feedparser sets this on malformed input but still returns usable data.
    #: Log it, never abort on it.
    bozo: bool = False
    bozo_message: str | None = None


# ── preparation (identity + semantic content resolved) ──────────────────────


@dataclass(frozen=True, slots=True)
class PreparedEntry:
    """A ParsedEntry with its stable ID and semantic content resolved.

    ``content`` is an lxml ``<content>`` element in the rssd namespace, already
    mapped to the semantic vocabulary. ``content_hash`` is computed over its
    canonical serialisation, never over the source HTML -- see SPEC §9.2.
    """

    parsed: ParsedEntry
    id: str
    id8: str
    content: etree._Element
    content_hash: str
    published: datetime
    published_origin: DateOrigin


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Feed-level facts render.py needs that aren't on the entry itself."""

    feed_name: str
    feed_url: str
    feed_title: str | None = None


# ── store ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntryRecord:
    """What the store knows about an entry already on disk.

    Reconstructible entirely by scanning ``entries/`` -- which is what makes
    ``var/`` disposable (SPEC invariant I1).
    """

    id: str
    id8: str
    #: Filename without the ``.rN.xml`` suffix, e.g.
    #: ``20260907T000000Z-9f2c1a3b-rust-debugging-survey``
    base_name: str
    revision: int
    content_hash: str
    first_seen: datetime


# ── diffing ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FeedDiff:
    """Result of comparing a freshly parsed feed against what's on disk."""

    new: tuple[PreparedEntry, ...] = ()
    revised: tuple[tuple[PreparedEntry, EntryRecord], ...] = ()
    unchanged: int = 0
    #: Set when a churn guard tripped (SPEC §9.4). When present the caller must
    #: write nothing and emit ``feed.anomaly``.
    anomaly: str | None = None
    #: True when rotating-guid detection fired and the feed should be pinned to
    #: content-based identity from now on.
    downgrade_identity: bool = False


# ── fetching ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one conditional GET.

    ``status`` is 0 for a transport-level failure; check ``error_kind``.
    A 304 carries no body and means "nothing changed, don't parse".
    """

    status: int
    body: bytes | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    max_age: int | None = None
    retry_after: float | None = None
    resolved_url: str | None = None
    error: str | None = None
    error_kind: ErrorKind | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


# ── persisted per-feed state (var/state/<name>.json) ────────────────────────


@dataclass
class FeedState:
    """Daemon-private polling state. A disposable cache -- if this file is lost
    or unparseable, discard it. The cost is one unconditional GET plus a rescan,
    and the rescan is idempotent.
    """

    name: str
    etag: str | None = None
    last_modified: str | None = None
    #: sha256 of the last response body, for feeds that send no validators.
    body_hash: str | None = None
    resolved_url: str | None = None
    failures: int = 0
    next_poll_at: float = 0.0
    poll_count: int = 0
    last_poll_at: str | None = None
    last_error: str | None = None
    health: Health = "pending"
    #: Set once rotating-guid detection fires; sticky thereafter.
    identity_override: IdBasis | None = None
    #: entry id -> ISO timestamps of revisions written, for the daily cap.
    revision_log: dict[str, list[str]] = field(default_factory=dict)
    #: True once feed.health-changed has been emitted for the current health,
    #: so failures are reported edge-triggered rather than every poll.
    health_reported: bool = False
