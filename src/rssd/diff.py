"""seen + parsed -> new/revised/unchanged, with the churn guards of SPEC §9.4.

PURE. This is the sharpest edge in the whole design: because rssd keeps every
revision, an unstable feed could otherwise spawn ``.r2, .r3, .r4...`` on every
single poll. Three guards live here:

1. ``max_new_entries_per_poll`` -- a broken feed cannot write ten thousand
   files in one poll.
2. Rotating-guid detection -- a feed that rotates its IDs every poll (while
   content stays the same) gets pinned to content-based identity.
3. Classification itself -- unseen id -> new, seen id + same hash ->
   unchanged (no write, no event, SPEC invariant I2), seen id + different
   hash -> revised.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rssd.config import Limits
from rssd.models import EntryRecord, FeedDiff, IdBasis, PreparedEntry


def revisions_today(revision_log: dict[str, list[str]], entry_id: str, now_iso: str) -> int:
    """Count revisions in ``revision_log[entry_id]`` that fall on the same UTC
    calendar date as ``now_iso``, for the daily revision cap.

    Compares ISO date prefixes directly rather than parsing timestamps --
    timeutil.py owns date parsing, not this module.
    """
    today = now_iso[:10]
    timestamps = revision_log.get(entry_id, [])
    return sum(1 for ts in timestamps if ts[:10] == today)


def detect_rotating_guids(
    prepared: Sequence[PreparedEntry],
    seen: Mapping[str, EntryRecord],
    poll_count: int,
    limits: Limits,
) -> bool:
    """SPEC §9.4: after ``churn_min_polls`` polls, if a large fraction of IDs
    are new *and* most of those "new" entries actually match content already
    on disk, the feed is rotating its GUIDs rather than publishing new
    content.
    """
    if poll_count < limits.churn_min_polls:
        return False
    if not prepared:
        return False

    new_entries = [e for e in prepared if e.id not in seen]
    new_ratio = len(new_entries) / len(prepared)
    if new_ratio < limits.churn_new_ratio:
        return False

    if not new_entries:
        return False

    known_hashes = {record.content_hash for record in seen.values()}
    overlap_count = sum(1 for e in new_entries if e.content_hash in known_hashes)
    overlap_ratio = overlap_count / len(new_entries)

    return overlap_ratio > limits.churn_hash_overlap


def diff_feed(
    prepared: Sequence[PreparedEntry],
    seen: Mapping[str, EntryRecord],
    *,
    poll_count: int,
    limits: Limits,
    identity_override: IdBasis | None = None,
) -> FeedDiff:
    """Classify a freshly parsed feed against what's already on disk.

    Guard order: the ``max_new_entries_per_poll`` anomaly check runs first
    and, if tripped, short-circuits everything else -- the caller must write
    nothing. Otherwise entries are classified per SPEC §9.3, and rotating-guid
    detection (skipped once the feed is already pinned to a sticky
    ``identity_override``) decides whether to flip identity basis.
    """
    new_count = sum(1 for e in prepared if e.id not in seen)
    cap = limits.max_new_entries_per_poll

    # The first poll of a feed legitimately adopts its entire window, however
    # large that is -- that is the whole point of subscribing. The cap only
    # protects against a feed that keeps producing implausible numbers of new
    # entries *after* we already know what it looks like.
    #
    # It has to be a flat cap rather than one scaled by the feed's own size:
    # a cap derived from len(prepared) can never be exceeded, since new_count
    # is bounded by it, which would leave this guard as dead code. And it must
    # not fire on an empty `seen`, or a large feed would deadlock -- anomaly
    # writes nothing, so `seen` would stay empty and every later poll would
    # trip the guard again, forever.
    if seen and new_count > cap:
        return FeedDiff(
            anomaly=f"too many new entries in one poll: {new_count} new (cap {cap})"
        )

    new: list[PreparedEntry] = []
    revised: list[tuple[PreparedEntry, EntryRecord]] = []
    unchanged = 0

    for entry in prepared:
        record = seen.get(entry.id)
        if record is None:
            new.append(entry)
        elif record.content_hash == entry.content_hash:
            unchanged += 1
        else:
            revised.append((entry, record))

    downgrade_identity = False
    if identity_override is None:
        downgrade_identity = detect_rotating_guids(prepared, seen, poll_count, limits)

    return FeedDiff(
        new=tuple(new),
        revised=tuple(revised),
        unchanged=unchanged,
        anomaly=None,
        downgrade_identity=downgrade_identity,
    )
