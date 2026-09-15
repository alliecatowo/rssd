from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rssd.config import Config, TMP_PREFIX
from rssd.store import FeedStore, atomic_symlink, atomic_write, sweep_temps


def make_entry_bytes(
    *, entry_id: str, revision: int, first_seen: str, content_hash: str
) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="https://rssd.dev/entry/1"
       id="{entry_id}" revision="{revision}"
       first-seen="{first_seen}" updated="{first_seen}">
  <source>
    <link>https://example.com/a</link>
    <feed name="example" title="Example">https://example.com/feed.xml</feed>
    <guid basis="atom-id">{entry_id}</guid>
  </source>
  <title>Some title</title>
  <author>Someone</author>
  <published origin="feed">{first_seen}</published>
  <categories></categories>
  <content origin="feed:content" hash="{content_hash}">
    <paragraph>Body text here.</paragraph>
  </content>
</entry>
""".encode("utf-8")


BASE = "20260907T000000Z-9f2c1a3b-rust-debugging-survey"


# ── atomic_write ─────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_visible_temp(tmp_path: Path) -> None:
    dest = tmp_path / "file.xml"
    atomic_write(dest, b"hello world")
    names = os.listdir(tmp_path)
    assert names == ["file.xml"]
    assert dest.read_bytes() == b"hello world"


def test_atomic_write_destination_absent_or_complete(tmp_path: Path) -> None:
    dest = tmp_path / "file.xml"
    atomic_write(dest, b"x" * 1000)
    assert dest.exists()
    assert len(dest.read_bytes()) == 1000
    for name in os.listdir(tmp_path):
        assert not name.startswith(TMP_PREFIX)


def test_atomic_write_temp_name_shape(tmp_path: Path, monkeypatch) -> None:
    # Verify the temp file, while it (briefly) exists, would fail both a
    # *.xml glob and a find -name '*.xml' — i.e. it doesn't end in .xml and
    # starts with a dot.
    import tempfile as tempfile_mod

    seen_names: list[str] = []
    real_mkstemp = tempfile_mod.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen_names.append(os.path.basename(name))
        return fd, name

    monkeypatch.setattr(tempfile_mod, "mkstemp", spy_mkstemp)
    dest = tmp_path / "out.xml"
    atomic_write(dest, b"data")
    assert len(seen_names) == 1
    name = seen_names[0]
    assert name.startswith(".")
    assert not name.endswith(".xml")
    assert name.startswith(TMP_PREFIX)


# ── concurrency: writer hammering, reader never sees partial/torn data ────


def test_concurrent_write_read_never_partial(tmp_path: Path) -> None:
    dest = tmp_path / "hot.xml"
    payload_a = b"A" * 50_000
    payload_b = b"B" * 70_003
    atomic_write(dest, payload_a)

    stop = threading.Event()
    errors: list[str] = []

    def writer() -> None:
        toggle = True
        while not stop.is_set():
            atomic_write(dest, payload_a if toggle else payload_b)
            toggle = not toggle

    def reader() -> None:
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            try:
                data = dest.read_bytes()
            except FileNotFoundError:
                errors.append("file vanished")
                continue
            if data not in (payload_a, payload_b):
                errors.append(f"torn read of length {len(data)}")
            # A temp file may legitimately exist transiently between
            # mkstemp() and os.replace() -- that's fine. What must NEVER
            # happen is a temp file being visible to a plain `ls` (dotfiles
            # hidden) or to a `*.xml` glob.
            for name in os.listdir(tmp_path):
                if name.startswith(TMP_PREFIX):
                    assert name.startswith(".")
                    assert not name.endswith(".xml")
            import glob as glob_mod

            for match in glob_mod.glob(str(tmp_path / "*.xml")):
                assert not Path(match).name.startswith(TMP_PREFIX)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    writer_thread.join()
    for t in threads:
        t.join()

    assert errors == []


# ── FeedStore.scan() ───────────────────────────────────────────────────────


def test_scan_round_trip_and_highest_revision_wins(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.ensure_dirs()

    id1 = "sha256:9f2c1a3b0000000000000000000000000000000000000000000000000000"
    base1 = "20260907T000000Z-9f2c1a3b-rust-debugging-survey"
    store.write_entry(
        base_name=base1,
        data=make_entry_bytes(
            entry_id=id1,
            revision=1,
            first_seen="2026-09-07T00:00:00Z",
            content_hash="sha256:aaa",
        ),
        revision=1,
    )
    store.write_entry(
        base_name=base1,
        data=make_entry_bytes(
            entry_id=id1,
            revision=2,
            first_seen="2026-09-07T00:00:00Z",
            content_hash="sha256:bbb",
        ),
        revision=2,
    )

    id2 = "sha256:deadbeef0000000000000000000000000000000000000000000000000000"
    base2 = "20260908T010203Z-deadbeef-other-post"
    store.write_entry(
        base_name=base2,
        data=make_entry_bytes(
            entry_id=id2,
            revision=1,
            first_seen="2026-09-08T01:02:03Z",
            content_hash="sha256:ccc",
        ),
        revision=1,
    )

    records = store.scan()
    assert set(records) == {id1, id2}

    r1 = records[id1]
    assert r1.base_name == base1
    assert r1.revision == 2
    assert r1.content_hash == "sha256:bbb"
    assert r1.id8 == "9f2c1a3b"
    assert r1.first_seen == datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)

    r2 = records[id2]
    assert r2.base_name == base2
    assert r2.revision == 1
    assert r2.content_hash == "sha256:ccc"

    assert store.entry_count() == 2


def test_scan_survives_garbage_file(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.ensure_dirs()

    id1 = "sha256:9f2c1a3b0000000000000000000000000000000000000000000000000000"
    base1 = "20260907T000000Z-9f2c1a3b-rust-debugging-survey"
    store.write_entry(
        base_name=base1,
        data=make_entry_bytes(
            entry_id=id1,
            revision=1,
            first_seen="2026-09-07T00:00:00Z",
            content_hash="sha256:aaa",
        ),
        revision=1,
    )

    (store.entries_dir / "not-an-entry.txt").write_bytes(b"nonsense")
    (store.entries_dir / "malformed.r1.xml").write_bytes(b"<not id or first-seen>")
    (store.entries_dir / "weird-name.xml").write_bytes(b"<entry/>")

    records = store.scan()
    assert set(records) == {id1}


def test_sweep_temps_removes_old_leaves_fresh(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.ensure_dirs()

    old = store.entries_dir / f"{TMP_PREFIX}old123"
    fresh = store.entries_dir / f"{TMP_PREFIX}fresh456"
    old.write_bytes(b"x")
    fresh.write_bytes(b"y")

    old_time = time.time() - 120
    os.utime(old, (old_time, old_time))

    removed = sweep_temps(tmp_path, older_than=60.0)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_symlink_resolves_to_newest_revision(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.ensure_dirs()

    entry_id = "sha256:9f2c1a3b0000000000000000000000000000000000000000000000000000"
    base = "20260907T000000Z-9f2c1a3b-rust-debugging-survey"

    for rev in (1, 2, 3):
        store.write_entry(
            base_name=base,
            data=make_entry_bytes(
                entry_id=entry_id,
                revision=rev,
                first_seen="2026-09-07T00:00:00Z",
                content_hash=f"sha256:rev{rev}",
            ),
            revision=rev,
        )
        link = store.entries_dir / f"{base}.xml"
        assert link.is_symlink()
        target = os.readlink(link)
        assert target == f"{base}.r{rev}.xml"
        resolved = (store.entries_dir / target).read_bytes()
        assert f"sha256:rev{rev}".encode() in resolved


def test_atomic_symlink_low_level(tmp_path: Path) -> None:
    target_a = tmp_path / "a.xml"
    target_b = tmp_path / "b.xml"
    target_a.write_bytes(b"A")
    target_b.write_bytes(b"B")
    link = tmp_path / "current.xml"

    atomic_symlink(link, "a.xml")
    assert link.resolve() == target_a
    atomic_symlink(link, "b.xml")
    assert link.resolve() == target_b
    for name in os.listdir(tmp_path):
        assert not name.startswith(TMP_PREFIX)


# ── write_feed_xml change-guard ─────────────────────────────────────────────


def test_write_feed_xml_returns_false_when_unchanged(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")

    assert store.write_feed_xml(b"<feed>1</feed>") is True
    assert store.write_feed_xml(b"<feed>1</feed>") is False
    assert store.write_feed_xml(b"<feed>2</feed>") is True
    assert config.feed_xml("example").read_bytes() == b"<feed>2</feed>"


def test_write_status_xml_always_writes(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.write_status_xml(b"<status>1</status>")
    store.write_status_xml(b"<status>1</status>")
    assert config.status_xml("example").read_bytes() == b"<status>1</status>"


def test_fsync_entries_and_batched_write(tmp_path: Path) -> None:
    config = Config(root=tmp_path)
    store = FeedStore(config, "example")
    store.ensure_dirs()
    entry_id = "sha256:9f2c1a3b0000000000000000000000000000000000000000000000000000"
    base = "20260907T000000Z-9f2c1a3b-rust-debugging-survey"
    store.write_entry(
        base_name=base,
        data=make_entry_bytes(
            entry_id=entry_id,
            revision=1,
            first_seen="2026-09-07T00:00:00Z",
            content_hash="sha256:aaa",
        ),
        revision=1,
        fsync=False,
    )
    # Should not raise, and directory should be fsync-able.
    store.fsync_entries()
    records = store.scan()
    assert entry_id in records
