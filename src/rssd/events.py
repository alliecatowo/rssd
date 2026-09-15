"""JSONL event log, seq recovery, rotation.

SPEC §12: append-only, one JSON object per line, stable key order for
greppability. Each line is written with a single ``os.write()`` to an
``O_APPEND`` fd — the kernel guarantees a single sub-page write is never
interleaved with another writer's, so concurrent emitters can't tear each
other's lines. Never build a line incrementally across multiple writes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rssd.config import Config
from rssd.store import atomic_write, fsync_dir

#: SPEC §12.1 taxonomy, verbatim. emit() asserts membership so a typo in an
#: event name fails loudly in tests instead of silently producing an event
#: nobody is listening for.
EVENTS: frozenset[str] = frozenset(
    {
        "daemon.started",
        "daemon.stopping",
        "daemon.reconciled",
        "subscription.added",
        "subscription.changed",
        "subscription.removed",
        "subscription.invalid",
        "feed.created",
        "feed.retired",
        "feed.redirected",
        "feed.poll-started",
        "feed.poll-succeeded",
        "feed.unchanged",
        "feed.poll-failed",
        "feed.throttled",
        "feed.too-large",
        "feed.identity-downgraded",
        "feed.anomaly",
        "feed.health-changed",
    "feed.metadata-changed",
        "entry.new",
        "entry.revised",
        "entry.revision-suppressed",
        "entry.content-degraded",
        "fulltext.ok",
        "fulltext.error",
        "log.rotated",
    }
)


def _format_ts(dt: datetime) -> str:
    """UTC, millisecond precision: ``2026-09-15T21:14:02.140Z``. Formatted
    inline — timeutil.py belongs to another module."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class EventLog:
    """Append-only JSONL event log with monotonic-across-restarts ``seq``."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._fd: int | None = None
        self._seq = 0
        self._checkpointed_seq = 0
        self._lock = threading.Lock()
        self._current_path: Path = config.events_path

    # ── lifecycle ────────────────────────────────────────────────────────

    def open(self) -> None:
        self.config.var.mkdir(parents=True, exist_ok=True)
        self._current_path = self.config.events_path
        self._fd = os.open(
            self._current_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        self._seq = self._recover_seq()
        self._checkpointed_seq = self._seq

    def close(self) -> None:
        if self._fd is not None:
            self._persist_seq(self._seq, force=True)
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "EventLog":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── seq recovery ─────────────────────────────────────────────────────

    def _recover_seq(self) -> int:
        file_seq = 0
        seq_path = self.config.seq_path
        if seq_path.exists():
            try:
                file_seq = int(seq_path.read_text().strip())
            except (OSError, ValueError):
                file_seq = 0
        last_seq = self._last_line_seq(self._current_path)
        return max(file_seq, last_seq)

    @staticmethod
    def _last_line_seq(path: Path) -> int:
        try:
            data = path.read_bytes()
        except OSError:
            return 0
        for line in reversed(data.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                return int(obj.get("seq", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                # A torn/garbage trailing line — skip and try the one before.
                continue
        return 0

    #: How many events may pass between checkpoints of var/seq. This is a
    #: performance valve, not a correctness one: _recover_seq() takes the max
    #: of the checkpoint and the seq on the log's last line, so a stale
    #: checkpoint costs nothing. Without it, writing 100 entries in one poll
    #: would mean 100 fsync'd checkpoint writes on btrfs -- and the events are
    #: emitted after the entry files are already safely on disk, so there is
    #: nothing to protect by flushing each one.
    SEQ_CHECKPOINT_EVERY = 64

    def _persist_seq(self, seq: int, *, force: bool = False) -> None:
        if not force and seq - self._checkpointed_seq < self.SEQ_CHECKPOINT_EVERY:
            return
        atomic_write(self.config.seq_path, str(seq).encode("ascii"), fsync=True)
        self._checkpointed_seq = seq

    # ── serialisation ────────────────────────────────────────────────────

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, Path):
            return self.config.relative(value)
        if isinstance(value, datetime):
            return _format_ts(value)
        if isinstance(value, dict):
            return {str(k): self._sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        # The log must never be the thing that crashes the daemon.
        return str(value)

    def _write_obj(self, obj: dict[str, Any]) -> None:
        assert self._fd is not None, "EventLog.open() was not called"
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        os.write(self._fd, line.encode("utf-8"))

    def _emit_locked(
        self, event: str, feed: str | None, data: dict[str, Any]
    ) -> int:
        self._seq += 1
        seq = self._seq
        obj: dict[str, Any] = {
            "v": 1,
            "seq": seq,
            "ts": _format_ts(datetime.now(timezone.utc)),
            "event": event,
        }
        if feed is not None:
            obj["feed"] = feed
        obj["data"] = {k: self._sanitize(v) for k, v in data.items()}
        self._write_obj(obj)
        self._persist_seq(seq)
        return seq

    # ── public API ───────────────────────────────────────────────────────

    def emit(self, event: str, feed: str | None = None, **data: Any) -> int:
        if event not in EVENTS:
            raise ValueError(f"unknown event name: {event!r}")
        with self._lock:
            seq = self._emit_locked(event, feed, data)
            self._rotate_if_needed_locked()
        return seq

    def rotate_if_needed(self) -> bool:
        with self._lock:
            return self._rotate_if_needed_locked()

    # ── rotation ─────────────────────────────────────────────────────────

    def _rotate_if_needed_locked(self) -> bool:
        if self._fd is None:
            return False
        try:
            size = os.fstat(self._fd).st_size
        except OSError:
            return False
        if size < self.config.limits.event_log_max_bytes:
            return False
        self._do_rotate_locked()
        return True

    def _do_rotate_locked(self) -> None:
        old_path = self._current_path
        # log.rotated is the LAST line of the old file — written before we
        # close it, never via copy-truncate (which can tear a line).
        self._emit_locked("log.rotated", None, {"to": None})

        os.close(self._fd)
        self._fd = None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rotated_path = self.config.var / f"events-{stamp}.jsonl"
        suffix = 1
        while rotated_path.exists():
            rotated_path = self.config.var / f"events-{stamp}-{suffix}.jsonl"
            suffix += 1
        os.replace(old_path, rotated_path)
        fsync_dir(self.config.var)

        self._fd = os.open(
            self._current_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        # log.rotated is also the FIRST line of the new file.
        self._emit_locked(
            "log.rotated", None, {"from": self.config.relative(rotated_path)}
        )

        self._enforce_keep_locked()

    def _enforce_keep_locked(self) -> None:
        keep = self.config.limits.event_log_keep
        rotated = sorted(self.config.var.glob("events-*.jsonl"))
        excess = len(rotated) - keep
        for p in rotated[: max(0, excess)]:
            try:
                p.unlink()
            except OSError:
                pass
