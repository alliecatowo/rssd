"""Root paths, defaults and limits.

Every path in rssd is derived from a single root, so a whole instance is one
directory you can move, tar, or throw away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Feed folder names double as directory names and appear in every event, so
#: they are kept boring on purpose.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

USER_AGENT = "rssd/0.1 (+https://github.com/allie/rssd)"

ACCEPT = (
    "application/atom+xml, application/rss+xml, application/rdf+xml;q=0.9, "
    "application/xml;q=0.8, text/xml;q=0.7, */*;q=0.1"
)

#: XML namespace for entry documents.
NS = "https://rssd.dev/entry/1"

#: Temp files carry BOTH a dot prefix and a non-.xml suffix, so that neither
#: `ls`, nor a `*.xml` glob, nor `find -name '*.xml'` can ever see one.
TMP_PREFIX = ".rssd-tmp-"


@dataclass(frozen=True, slots=True)
class Limits:
    #: Interval bounds. Nothing polls faster than a minute or slower than 6h.
    min_interval: int = 60
    max_interval: int = 6 * 3600
    default_interval: int = 900
    #: Feeds that send no ETag/Last-Modified can't be checked cheaply, so they
    #: are checked less often (SPEC §10.4).
    no_validator_floor: int = 1800

    #: A broken feed must not be able to write ten thousand files mid-demo.
    max_new_entries_per_poll: int = 100
    max_revisions_per_entry_per_day: int = 8
    max_tracked_entries: int = 50_000

    #: Rotating-guid detection thresholds (SPEC §9.4).
    churn_new_ratio: float = 0.8
    churn_hash_overlap: float = 0.5
    churn_min_polls: int = 2

    max_body_bytes: int = 5 * 1024 * 1024
    request_timeout: float = 30.0
    max_redirects: int = 5

    global_concurrency: int = 6
    per_host_concurrency: int = 2

    max_failures_before_failing: int = 10
    backoff_cap: int = 6 * 3600

    event_log_max_bytes: int = 16 * 1024 * 1024
    event_log_keep: int = 10

    #: Startup stagger so the first minute isn't a thundering herd.
    stagger_step: float = 0.75
    stagger_cap: float = 30.0

    #: Debounce for feeds.d/ changes, and the grace window that stops a vim
    #: `:w` (unlink-then-create) from retiring a feed.
    watch_debounce: float = 0.3
    unlink_grace: float = 5.0

    slug_max_len: int = 48
    #: Entries dated further ahead than this are clamped (SPEC §4.2).
    future_clamp: int = 24 * 3600


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    limits: Limits = field(default_factory=Limits)
    #: Collapse the startup stagger; for demos.
    poll_now: bool = False
    #: Point every subscription at the local fixture server instead of the
    #: real internet. Demo insurance.
    fixture_mode: bool = False
    fixture_base: str = "http://127.0.0.1:8765"
    #: Escape hatch for slow filesystems; never use in production.
    no_fsync: bool = False

    # ── derived paths ────────────────────────────────────────────────────

    @property
    def feeds_d(self) -> Path:
        return self.root / "feeds.d"

    @property
    def store(self) -> Path:
        return self.root / "store"

    @property
    def var(self) -> Path:
        return self.root / "var"

    @property
    def state_dir(self) -> Path:
        return self.var / "state"

    @property
    def events_path(self) -> Path:
        return self.var / "events.jsonl"

    @property
    def seq_path(self) -> Path:
        return self.var / "seq"

    @property
    def lock_path(self) -> Path:
        return self.var / "rssd.lock"

    def feed_dir(self, name: str) -> Path:
        return self.store / name

    def entries_dir(self, name: str) -> Path:
        return self.store / name / "entries"

    def feed_xml(self, name: str) -> Path:
        return self.store / name / "feed.xml"

    def status_xml(self, name: str) -> Path:
        return self.store / name / "status.xml"

    def state_path(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def relative(self, path: Path) -> str:
        """Paths in events are always relative to the root, so a consumer can
        resolve them regardless of where the instance lives."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def ensure_dirs(self) -> None:
        for d in (self.feeds_d, self.store, self.var, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))
