from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from rssd.config import Config
from rssd.events import EVENTS, EventLog


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_emit_rejects_unknown_event(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        with pytest.raises(ValueError):
            log.emit("not.a.real.event")


def test_emit_key_order_and_feed_omission(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        log.emit("daemon.started", data_field=1)
        log.emit("entry.new", feed="rust-blog", id="sha256:abc")

    lines = read_lines(config.events_path)
    assert list(lines[0].keys()) == ["v", "seq", "ts", "event", "data"]
    assert "feed" not in lines[0]
    assert list(lines[1].keys()) == ["v", "seq", "ts", "event", "feed", "data"]
    assert lines[1]["feed"] == "rust-blog"
    assert lines[1]["data"] == {"id": "sha256:abc"}


def test_ordering_and_seq_increments(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        seqs = [log.emit("daemon.started") for _ in range(5)]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, 6))
    lines = read_lines(config.events_path)
    assert [line["seq"] for line in lines] == seqs


def test_ts_format(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        log.emit("daemon.started")
    lines = read_lines(config.events_path)
    ts = lines[0]["ts"]
    assert ts.endswith("Z")
    # e.g. 2026-09-15T21:14:02.140Z
    date_part, time_part = ts[:-1].split("T")
    assert len(date_part) == 10
    assert "." in time_part
    hms, ms = time_part.split(".")
    assert len(hms) == 8
    assert len(ms) == 3


def test_path_value_becomes_root_relative(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        entry_path = config.store / "example" / "entries" / "foo.r1.xml"
        log.emit("entry.new", feed="example", path=entry_path)
    lines = read_lines(config.events_path)
    assert lines[0]["data"]["path"] == "store/example/entries/foo.r1.xml"


def test_unserialisable_value_becomes_str_not_raise(tmp_path: Path) -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-value"

    config = Config(root=tmp_path)
    with EventLog(config) as log:
        log.emit("daemon.started", odd=Weird())
    lines = read_lines(config.events_path)
    assert lines[0]["data"]["odd"] == "weird-value"


def test_concurrent_emit_all_valid_json_unique_seq(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    n_threads = 8
    per_thread = 50

    with EventLog(config) as log:
        errors: list[str] = []

        def worker() -> None:
            for _ in range(per_thread):
                log.emit("daemon.reconciled", n=1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    raw_lines = config.events_path.read_bytes().split(b"\n")
    raw_lines = [ln for ln in raw_lines if ln.strip()]
    assert len(raw_lines) == n_threads * per_thread

    seqs = []
    for ln in raw_lines:
        obj = json.loads(ln)  # must not raise: no torn lines
        seqs.append(obj["seq"])

    assert len(seqs) == len(set(seqs))
    assert sorted(seqs) == list(range(1, n_threads * per_thread + 1))


def test_seq_persists_across_reopen(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        for _ in range(3):
            log.emit("daemon.started")
        last_seq = log.emit("daemon.started")
    assert last_seq == 4

    with EventLog(config) as log2:
        next_seq = log2.emit("daemon.stopping")
    assert next_seq == 5

    lines = read_lines(config.events_path)
    assert [line["seq"] for line in lines] == [1, 2, 3, 4, 5]


def test_seq_persists_even_if_seq_file_missing_but_log_present(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    with EventLog(config) as log:
        log.emit("daemon.started")
        log.emit("daemon.started")
    # Simulate the seq file being lost/corrupted but the log surviving.
    config.seq_path.unlink()

    with EventLog(config) as log2:
        seq = log2.emit("daemon.started")
    assert seq == 3


def test_rotation_writes_marker_in_both_files(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    config.limits.__class__  # sanity: frozen dataclass, just touch attr access
    # Use a tiny max size so a few emits force rotation.
    object.__setattr__(config.limits, "event_log_max_bytes", 200)

    with EventLog(config) as log:
        for i in range(30):
            log.emit("daemon.reconciled", i=i)

    var = config.var
    rotated_files = sorted(var.glob("events-*.jsonl"))
    assert len(rotated_files) >= 1

    old_lines = read_lines(rotated_files[0])
    assert old_lines[-1]["event"] == "log.rotated"

    current_lines = read_lines(config.events_path)
    # First line of the (possibly further-rotated) surviving current file,
    # or of the first rotated file after this one, must be log.rotated.
    all_files = rotated_files + [config.events_path]
    first_lines = [read_lines(p)[0] for p in all_files if read_lines(p)]
    assert any(line["event"] == "log.rotated" for line in first_lines[1:])


def test_rotation_keeps_limited_history(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    object.__setattr__(config.limits, "event_log_max_bytes", 150)
    object.__setattr__(config.limits, "event_log_keep", 2)

    with EventLog(config) as log:
        for i in range(200):
            log.emit("daemon.reconciled", i=i)

    rotated_files = sorted(config.var.glob("events-*.jsonl"))
    assert len(rotated_files) <= 2


def test_rotate_if_needed_manual(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    # Large enough that emit() itself doesn't trigger rotation, but small
    # enough that a manual rotate_if_needed() call does.
    object.__setattr__(config.limits, "event_log_max_bytes", 10_000)
    with EventLog(config) as log:
        log.emit("daemon.started")
        assert log.rotate_if_needed() is False
        object.__setattr__(config.limits, "event_log_max_bytes", 10)
        rotated = log.rotate_if_needed()
        assert rotated is True
    assert len(list(config.var.glob("events-*.jsonl"))) >= 1


def test_events_taxonomy_contains_expected_names() -> None:
    for name in (
        "daemon.started",
        "entry.new",
        "entry.revised",
        "feed.poll-succeeded",
        "log.rotated",
        "fulltext.ok",
    ):
        assert name in EVENTS
    assert "not.a.real.event" not in EVENTS
