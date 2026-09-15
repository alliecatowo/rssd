"""Tests for the vim-shaped TUI.

Mostly logic, not pixels: key dispatch, ex commands, search, and -- the one
invariant that matters most -- that driving the app never writes a single
byte to the store. The fixture mirrors test_integration.py: a real HTTP
server serving the committed fixtures, polled once with `run_once`, so the
TUI is exercised against a real tree of ~100 entries rather than a mock.

A layout bug (panes collapsing to one line because Static's `height: auto`
depended on content that depended on height) walked straight through a
test suite that only inspected app state. `test_screen_*` below renders the
actual compositor output to text and asserts on *that*, specifically to
catch the next one of these.
"""

from __future__ import annotations

import asyncio
import shutil
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from rssd import fixtures
from rssd.cli import MUTABLE_FIXTURE, subscription_xml
from rssd.config import Config
from rssd.daemon import run_once
from rssd.reader import feed_names
from rssd.tui.app import RssdApp
from rssd.userconf import ReaderConfig

FIXTURE_FEEDS = ["lobsters", "rust-blog", "simonw", "hn", "xkcd", "godev"]


@pytest.fixture(scope="module")
def server():
    srv, _thread = fixtures.serve_in_thread(port=0, directory=fixtures.FIXTURE_DIR)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def root(tmp_path: Path, server: str) -> Path:
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


@pytest.fixture
async def populated_root(root: Path) -> Path:
    config = Config(root=root)
    assert await run_once(config) == 0
    return root


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def make_app(root: Path, **rc_overrides) -> RssdApp:
    config = Config(root=root)
    rc = ReaderConfig(**rc_overrides)
    return RssdApp(config, rc)


async def render_screen(pilot, width: int = 118, height: int = 30) -> str:
    """The actual compositor output, as plain text -- what a person would
    see. Used to catch layout bugs (panes collapsing, columns not
    appearing) that app-state assertions can't see."""
    await pilot.pause()
    await asyncio.sleep(0.5)
    await pilot.pause()
    console = Console(file=StringIO(), width=width, height=height, legacy_windows=False)
    console.print(pilot.app.screen._compositor)
    return console.file.getvalue()


# ── movement ─────────────────────────────────────────────────────────────


