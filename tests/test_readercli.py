"""Tests for the `rss` reader CLI.

Builds a real store the same way tests/test_integration.py does: serve the
committed fixtures over localhost, run the daemon's `run_once` against them,
then exercise `rss` (via `main([...])`) against the populated tree.

The one load-bearing assertion beyond "command exits 0" is that `rss` opens
nothing for writing, ever -- checked by snapshotting every file's mtime
before and after a whole battery of commands runs.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from rssd import fixtures
from rssd.cli import MUTABLE_FIXTURE, subscription_xml
from rssd.config import Config
from rssd.daemon import run_once
from rssd.readercli import main
from rssd.reader import feed_names, iter_entry_refs, load_entry

FIXTURE_FEEDS = ["lobsters", "rust-blog", "simonw", "hn", "xkcd", "godev"]


@pytest.fixture(scope="module")
def server():
    srv, _thread = fixtures.serve_in_thread(port=0, directory=fixtures.FIXTURE_DIR)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _populate(tmp_path: Path, server: str, feeds: list[str]) -> Config:
    """Point a fresh instance's subscriptions at the fixture server and run
    one poll pass. Shared by the module-wide read-only store and by the
    private roots the mutation tests build for themselves."""
    config = Config(root=tmp_path)
    config.ensure_dirs()
    served = tmp_path / "served"
    served.mkdir()
    for name in feeds:
        if name == "mutable":
            continue
        shutil.copy2(fixtures.FIXTURE_DIR / f"{name}.xml", served / f"{name}.xml")
        (config.feeds_d / f"{name}.xml").write_text(
            subscription_xml(name, f"{server}/{name}.xml", name)
        )
    if "mutable" in feeds:
        (served / "mutable.xml").write_text(MUTABLE_FIXTURE.format(n=1))
        (config.feeds_d / "mutable.xml").write_text(
            subscription_xml("mutable", f"{server}/mutable.xml", "editable")
        )
    fixtures.FixtureHandler.directory = served
    asyncio.run(run_once(config))
    return config


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory, server: str) -> Path:
    """A store populated once for the whole module. Every test in this file
    except the mutation-diff tests only reads from it -- which is exactly the
    behaviour under test, so one shared, read-only tree keeps the fixture
    server from being hammered by ~30 independent full polls."""
    tmp_path = tmp_path_factory.mktemp("rss-store")
    config = _populate(tmp_path, server, [*FIXTURE_FEEDS, "mutable"])
    assert list(config.store.iterdir()), "fixture poll produced an empty store"
    return tmp_path


def all_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() or p.is_symlink()]


def mtime_snapshot(root: Path) -> dict[str, float]:
    out = {}
    for p in all_files(root):
        try:
            out[str(p)] = p.lstat().st_mtime_ns
        except OSError:
            pass
    return out


def some_entry_id8(root: Path) -> str:
    config = Config(root=root)
    for name in feed_names(config):
        refs = iter_entry_refs(config, name)
        if refs:
            return refs[0].id8
    raise AssertionError("no entries in fixture store")


def entry_with_no_body(root: Path) -> str | None:
    """hn's feed carries titles/links only -- find an entry with content
    origin 'none' to exercise that code path, if one exists."""
    config = Config(root=root)
    for name in feed_names(config):
        for ref in iter_entry_refs(config, name):
            doc = load_entry(ref.path)
            if doc.content_origin == "none":
                return ref.id8
    return None


# ── every command exits 0 on a populated store ──────────────────────────────


def test_feeds(root: Path, capsys):
    assert main(["--root", str(root), "feeds"]) == 0
    out = capsys.readouterr().out
    for name in [*FIXTURE_FEEDS, "mutable"]:
        assert name in out


def test_global_flags_before_subcommand(root: Path, capsys):
    """`rss --root demo feeds` -- global flags in front of the subcommand,
    the position that works if you type the flags first."""
    assert main(["--root", str(root), "--json", "feeds"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {f["name"] for f in data} == {*FIXTURE_FEEDS, "mutable"}


def test_global_flags_after_subcommand(root: Path, capsys):
    """`rss feeds --root demo` -- global flags after the subcommand, matching
    `rssd`'s own convention (`rssd daemon --root demo`). Both positions must
    work identically; a flag declared on both the top-level parser and every
    subparser is a classic argparse trap where the second parse silently
    stomps the first with its own default unless it uses
    default=argparse.SUPPRESS."""
    assert main(["feeds", "--root", str(root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {f["name"] for f in data} == {*FIXTURE_FEEDS, "mutable"}

    id8 = some_entry_id8(root)
    assert main(["show", id8, "--root", str(root)]) == 0
    assert capsys.readouterr().out.strip()


def test_feeds_json(root: Path, capsys):
    assert main(["--root", str(root), "--json", "feeds"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {f["name"] for f in data} == {*FIXTURE_FEEDS, "mutable"}
    assert all("health" in f and "entries" in f for f in data)


def test_ls_all_feeds(root: Path, capsys):
    assert main(["--root", str(root), "ls"]) == 0
    out = capsys.readouterr().out
    assert out.strip()


def test_ls_one_feed_and_limit(root: Path, capsys):
    assert main(["--root", str(root), "ls", "rust-blog", "-n", "2"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert 1 <= len(out) <= 2


def test_ls_long(root: Path, capsys):
    assert main(["--root", str(root), "ls", "rust-blog", "--long"]) == 0
    assert capsys.readouterr().out.strip()


def test_ls_json_parses(root: Path, capsys):
    assert main(["--root", str(root), "--json", "ls"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    assert {"id8", "feed", "published", "title", "path", "id", "link"} <= data[0].keys()


def test_ls_shows_real_titles_not_slugs(root: Path, capsys):
    """`ls` is bounded (one feed, or the default -n cap), so it should load
    the real <title> rather than deriving a pseudo-title from the filename
    slug -- a slug has lost punctuation and casing and reads as broken."""
    assert main(["--root", str(root), "ls", "rust-blog"]) == 0
    out = capsys.readouterr().out

    config = Config(root=root)
    refs = iter_entry_refs(config, "rust-blog")
    titles = [load_entry(r.path).title for r in refs]
    assert any(t in out for t in titles), out
    # A slug never contains real punctuation or capitals mid-word.
    for ref in refs:
        assert ref.title_hint not in out


def test_ls_default_limit_is_bounded(root: Path, capsys):
    assert main(["--root", str(root), "ls"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    # One line per entry, optionally more with --long; without it, entries <= default cap.
    assert 1 <= len(out) <= 50


def test_ls_unknown_feed_is_error(root: Path, capsys):
    rc = main(["--root", str(root), "ls", "does-not-exist"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("rss: ")
    assert "Traceback" not in err


def test_show(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "show", id8]) == 0
    out = capsys.readouterr().out
    assert out.strip()


def test_show_json(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "--json", "show", id8]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id8"] == id8
    assert "text" in data


def test_show_no_body_entry_still_prints_something(root: Path, capsys):
    id8 = entry_with_no_body(root)
    if id8 is None:
        pytest.skip("no content-origin=none entry in this fixture set")
    assert main(["--root", str(root), "show", id8]) == 0
    out = capsys.readouterr().out
    assert "no body" in out.lower()


def test_cat_prints_raw_xml(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "cat", id8]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<?xml")
    assert "<entry" in out


def test_link_prints_exactly_one_line(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "link", id8]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("http")


def test_links(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "links", id8]) == 0
    assert capsys.readouterr().out


def test_links_json(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "--json", "links", id8]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)


def test_open_no_browser_prints_url_and_fails(root: Path, capsys, monkeypatch):
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    id8 = some_entry_id8(root)
    rc = main(["--root", str(root), "open", id8])
    assert rc == 1
    out = capsys.readouterr().out
    assert out.strip().startswith("http")


def test_open_with_explicit_browser(root: Path, capsys):
    id8 = some_entry_id8(root)
    rc = main(["--root", str(root), "open", id8, "--browser", "true"])
    assert rc == 0


def test_search(root: Path, capsys):
    assert main(["--root", str(root), "search", "e"]) == 0
    assert capsys.readouterr().out.strip()


def test_search_json(root: Path, capsys):
    assert main(["--root", str(root), "--json", "search", "the"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)


def test_search_no_matches(root: Path, capsys):
    assert main(["--root", str(root), "search", "zzzznomatchzzzz"]) == 0
    out = capsys.readouterr().out
    assert "no matches" in out.lower()


def test_revisions(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "revisions", id8]) == 0
    assert "r1" in capsys.readouterr().out


def test_revisions_json(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "--json", "revisions", id8]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data and data[0]["revision"] == 1


def test_info(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "info", id8]) == 0
    out = capsys.readouterr().out
    assert id8 in out or "id8" in out


def test_info_json(root: Path, capsys):
    id8 = some_entry_id8(root)
    assert main(["--root", str(root), "--json", "info", id8]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id8"] == id8
    assert "content_hash" in data


def test_config(root: Path, capsys):
    assert main(["--root", str(root), "config"]) == 0
    out = capsys.readouterr().out
    assert "width" in out


def test_config_json(root: Path, capsys):
    assert main(["--root", str(root), "--json", "config"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "settings" in data and "source" in data


# ── diff, after forcing a real mutation ─────────────────────────────────────
#
# These need their own private instance -- unlike every other test in this
# file, they write a second revision on purpose -- so each builds a minimal
# one-feed store rather than sharing the module-wide `root`.


def _mutated_root(tmp_path: Path, server: str, revision: int) -> tuple[Path, str]:
    config = _populate(tmp_path, server, ["mutable"])
    (tmp_path / "served" / "mutable.xml").write_text(MUTABLE_FIXTURE.format(n=revision))
    asyncio.run(run_once(config))
    refs = iter_entry_refs(config, "mutable")
    assert refs, "mutable feed has no entries"
    return tmp_path, refs[0].id8


def test_diff_shows_the_change(tmp_path: Path, server: str, capsys):
    root, id8 = _mutated_root(tmp_path, server, revision=2)
    rc = main(["--root", str(root), "diff", id8])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Revision 1" in out or "Revision 2" in out or out.startswith("---")


def test_diff_json_and_xml_mode(tmp_path: Path, server: str, capsys):
    root, id8 = _mutated_root(tmp_path, server, revision=3)

    rc = main(["--root", str(root), "--json", "diff", id8])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "diff" in data

    rc = main(["--root", str(root), "diff", id8, "--xml"])
    assert rc == 0
    assert capsys.readouterr().out


def test_diff_single_revision_is_error(root: Path, capsys):
    """Every rust-blog entry should have exactly one revision after a fresh
    poll -- diffing it must fail cleanly, not crash."""
    config = Config(root=root)
    ref = iter_entry_refs(config, "rust-blog")[0]
    rc = main(["--root", str(root), "diff", ref.id8])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("rss: ")
    assert "Traceback" not in err


# ── resolve() failure modes surface as exit 1, no traceback ────────────────


def test_show_unknown_ref(root: Path, capsys):
    rc = main(["--root", str(root), "show", "deadbeef"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("rss: ")
    assert "Traceback" not in err


def test_show_ambiguous_prefix(root: Path, capsys):
    # A 2-hex prefix is very likely to match more than one of the 100 entries.
    config = Config(root=root)
    id8s = [
        ref.id8
        for name in feed_names(config)
        for ref in iter_entry_refs(config, name)
    ]
    from collections import Counter

    prefixes = Counter(i[:2] for i in id8s)
    ambiguous = next((p for p, n in prefixes.items() if n > 1), None)
    if ambiguous is None:
        pytest.skip("no ambiguous 2-hex prefix in this fixture set")
    rc = main(["--root", str(root), "show", ambiguous])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("rss: ")
    assert "ambiguous" in err.lower()


# ── tui import guard ─────────────────────────────────────────────────────


def test_tui_without_extra_reports_cleanly(root: Path, capsys, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.endswith("tui.app"):
            raise ImportError("no textual")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = main(["--root", str(root), "tui"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "tui" in err.lower()


# ── usage errors are exit code 2 ────────────────────────────────────────────


def test_missing_ref_argument_is_usage_error(root: Path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(root), "show"])
    assert exc.value.code == 2


# ── the load-bearing one: rss never opens a file for writing ───────────────


def test_rss_opens_nothing_for_writing(root: Path, capsys):
    before = mtime_snapshot(root)
    files_before = set(before)

    config = Config(root=root)
    id8 = some_entry_id8(root)
    feed = feed_names(config)[0]

    commands = [
        ["--root", str(root), "feeds"],
        ["--root", str(root), "--json", "feeds"],
        ["--root", str(root), "ls"],
        ["--root", str(root), "ls", feed, "--long"],
        ["--root", str(root), "show", id8],
        ["--root", str(root), "cat", id8],
        ["--root", str(root), "link", id8],
        ["--root", str(root), "links", id8],
        ["--root", str(root), "search", "a"],
        ["--root", str(root), "revisions", id8],
        ["--root", str(root), "info", id8],
        ["--root", str(root), "config"],
    ]
    for argv in commands:
        rc = main(argv)
        assert rc == 0, f"{argv} failed with {rc}: {capsys.readouterr().err}"
    capsys.readouterr()

    after = mtime_snapshot(root)
    assert set(after) == files_before, "rss created or deleted files"
    assert after == before, "rss modified an existing file's mtime"


def test_broken_pipe_exits_cleanly(root: Path):
    """Simulate `rss ls | head` by closing stdout mid-write."""
    import io

    class ExplodingStdout(io.TextIOBase):
        def write(self, s):
            raise BrokenPipeError()

        def close(self):
            pass

        def fileno(self):
            raise OSError("no real fd in this fake stdout")

    import sys

    old_stdout = sys.stdout
    sys.stdout = ExplodingStdout()
    try:
        rc = main(["--root", str(root), "ls"])
    finally:
        sys.stdout = old_stdout
    assert rc == 0
