"""Tests for rssd.diff -- classification and the three churn guards of
SPEC §9.4."""

from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from rssd.config import Limits
from rssd.diff import detect_rotating_guids, diff_feed, revisions_today
from rssd.models import EntryRecord, ParsedEntry, PreparedEntry

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_prepared(entry_id: str, content_hash: str, title: str = "Title") -> PreparedEntry:
    parsed = ParsedEntry(raw_id=entry_id, basis="guid", title=title)
    return PreparedEntry(
        parsed=parsed,
        id=entry_id,
        id8=entry_id[:8],
        content=etree.Element("content"),
        content_hash=content_hash,
        published=NOW,
        published_origin="feed",
    )


def make_record(entry_id: str, content_hash: str, base_name: str | None = None) -> EntryRecord:
    return EntryRecord(
        id=entry_id,
        id8=entry_id[:8],
        base_name=base_name or f"20260907T000000Z-{entry_id[:8]}-slug",
        revision=1,
        content_hash=content_hash,
        first_seen=NOW,
    )


LIMITS = Limits()


# ── classification ───────────────────────────────────────────────────────


def test_unseen_id_is_new():
    prepared = [make_prepared("id-1", "hash-1")]
    diff = diff_feed(prepared, {}, poll_count=5, limits=LIMITS)
    assert len(diff.new) == 1
    assert diff.new[0].id == "id-1"
    assert diff.revised == ()
    assert diff.unchanged == 0
    assert diff.anomaly is None


def test_seen_id_same_hash_is_unchanged_no_write():
    prepared = [make_prepared("id-1", "hash-1")]
    seen = {"id-1": make_record("id-1", "hash-1")}
    diff = diff_feed(prepared, seen, poll_count=5, limits=LIMITS)
    assert diff.new == ()
    assert diff.revised == ()
    assert diff.unchanged == 1
    assert diff.anomaly is None


def test_seen_id_different_hash_is_revised():
    prepared = [make_prepared("id-1", "hash-2")]
    seen = {"id-1": make_record("id-1", "hash-1")}
    diff = diff_feed(prepared, seen, poll_count=5, limits=LIMITS)
    assert diff.new == ()
    assert len(diff.revised) == 1
    entry, record = diff.revised[0]
    assert entry.id == "id-1"
    assert record.content_hash == "hash-1"
    assert diff.unchanged == 0


def test_mixed_classification():
    prepared = [
        make_prepared("new-1", "h-new"),
        make_prepared("same-1", "h-same"),
        make_prepared("changed-1", "h-changed-new"),
    ]
    seen = {
        "same-1": make_record("same-1", "h-same"),
        "changed-1": make_record("changed-1", "h-changed-old"),
    }
    diff = diff_feed(prepared, seen, poll_count=5, limits=LIMITS)
    assert [e.id for e in diff.new] == ["new-1"]
    assert [e.id for e, _ in diff.revised] == ["changed-1"]
    assert diff.unchanged == 1
    assert diff.anomaly is None


# ── anomaly cap ───────────────────────────────────────────────────────────


def test_anomaly_fires_when_new_entries_swamp_the_poll():
    limits = Limits(max_new_entries_per_poll=10)
    # A feed we already know about suddenly claims far more new entries than
    # the cap allows.
    seen = {"known": make_record("known", "hash-known")}
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(15)]
    diff = diff_feed(prepared, seen, poll_count=5, limits=limits)
    assert diff.anomaly is not None
    assert diff.new == () and diff.revised == ()


def test_first_poll_adopts_the_whole_window_however_large():
    """An empty `seen` means we have never polled this feed. Adopting its
    entire window is the point of subscribing, not an anomaly."""
    limits = Limits(max_new_entries_per_poll=10)
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(500)]
    diff = diff_feed(prepared, {}, poll_count=1, limits=limits)
    assert diff.anomaly is None
    assert len(diff.new) == 500


def test_large_feed_does_not_deadlock_across_polls():
    """Regression: an anomaly writes nothing, so if the guard fired on an empty
    `seen` the feed could never populate it and would trip forever."""
    limits = Limits(max_new_entries_per_poll=10)
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(200)]

    first = diff_feed(prepared, {}, poll_count=1, limits=limits)
    assert first.anomaly is None

    # Simulate the caller having written that first batch.
    seen = {e.id: make_record(e.id, e.content_hash) for e in first.new}
    second = diff_feed(prepared, seen, poll_count=2, limits=limits)
    assert second.anomaly is None
    assert second.unchanged == 200