async def test_j_k_move_selection(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.entries, "expected entries to be loaded"
        await pilot.press("l")  # feeds -> entries
        assert app.focus_pane == "entries"
        start = app.entry_index
        await pilot.press("j")
        assert app.entry_index == start + 1
        await pilot.press("j")
        assert app.entry_index == start + 2
        await pilot.press("k")
        assert app.entry_index == start + 1


async def test_gg_and_G_go_to_top_and_bottom(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        total = len(app.entries)
        assert total > 1
        await pilot.press("l")  # feeds -> entries
        await pilot.press("j", "j", "j")
        assert app.entry_index != 0
        await pilot.press("g", "g")
        assert app.entry_index == 0
        await pilot.press("G")
        assert app.entry_index == total - 1


async def test_j_does_not_move_past_the_last_entry(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        total = len(app.entries)
        await pilot.press("l")  # feeds -> entries
        await pilot.press("G")
        assert app.entry_index == total - 1
        await pilot.press("j")
        assert app.entry_index == total - 1


async def test_h_l_move_between_panes(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.focus_pane == "feeds"  # leftmost pane, like a file manager
        await pilot.press("h")
        assert app.focus_pane == "feeds"  # already leftmost
        await pilot.press("l", "l")
        assert app.focus_pane == "reader"
        await pilot.press("h")
        assert app.focus_pane == "entries"


async def test_tab_cycles_focus(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.focus_pane == "feeds"
        await pilot.press("tab")
        assert app.focus_pane == "entries"
        await pilot.press("tab")
        assert app.focus_pane == "reader"
        await pilot.press("tab")
        assert app.focus_pane == "feeds"


# ── :set ─────────────────────────────────────────────────────────────────


async def test_set_width_applies(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.rc.width == 80
        for ch in ":set width=100":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.rc.width == 100
        assert not app.status_error


async def test_set_nopreview_hides_reader_pane(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.rc.preview is True
        for ch in ":set nopreview":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.rc.preview is False
        reader = app.query_one("#reader")
        assert reader.has_class("hidden")


async def test_set_bogus_option_shows_error_and_does_not_raise(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        for ch in ":set bogus":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.status_error is True
        assert "bogus" in app.status_message
        # the app is still alive and usable
        await pilot.press("j")


async def test_p_toggles_preview(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        assert app.rc.preview is True
        await pilot.press("p")
        assert app.rc.preview is False
        await pilot.press("p")
        assert app.rc.preview is True


# ── search ───────────────────────────────────────────────────────────────


async def test_search_forward_finds_match_and_n_advances(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        titles = [r.title_hint for r in app.entries]
        # pick a word that appears in at least two titles, else skip meaningfully
        word = None
        for candidate in ("the", "a", "e", "o"):
            hits = [t for t in titles if candidate in t.lower()]
            if len(hits) >= 2:
                word = candidate
                break
        assert word is not None, "fixture titles unexpectedly have no common letters"

        await pilot.press("l")  # feeds -> entries
        await pilot.press("g", "g")  # start from the top
        await pilot.press("/")
        assert app.mode == "search"
        for ch in word:
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.mode == "normal"
        assert word in app.entries[app.entry_index].title_hint.lower()
        first_match = app.entry_index

        await pilot.press("n")
        assert word in app.entries[app.entry_index].title_hint.lower()
        assert app.entry_index != first_match


async def test_search_with_no_match_sets_error(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("/")
        for ch in "zzzznosuchtitlezzzz":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.status_error is True


# ── reader pane / entries ───────────────────────────────────────────────


async def test_enter_populates_reader_pane(populated_root: Path):
    """The exact sequence a user reported as broken: tab into the entry
    list, move down one, open it."""
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("tab")  # feeds -> entries
        assert app.focus_pane == "entries"
        await pilot.press("j")
        await pilot.press("enter")
        assert app.doc is not None
        assert app.focus_pane == "reader"
        assert app.reader_lines
        assert app.doc.title in "\n".join(app.reader_lines) or any(
            app.doc.title in line for line in app.reader_lines
        )


async def test_links_and_open_agree_on_numbering(populated_root: Path):
    config = Config(root=populated_root)
    # Find an entry that actually has links in its body.
    target_ref = None
    target_doc = None
    for name in feed_names(config):
        from rssd.reader import iter_entry_refs, load_entry

        for ref in iter_entry_refs(config, name):
            doc = load_entry(ref.path)
            from rssd.reader import extract_links

            links = extract_links(doc.content)
            if links:
                target_ref, target_doc = ref, doc
                break
        if target_ref:
            break
    assert target_ref is not None, "expected at least one entry with links"

    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        # switch to the feed containing our target entry and select it
        for ch in f":e {target_ref.feed}":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.status_error is False, app.status_message

        idx = next(i for i, r in enumerate(app.entries) if r.path == target_ref.path)
        for _ in range(idx):
            await pilot.press("j")
        await pilot.press("enter")
        assert app.doc is not None and app.doc.path == target_ref.path

        expected_links = app.doc_links
        assert expected_links

        for ch in ":links":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.reader_mode == "links"
        links_text = "\n".join(app.reader_lines)
        for i, (_, href) in enumerate(expected_links, start=1):
            assert f"[{i}] {href}" in links_text

        captured = {}
        app._launch = lambda url: captured.setdefault("url", url)  # avoid real browser
        for ch in ":open 1":
            await pilot.press(ch)
        await pilot.press("enter")
        assert captured.get("url") == expected_links[0][1]


async def test_content_origin_none_shows_plain_message(populated_root: Path):
    """hn entries carry title+link only, no body (SPEC fixture note)."""
    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        for ch in ":e hn":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("enter")  # open the first entry
        assert app.doc is not None
        if app.doc.content_origin == "none":
            text = "\n".join(app.reader_lines)
            assert "no body" in text.lower()


# ── the load-bearing invariant ──────────────────────────────────────────


# ── rendered screen (catches layout bugs state assertions can't) ─────────


async def test_screen_shows_multiple_feeds_and_entries(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(118, 30)) as pilot:
        screen = await render_screen(pilot)

        feed_hits = [f.name for f in app.feed_summaries if f.name in screen]
        assert len(feed_hits) > 1, (
            f"expected more than one feed name visible, got {feed_hits!r}\n{screen}"
        )

        entry_rows = [
            r for r in app.entries[: len(app.entries)] if app._entry_title(r) in screen
        ]
        assert len(entry_rows) >= 5, (
            f"expected at least 5 entry rows visible, got {len(entry_rows)}\n{screen}"
        )


async def test_screen_enter_shows_article_body(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(118, 30)) as pilot:
        for ch in ":e rust-blog":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("enter")  # open the first entry
        assert app.doc is not None
        # A word pulled straight from the rendered body, not just the title,
        # so this genuinely exercises to_text() reaching the screen.
        body_words = [w for w in app.reader_lines[3:] if len(w.strip()) > 8]
        assert body_words, "expected a non-trivial rendered body"

        screen = await render_screen(pilot)
        assert app.doc.title in screen, screen
        distinctive = next(
            (line.strip() for line in app.reader_lines if len(line.strip()) > 20), None
        )
        assert distinctive is not None
        # The exact line may be wrapped differently by the terminal, so
        # check a distinctive fragment of it rather than the whole line.
        fragment = distinctive[:20]
        assert fragment in screen, f"{fragment!r} not found in rendered screen\n{screen}"


async def test_screen_set_nopreview_removes_reader_column(populated_root: Path):
    app = make_app(populated_root)
    async with app.run_test(size=(118, 30)) as pilot:
        for ch in ":e rust-blog":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("enter")

        # A fragment of the rendered *body*, not the title -- the title also
        # legitimately appears as a row in the entries list regardless of
        # whether the reader pane is showing, so it can't tell the two
        # states apart. A body fragment can only come from the reader pane.
        body_fragment = next(
            (line.strip()[:20] for line in app.reader_lines[3:] if len(line.strip()) > 20),
            None,
        )
        assert body_fragment is not None

        before = await render_screen(pilot)
        assert body_fragment in before, before
        # The reader pane's own header line has real text after its
        # rightmost border, on a line that has 2 separators.
        title_line = next(l for l in before.splitlines() if app.doc.title in l)
        assert title_line.count("│") >= 2
        after_last_sep = title_line.rsplit("│", 1)[1]
        assert after_last_sep.strip(), "expected reader content past the 2nd separator"

        for ch in ":set nopreview":
            await pilot.press(ch)
        await pilot.press("enter")
        assert app.rc.preview is False

        after = await render_screen(pilot)
        assert body_fragment not in after, after
        # Same row, now with nothing (or just the screen-edge border, with
        # only blank padding after it) past the entries column.
        title_line_after = next(l for l in after.splitlines() if app.doc.title in l)
        after_last_sep = title_line_after.rsplit("│", 1)[1]
        assert not after_last_sep.strip(), f"reader content leaked: {title_line_after!r}"


async def test_tui_writes_nothing(populated_root: Path):
    before = snapshot(populated_root)

    app = make_app(populated_root)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("j", "j", "k", "g", "g", "G")
        await pilot.press("tab", "tab", "h", "l")
        await pilot.press("enter")  # open an entry
        await pilot.press("p", "p")  # toggle preview off/on
        for ch in ":set width=100":
            await pilot.press(ch)
        await pilot.press("enter")
        for ch in ":set bogus":
            await pilot.press(ch)
        await pilot.press("enter")
        for ch in ":links":
            await pilot.press(ch)
        await pilot.press("enter")
        for ch in ":sort oldest":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("/")
        for ch in "e":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.press("n")
        await pilot.press("y")  # yank -- clipboard only, no file writes
        await pilot.press("Y")
        await pilot.press("r")  # explicit refresh from disk

    after = snapshot(populated_root)
    assert after == before, "the TUI must never write to the store"
