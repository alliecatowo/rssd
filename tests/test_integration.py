"""End-to-end tests over a real HTTP server and a real filesystem.

These are the demo, encoded. They exercise the whole stack -- fetch, conditional
GET, parse, semantic mapping, atomic writes, revisions, symlinks, the event log
-- without ever touching the network, by serving the committed fixtures from
localhost. If these pass, the demo works on a plane.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rssd import fixtures
from rssd.cli import MUTABLE_FIXTURE, subscription_xml
from rssd.config import Config
from rssd.daemon import run_once

FIXTURE_FEEDS = ["lobsters", "rust-blog", "simonw", "hn", "xkcd", "godev"]

#: The six reference feeds plus the editable one. Pinned so that a fixture
#: silently losing entries shows up as a failure here rather than as a vague
#: "looks about right" during a demo.
EXPECTED_ENTRIES = 100


@pytest.fixture(scope="module")
def server():
    srv, _thread = fixtures.serve_in_thread(port=0, directory=fixtures.FIXTURE_DIR)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def root(tmp_path: Path, server: str) -> Path:
    """An instance whose subscriptions point at this test's private copy of the
    fixtures, so a mutation in one test cannot leak into another."""
    config = Config(root=tmp_path)
    config.ensure_dirs()
    served = tmp_path / "served"
    served.mkdir()
    for name in FIXTURE_FEEDS:
        shutil.copy2(fixtures.FIXTURE_DIR / f"{name}.xml", served / f"{name}.xml")
        (config.feeds_d / f"{name}.xml").write_text(
            subscription_xml(name, f"{server}/{name}.xml", name)
        )
    (served / "mutable.xml").write_text(MUTABLE_FIXTURE.format(n=1))
    (config.feeds_d / "mutable.xml").write_text(
        subscription_xml("mutable", f"{server}/mutable.xml", "editable")
    )
    fixtures.FixtureHandler.directory = served
    return tmp_path


def events(config: Config) -> list[dict]:
    if not config.events_path.exists():
        return []
    return [
        json.loads(line)
        for line in config.events_path.read_text().splitlines()
        if line.strip()
    ]


def entry_files(config: Config) -> list[Path]:
    return sorted(config.store.glob("*/entries/*.r*.xml"))


def snapshot(config: Config) -> dict[str, tuple[int, int]]:
    return {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in entry_files(config)
    }


async def test_first_poll_materialises_every_feed(root: Path, server: str):
    config = Config(root=root)
    assert await run_once(config) == 0

    names = {p.name for p in config.store.iterdir()}
    assert names == set(FIXTURE_FEEDS) | {"mutable"}

    files = entry_files(config)
    assert len(files) == EXPECTED_ENTRIES, f"got {len(files)}"

    created = [e for e in events(config) if e["event"] == "entry.new"]
    assert len(created) == EXPECTED_ENTRIES
    assert not [e for e in events(config) if e["event"] == "feed.poll-failed"]


async def test_every_written_file_is_well_formed_xml(root: Path):
    """Invariant I3, checked with an external parser rather than our own."""
    config = Config(root=root)
    await run_once(config)

    targets = [str(p) for p in entry_files(config)]
    targets += [str(p) for p in config.store.glob("*/feed.xml")]
    targets += [str(p) for p in config.store.glob("*/status.xml")]
    assert targets

    if shutil.which("xmllint"):
        result = subprocess.run(
            ["xmllint", "--noout", *targets], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr[:2000]
    else:  # pragma: no cover
        from lxml import etree

        for target in targets:
            etree.parse(target)


async def test_unchanged_poll_writes_nothing(root: Path):
    """Invariant I2. If this regresses, mtime stops meaning anything to readers
    and -- because rssd keeps every revision -- the tree would grow a new .rN
    for every entry on every poll."""
    config = Config(root=root)
    await run_once(config)
    before = snapshot(config)

    await run_once(config)

    assert snapshot(config) == before, "an unchanged poll rewrote files"
    assert any(e["event"] == "feed.unchanged" for e in events(config))


async def test_changed_content_adds_a_revision_and_repoints_the_symlink(root: Path):
    config = Config(root=root)
    await run_once(config)

    entries = config.store / "mutable" / "entries"
    assert len(list(entries.glob("*.r*.xml"))) == 1
    link = next(p for p in entries.iterdir() if p.is_symlink())
    assert link.resolve().name.endswith(".r1.xml")
    r1_bytes = link.resolve().read_bytes()

    (root / "served" / "mutable.xml").write_text(MUTABLE_FIXTURE.format(n=2))
    await run_once(config)

    revisions = sorted(p.name for p in entries.glob("*.r*.xml"))
    assert len(revisions) == 2, revisions
    assert revisions[0].endswith(".r1.xml")
    assert revisions[1].endswith(".r2.xml")
    assert link.resolve().name.endswith(".r2.xml")
    # Nothing is ever mutated: the old revision is preserved byte for byte.
    assert (entries / revisions[0]).read_bytes() == r1_bytes
    assert any(e["event"] == "entry.revised" for e in events(config))


async def test_tree_alone_survives_losing_var(root: Path):
    """Invariant I1: var/ is a disposable cache. After deleting it entirely, a
    fresh poll must recognise every entry already on disk and write nothing."""
    config = Config(root=root)
    await run_once(config)
    before = snapshot(config)

    shutil.rmtree(config.var)
    await run_once(config)

    assert snapshot(config) == before
    fresh = events(config)
    assert not [e for e in fresh if e["event"] == "entry.new"]
    assert not [e for e in fresh if e["event"] == "entry.revised"]


async def test_no_temp_file_ever_survives_a_run(root: Path):
    config = Config(root=root)
    await run_once(config)
    assert list(root.rglob(".rssd-tmp-*")) == []


async def test_entry_files_are_world_readable(root: Path):
    """The whole premise is that other programs read this tree."""
    config = Config(root=root)
    await run_once(config)
    for path in entry_files(config)[:20]:
        assert path.stat().st_mode & 0o044, f"{path} is not readable by others"


async def test_thin_and_rich_feeds_both_produce_valid_content(root: Path):
    """rust-blog carries full article HTML; xkcd's body is a single image.
    Both must come out as well-formed semantic content."""
    config = Config(root=root)
    await run_once(config)

    rust = next((config.store / "rust-blog" / "entries").glob("*.r1.xml")).read_text()
    assert "<paragraph>" in rust
    assert 'origin="feed:content"' in rust
    # Relative links in the source must have been resolved against xml:base.
    assert 'href="http' in rust

    xkcd = next((config.store / "xkcd" / "entries").glob("*.r1.xml")).read_text()
    assert "<image" in xkcd


async def test_event_log_is_ordered_and_paths_are_root_relative(root: Path):
    config = Config(root=root)
    await run_once(config)

    log = events(config)
    assert [e["seq"] for e in log] == sorted(e["seq"] for e in log)
    assert len({e["seq"] for e in log}) == len(log)

    for entry in log:
        path = entry.get("data", {}).get("path")
        if path:
            assert not path.startswith("/"), path
            assert (root / path).exists()