def test_anomaly_does_not_fire_for_a_normal_small_poll():
    limits = Limits(max_new_entries_per_poll=100)
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(5)]
    diff = diff_feed(prepared, {}, poll_count=5, limits=limits)
    assert diff.anomaly is None
    assert len(diff.new) == 5


def test_anomaly_does_not_fire_when_most_entries_already_seen():
    limits = Limits(max_new_entries_per_poll=10)
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(15)]
    # Seed `seen` with 13 of the 15 ids already tracked, unchanged.
    seen = {e.id: make_record(e.id, e.content_hash) for e in prepared[:13]}
    diff = diff_feed(prepared, seen, poll_count=5, limits=limits)
    assert diff.anomaly is None
    assert len(diff.new) == 2
    assert diff.unchanged == 13


# ── rotating-guid detection ────────────────────────────────────────────────


def test_rotating_guid_detection_fires_on_synthetic_churny_feed():
    limits = Limits(churn_min_polls=2, churn_new_ratio=0.8, churn_hash_overlap=0.5)
    # `seen` holds entries under their old (now-rotated) ids, but with the
    # SAME content hashes the "new" entries carry -- i.e. content is stable,
    # only the id churns.
    seen = {f"old-id-{i}": make_record(f"old-id-{i}", f"stable-hash-{i}") for i in range(10)}
    prepared = [make_prepared(f"new-id-{i}", f"stable-hash-{i}") for i in range(10)]
    fired = detect_rotating_guids(prepared, seen, poll_count=3, limits=limits)
    assert fired is True

    diff = diff_feed(prepared, seen, poll_count=3, limits=limits)
    assert diff.downgrade_identity is True
    assert diff.anomaly is None


def test_rotating_guid_detection_does_not_fire_on_normal_feed():
    limits = Limits(churn_min_polls=2, churn_new_ratio=0.8, churn_hash_overlap=0.5)
    seen = {f"id-{i}": make_record(f"id-{i}", f"hash-{i}") for i in range(10)}
    # Mostly unchanged, one genuinely new entry with a genuinely new hash.
    prepared = [make_prepared(f"id-{i}", f"hash-{i}") for i in range(9)]
    prepared.append(make_prepared("id-new", "hash-new"))
    fired = detect_rotating_guids(prepared, seen, poll_count=3, limits=limits)
    assert fired is False

    diff = diff_feed(prepared, seen, poll_count=3, limits=limits)
    assert diff.downgrade_identity is False


def test_rotating_guid_detection_respects_min_polls():
    limits = Limits(churn_min_polls=5, churn_new_ratio=0.8, churn_hash_overlap=0.5)
    seen = {f"old-id-{i}": make_record(f"old-id-{i}", f"stable-hash-{i}") for i in range(10)}
    prepared = [make_prepared(f"new-id-{i}", f"stable-hash-{i}") for i in range(10)]
    # poll_count below churn_min_polls -- must not fire yet.
    assert detect_rotating_guids(prepared, seen, poll_count=1, limits=limits) is False


def test_rotating_guid_detection_empty_feed_no_division_by_zero():
    assert detect_rotating_guids([], {}, poll_count=10, limits=LIMITS) is False


def test_rotating_guid_detection_skipped_once_identity_already_overridden():
    limits = Limits(churn_min_polls=2, churn_new_ratio=0.8, churn_hash_overlap=0.5)
    seen = {f"old-id-{i}": make_record(f"old-id-{i}", f"stable-hash-{i}") for i in range(10)}
    prepared = [make_prepared(f"new-id-{i}", f"stable-hash-{i}") for i in range(10)]
    diff = diff_feed(
        prepared, seen, poll_count=3, limits=limits, identity_override="content"
    )
    assert diff.downgrade_identity is False


# ── revisions_today ─────────────────────────────────────────────────────


def test_revisions_today_counts_same_date():
    log = {
        "id-1": [
            "2026-09-15T01:00:00Z",
            "2026-09-15T14:30:00Z",
            "2026-09-14T23:59:59Z",
        ]
    }
    assert revisions_today(log, "id-1", "2026-09-15T18:00:00Z") == 2


def test_revisions_today_missing_entry_is_zero():
    assert revisions_today({}, "missing", "2026-09-15T18:00:00Z") == 0
