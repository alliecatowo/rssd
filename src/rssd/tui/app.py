"""A vim-shaped TUI for `rss`.

Modal, ex-command driven, and -- the one architectural rule that matters --
this module never opens a file for writing. It only calls into `reader.py`
(filenames and parsed documents) and `userconf.py` (an in-memory
`ReaderConfig`). `:set` mutates that in-memory config for the running session
only, exactly like vim; anything a person wants to keep goes in `rss.toml` by
hand. No cache, no read/unread state, no lock file: `rss` is a pure consumer
of whatever `rssd` left on disk, and it has to work perfectly against a static
tree with no daemon running at all.

Layout is three panes (feeds | entries | reader) separated by a single-line
border -- not a box -- plus a vim-style status line and a command line that
only appears in command/search mode. There is no Header, no Footer, no
DataTable: nothing here should look like a widget toolkit.
"""

from __future__ import annotations

from datetime import UTC, datetime

import subprocess
import webbrowser
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from ..config import Config
from ..reader import (
    EntryDoc,
    EntryRef,
    FeedSummary,
    ReaderError,
    extract_links,
    iter_entry_refs,
    list_feeds,
    load_entry,
    resolve,
    to_text,
)
from ..userconf import ConfigError, ReaderConfig, set_option

PANES = ("feeds", "entries", "reader")

HELP_TEXT = """\
rss -- key reference

NORMAL mode
  h j k l          left/down/up/right  (h/l move between panes)
  gg  G            top / bottom
  ctrl-d  ctrl-u   half page down / up
  ctrl-f  ctrl-b   full page down / up
  Tab              cycle pane focus
  Enter            open the entry in the reader pane
  o                open the source URL in a browser
  y                yank the source URL to the clipboard
  Y                yank the entry's permalink list
  /  ?             search forward / backward
  n  N             next / previous match
  J  K             next / previous entry, staying in the reader pane
  r                refresh from disk
  p                toggle the reader pane
  q                quit
  :                enter command mode
  zz               centre the cursor line

COMMAND mode (:)
  :q :qa :quit           quit
  :e <feed>              switch feed
  :feeds                 focus the feed pane
  :set                   list every option and its current value
  :set <opt>             :set preview / :set nopreview / :set preview! / :set width=100
  :sort <mode>           newest | oldest | title | feed
  :open [ref]            open in browser -- a bare number opens that :links entry
  :links                 list every link in the current entry, numbered
  :N                     jump to entry N
  :help                  this text
"""


def _sort_entries(refs: list[EntryRef], mode: str) -> list[EntryRef]:
    if mode == "oldest":
        return sorted(refs, key=lambda r: (r.published, r.id8))
    if mode == "title":
        return sorted(refs, key=lambda r: r.title_hint.lower())
    if mode == "feed":
        return sorted(refs, key=lambda r: (r.feed, r.title_hint.lower()))
    return sorted(refs, key=lambda r: (r.published, r.id8), reverse=True)


def _clamp_window(top: int, index: int, height: int, total: int, scrolloff: int) -> int:
    """Vim-style window scrolling: keep `index` visible with `scrolloff` slack."""
    if height <= 0 or total <= 0:
        return 0
    off = max(0, min(scrolloff, (height - 1) // 2))
    lo, hi = top + off, top + height - 1 - off
    if index < lo:
        top = index - off
    elif index > hi:
        top = index - height + 1 + off
    return max(0, min(top, max(0, total - height)))


def _format_date(dt, fmt: str) -> str:
    """Match how `rss ls` prints dates, so the two tools agree on screen."""
    if dt is None:
        return "unknown date"
    if fmt == "iso":
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if fmt == "short":
        return dt.strftime("%Y-%m-%d")
    delta = datetime.now(UTC) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return dt.strftime("%Y-%m-%d")
    for limit, div, unit in (
        (3600, 60, "m"),
        (86400, 3600, "h"),
        (86400 * 30, 86400, "d"),
        (86400 * 365, 86400 * 30, "mo"),
    ):
        if seconds < limit:
            return f"{max(seconds // div, 0)}{unit} ago"
    return f"{seconds // (86400 * 365)}y ago"


class RssdApp(App):
    CSS = """
    Screen { background: $surface; layout: vertical; }
    #body { height: 1fr; }
    #feeds { width: 24; height: 100%; border-right: solid $panel-lighten-2; padding: 0 1; overflow: hidden; }
    #entries { width: 46; height: 100%; border-right: solid $panel-lighten-2; padding: 0 1; overflow: hidden; }
    #reader { width: 1fr; height: 100%; padding: 0 1; overflow: hidden; }
    #reader.hidden { display: none; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    #status.error { background: $error; color: $text; }
    #cmdline { height: 1; padding: 0 1; display: none; }
    #cmdline.visible { display: block; }
    """

    def __init__(self, config: Config, reader_config: ReaderConfig) -> None:
        super().__init__()
        self.config = config
        self.rc = reader_config

        self.mode = "normal"  # normal | command | search
        self.pending = ""  # a half-typed "gg" / "zz"
        # Leftmost pane first, like a file manager: Tab then reads
        # feeds -> entries -> reader -> feeds, the order a user expects.
        self.focus_pane = "feeds"

        self.feed_summaries: list[FeedSummary] = []
        self.feed_index = 0
        self.feeds_top = 0

        self.entries: list[EntryRef] = []
        self.entry_index = 0
        self.entries_top = 0
        #: Real titles for the entry list, loaded lazily for the visible
        #: window only (iter_entry_refs is filename-only and cheap; a title
        #: needs the document). Keyed on (symlink path, revision target) so
        #: a revision landing on the same filename invalidates the cache.
        self.title_cache: dict[tuple[Path, str], str] = {}

        self.doc: EntryDoc | None = None
        self.doc_links: list[tuple[str, str]] = []
        self.reader_mode = "entry"  # entry | links | set | help
        self.reader_lines: list[str] = []
        self.reader_scroll = 0

        self.command_prefix = ":"
        self.command_buffer = ""
        self.search_direction = "forward"
        self.last_search: tuple[str, str] | None = None

        self.status_message = ""
        self.status_error = False

    # ── layout ───────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield Static(id="feeds")
            yield Static(id="entries")
            yield Static(id="reader")
        yield Static(id="status")
        yield Static(id="cmdline")

    def on_mount(self) -> None:
        self.refresh_feeds(initial=True)
        self.set_interval(2.0, self.refresh_feeds)
        self.render_all()
        # The very first render_all() above runs before Textual has laid
        # out the panes, so widget.size.height is still 0 and each list
        # renders a single row. Queue a second pass for right after the
        # initial layout settles, so the cold-start screen is correct
        # without needing a keypress or a resize to fix itself.
        self.call_after_refresh(self.render_all)

    def on_resize(self, event: events.Resize) -> None:
        self.render_all()

    # ── filesystem refresh (read-only, never mutates the tree) ─────────────

    def refresh_feeds(self, initial: bool = False) -> None:
        self.feed_summaries = list_feeds(self.config)
        names = [f.name for f in self.feed_summaries]
        if not names:
            self.feed_index = 0
            self.entries = []
            self.entry_index = 0
            self.render_all()
            return
        current = None if initial else self._current_feed_name()
        self.feed_index = names.index(current) if current in names else 0
        self.refresh_entries()

    def _current_feed_name(self) -> str | None:
        if 0 <= self.feed_index < len(self.feed_summaries):
            return self.feed_summaries[self.feed_index].name
        return None

    def refresh_entries(self) -> None:
        name = self._current_feed_name()
        if name is None:
            self.entries = []
            self.entry_index = 0
            self.render_all()
            return
        keep_path = (
            self.entries[self.entry_index].path
            if 0 <= self.entry_index < len(self.entries)
            else None
        )
        refs = iter_entry_refs(self.config, name)
        self.entries = _sort_entries(refs, self.rc.sort)
        self.entry_index = 0
        if keep_path is not None:
            for i, ref in enumerate(self.entries):
                if ref.path == keep_path:
                    self.entry_index = i
                    break
        self.entries_top = 0
        self.render_all()

    # ── rendering ────────────────────────────────────────────────────────

    def render_all(self) -> None:
        self.render_feeds()
        self.render_entries()
        self.render_reader()
        self.render_status()
        self.render_cmdline()

    def _pane_static(self, name: str) -> Static:
        return self.query_one(f"#{name}", Static)

    def render_feeds(self) -> None:
        widget = self._pane_static("feeds")
        height = max(widget.size.height, 1)
        total = len(self.feed_summaries)
        self.feeds_top = _clamp_window(self.feeds_top, self.feed_index, height, total, 0)
        lines = []
        for offset, feed in enumerate(self.feed_summaries[self.feeds_top : self.feeds_top + height]):
            i = self.feeds_top + offset
            marker = ">" if i == self.feed_index else " "
            lines.append(f"{marker} {feed.name}")
        if self.rc.tildes:
            while len(lines) < height:
                lines.append("~")
        widget.update("\n".join(lines) if self.feed_summaries else "(no feeds)")

    def render_entries(self) -> None:
        widget = self._pane_static("entries")
        height = max(widget.size.height, 1)
        total = len(self.entries)
        self.entries_top = _clamp_window(
            self.entries_top, self.entry_index, height, total, self.rc.scrolloff
        )
        width_for_num = len(str(total)) if total else 1
        lines = []
        for offset, ref in enumerate(self.entries[self.entries_top : self.entries_top + height]):
            i = self.entries_top + offset
            marker = ">" if i == self.entry_index else " "
            gutter = ""
            if self.rc.number or self.rc.relativenumber:
                n = (
                    abs(i - self.entry_index)
                    if self.rc.relativenumber and i != self.entry_index
                    else i + 1
                )
                gutter = f"{n:>{width_for_num}} "
            lines.append(f"{marker}{gutter}{self._entry_title(ref)}")
        if self.rc.tildes:
            while len(lines) < height:
                lines.append("~")
        widget.update("\n".join(lines) if self.entries else "(no entries)")

    def _entry_title(self, ref: EntryRef) -> str:
        """The real title for one row of the entry list.

        `iter_entry_refs` is filename-only, so `ref.title_hint` is a slug
        ("announcing-rust-1-98-1") until something actually reads the
        document. `render_entries` only calls this for the rows currently
        on screen, so a full list never costs more than one `load_entry`
        per visible row -- milliseconds, not a scan of the whole feed.
        """
        try:
            target = ref.path.resolve().name
        except OSError:
            target = ""
        key = (ref.path, target)
        cached = self.title_cache.get(key)
        if cached is not None:
            return cached
        try:
            title = load_entry(ref.path).title or ref.title_hint
        except ReaderError:
            title = ref.title_hint
        self.title_cache[key] = title
        return title

    def render_reader(self) -> None:
        widget = self._pane_static("reader")
        widget.set_class(not self.rc.preview, "hidden")
        entries_widget = self._pane_static("entries")
        entries_widget.styles.width = "1fr" if not self.rc.preview else 46
        if not self.rc.preview:
            self.reader_lines = []
            return

        if self.reader_mode == "links":
            text = self._links_text()
        elif self.reader_mode == "help":
            text = HELP_TEXT
        elif self.reader_mode == "set":
            text = self._set_listing_text()
        elif self.doc is not None:
            text = self._doc_text()
        else:
            text = "(nothing open -- press <enter> on an entry in the list)"

        self.reader_lines = text.splitlines() or [""]
        height = max(widget.size.height, 1)
        self.reader_scroll = max(
            0, min(self.reader_scroll, max(0, len(self.reader_lines) - height))
        )
        visible = list(self.reader_lines[self.reader_scroll : self.reader_scroll + height])
        if self.rc.tildes:
            while len(visible) < height:
                visible.append("~")
        widget.update("\n".join(visible))

    def _doc_text(self) -> str:
        doc = self.doc
        assert doc is not None
        widget = self._pane_static("reader")
        # rc.width is a readability *maximum* for wide terminals, not an
        # override. Wrapping to 80 inside a 46-column pane just means Textual
        # wraps the result a second time, which is what produces ragged
        # half-lines. Cap at the pane.
        pane = max((widget.size.width or 80) - 2, 20)
        width = min(self.rc.width, pane) if self.rc.width else pane
        date = _format_date(doc.published, self.rc.date_format)
        header_lines = [doc.title, f"{doc.feed_title or doc.feed}  ·  {date}"]
        if doc.author:
            header_lines.append(f"by {doc.author}")
        header = "\n".join(header_lines) + "\n" + "-" * width
        if doc.content_origin == "none":
            body = "(this feed supplied no body for this entry)"
            if doc.link:
                body += f"\n\n{doc.link}"
            return f"{header}\n\n{body}"
        body = to_text(doc.content, width=width, marks=self.rc.marks, link_refs=self.rc.links)
        return f"{header}\n\n{body}" if body else f"{header}\n\n(empty body)"

    def _links_text(self) -> str:
        if self.doc is None:
            return "(no entry selected)"
        if not self.doc_links:
            return "(no links in this entry)"
        return "\n".join(
            f"[{i}] {href}  {label}" for i, (label, href) in enumerate(self.doc_links, start=1)
        )

    def _set_listing_text(self) -> str:
        lines = [":set -- current session options (persist by hand in rss.toml)", ""]
        for name, value in self.rc.describe():
            lines.append(f"  {name:<16} {value}")
        return "\n".join(lines)

    def render_status(self) -> None:
        widget = self._pane_static("status")
        mode_label = {"normal": "NORMAL", "command": "COMMAND", "search": "SEARCH"}[self.mode]
        feed = self._current_feed_name() or "-"
        total = len(self.entries)
        pos = f"{self.entry_index + 1}/{total}" if total else "0/0"
        pct = "--"
        if self.rc.preview and self.reader_lines:
            height = max(self._pane_static("reader").size.height, 1)
            denom = max(len(self.reader_lines) - height, 1)
            pct = f"{min(100, int(self.reader_scroll / denom * 100))}%"
        text = f"-- {mode_label} --  feed:{feed}  pane:{self.focus_pane}  {pos}  {pct}"
        if self.status_message:
            text += f"   {self.status_message}"
        widget.set_class(self.status_error, "error")
        widget.update(text)

    def render_cmdline(self) -> None:
        widget = self._pane_static("cmdline")
        visible = self.mode in ("command", "search")
        widget.set_class(visible, "visible")
        if visible:
            widget.update(f"{self.command_prefix}{self.command_buffer}")

    # ── key dispatch ─────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if self.mode == "normal":
            self._normal_key(event)
        else:
            self._command_key(event)
        self.render_all()

    def _normal_key(self, event: events.Key) -> None:
        char = event.character
        key = event.key

        if self.pending == "g":
            self.pending = ""
            if char == "g":
                self.move_top()
                return
        elif self.pending == "z":
            self.pending = ""
            if char == "z":
                self.center_cursor()
                return

        if char == "g":
            self.pending = "g"
            return
        if char == "z":
            self.pending = "z"
            return

        if char == ":":
            self.mode, self.command_prefix, self.command_buffer = "command", ":", ""
        elif char == "/":
            self.start_search("forward")
        elif char == "?":
            self.start_search("backward")
        elif char == "h":
            self.pane_left()
        elif char == "l":
            self.pane_right()
        elif char == "j":
            self.move_cursor(1)
        elif char == "k":
            self.move_cursor(-1)
        elif char == "G":
            self.move_bottom()
        elif char == "J":
            self.reader_step_entry(1)
        elif char == "K":
            self.reader_step_entry(-1)
        elif char == "n":
            self.next_match()
        elif char == "N":
            self.next_match(reverse=True)
        elif char == "o":
            self.open_source_link()
        elif char == "y":
            self.yank_url()
        elif char == "Y":
            self.yank_permalinks()
        elif char == "r":
            self.refresh_feeds()
        elif char == "p":
            self.toggle_preview()
        elif char == "q":
            self.exit(0)
        elif key == "enter":
            self.open_or_switch()
        elif key == "tab":
            self.cycle_focus()
        elif key == "ctrl+d":
            self.page(1, half=True)
        elif key == "ctrl+u":
            self.page(-1, half=True)
        elif key == "ctrl+f":
            self.page(1, half=False)
        elif key == "ctrl+b":
            self.page(-1, half=False)

    def _command_key(self, event: events.Key) -> None:
        key = event.key
        char = event.character
        if key == "escape":
            self.mode, self.command_buffer = "normal", ""
            return
        if key == "enter":
            buffer, was_search, direction = (
                self.command_buffer,
                self.mode == "search",
                self.search_direction,
            )
            self.mode, self.command_buffer = "normal", ""
            if was_search:
                self.run_search(buffer, direction)
            else:
                self.dispatch_command(buffer)
            return
        if key == "backspace":
            self.command_buffer = self.command_buffer[:-1]
            return
        if char and event.is_printable:
            self.command_buffer += char

    # ── movement ─────────────────────────────────────────────────────────

    def move_cursor(self, delta: int) -> None:
        pane = self.focus_pane
        if pane == "feeds":
            if not self.feed_summaries:
                return
            self.feed_index = max(0, min(len(self.feed_summaries) - 1, self.feed_index + delta))
            self.refresh_entries()
        elif pane == "entries":
            if not self.entries:
                return
            self.entry_index = max(0, min(len(self.entries) - 1, self.entry_index + delta))
        else:
            self.reader_scroll = max(0, self.reader_scroll + delta)

    def move_top(self) -> None:
        if self.focus_pane == "feeds":
            if self.feed_summaries:
                self.feed_index = 0
                self.refresh_entries()
        elif self.focus_pane == "entries":
            self.entry_index = 0
        else:
            self.reader_scroll = 0

    def move_bottom(self) -> None:
        if self.focus_pane == "feeds":
            if self.feed_summaries:
                self.feed_index = len(self.feed_summaries) - 1
                self.refresh_entries()
        elif self.focus_pane == "entries":
            if self.entries:
                self.entry_index = len(self.entries) - 1
        else:
            self.reader_scroll = max(0, len(self.reader_lines) - 1)

    def page(self, direction: int, half: bool) -> None:
        widget = self._pane_static(self.focus_pane)
        height = max(widget.size.height or 20, 1)
        step = max(1, height // 2 if half else height)
        self.move_cursor(direction * step)

    def cycle_focus(self) -> None:
        i = PANES.index(self.focus_pane)
        self.focus_pane = PANES[(i + 1) % len(PANES)]

    def pane_left(self) -> None:
        i = PANES.index(self.focus_pane)
        self.focus_pane = PANES[max(0, i - 1)]

    def pane_right(self) -> None:
        i = PANES.index(self.focus_pane)
        self.focus_pane = PANES[min(len(PANES) - 1, i + 1)]

    def center_cursor(self) -> None:
        if self.focus_pane == "entries":
            height = max(self._pane_static("entries").size.height, 1)
            self.entries_top = max(0, self.entry_index - height // 2)
        elif self.focus_pane == "feeds":
            height = max(self._pane_static("feeds").size.height, 1)
            self.feeds_top = max(0, self.feed_index - height // 2)
        else:
            height = max(self._pane_static("reader").size.height, 1)
            self.reader_scroll = max(0, self.reader_scroll - height // 2)

    # ── opening entries ──────────────────────────────────────────────────

    def open_or_switch(self) -> None:
        if self.focus_pane == "feeds":
            self.focus_pane = "entries"
        elif self.focus_pane == "entries":
            self.open_current_entry()

    def open_current_entry(self) -> None:
        if not self.entries:
            return
        ref = self.entries[self.entry_index]
        try:
            doc = load_entry(ref.path)
        except ReaderError as exc:
            self._set_error(str(exc))
            return
        self.doc = doc
        self.doc_links = extract_links(doc.content)
        self.reader_mode = "entry"
        self.reader_scroll = 0
        self.focus_pane = "reader"

    def reader_step_entry(self, delta: int) -> None:
        if not self.entries:
            return
        self.entry_index = max(0, min(len(self.entries) - 1, self.entry_index + delta))
        focus = self.focus_pane
        self.open_current_entry()
        self.focus_pane = focus

    # ── search ───────────────────────────────────────────────────────────

    def start_search(self, direction: str) -> None:
        self.mode = "search"
        self.command_prefix = "/" if direction == "forward" else "?"
        self.command_buffer = ""
        self.search_direction = direction

    def run_search(self, query: str, direction: str) -> None:
        if not query:
            return
        self.last_search = (direction, query)
        self._do_search(query, direction)

    def next_match(self, reverse: bool = False) -> None:
        if not self.last_search:
            return
        direction, query = self.last_search
        if reverse:
            direction = "backward" if direction == "forward" else "forward"
        self._do_search(query, direction)

    def _search_items(self) -> tuple[str, list[str]]:
        pane = self.focus_pane if self.focus_pane in ("feeds", "entries") else "entries"
        if pane == "feeds":
            return pane, [f.name for f in self.feed_summaries]
        return pane, [r.title_hint for r in self.entries]

    def _do_search(self, query: str, direction: str) -> None:
        pane, items = self._search_items()
        if not items:
            self._set_error("no matches")
            return
        n = len(items)
        idx = self.feed_index if pane == "feeds" else self.entry_index
        steps = range(1, n + 1) if direction == "forward" else range(-1, -n - 1, -1)
        q = query.lower()
        for step in steps:
            i = (idx + step) % n
            if q in items[i].lower():
                if pane == "feeds":
                    self.feed_index = i
                    self.refresh_entries()
                else:
                    self.entry_index = i
                self.focus_pane = pane
                self.status_message, self.status_error = "", False
                return
        self._set_error(f"no match: {query}")

    # ── open / yank ──────────────────────────────────────────────────────

    def open_source_link(self) -> None:
        if self.doc is None or not self.doc.link:
            self._set_error("no link for this entry")
            return
        self._launch(self.doc.link)

    def _launch(self, url: str) -> None:
        try:
            if self.rc.browser:
                subprocess.Popen(
                    [self.rc.browser, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                webbrowser.open(url)
        except Exception as exc:  # pragma: no cover - environment dependent
            self._set_error(f"could not open browser: {exc}")

    def yank_url(self) -> None:
        if self.doc is None or not self.doc.link:
            self._set_error("no link to yank")
            return
        self.copy_to_clipboard(self.doc.link)
        self.status_message, self.status_error = "yanked url", False

    def yank_permalinks(self) -> None:
        if self.doc is None:
            self._set_error("no entry selected")
            return
        links = [href for _, href in self.doc_links] or ([self.doc.link] if self.doc.link else [])
        if not links:
            self._set_error("no links to yank")
            return
        self.copy_to_clipboard("\n".join(links))
        self.status_message, self.status_error = f"yanked {len(links)} link(s)", False

    def toggle_preview(self) -> None:
        self.rc = set_option(self.rc, "preview!", None)
        if not self.rc.preview and self.focus_pane == "reader":
            self.focus_pane = "entries"

    # ── ex commands ──────────────────────────────────────────────────────

    def _set_error(self, message: str) -> None:
        self.status_message, self.status_error = message, True

    def dispatch_command(self, raw: str) -> None:
        raw = raw.strip()
        self.status_message, self.status_error = "", False
        if not raw:
            return
        if raw.isdigit():
            self._goto_entry(int(raw))
            return
        parts = raw.split(None, 1)
        cmd, rest = parts[0], parts[1] if len(parts) > 1 else ""
        try:
            if cmd in ("q", "qa", "quit"):
                self.exit(0)
            elif cmd == "e":
                self._switch_feed(rest.strip())
            elif cmd == "feeds":
                self.focus_pane = "feeds"
            elif cmd == "set":
                self._cmd_set(rest.strip())
            elif cmd == "sort":
                self.rc = set_option(self.rc, "sort", rest.strip())
                self.refresh_entries()
            elif cmd == "open":
                self._cmd_open(rest.strip())
            elif cmd == "links":
                self.reader_mode = "links"
            elif cmd == "help":
                self.reader_mode = "help"
            else:
                self._set_error(f"unknown command: {cmd}")
        except (ConfigError, ReaderError, ValueError) as exc:
            self._set_error(str(exc))

    def _goto_entry(self, n: int) -> None:
        if not self.entries:
            self._set_error("no entries")
            return
        if not 1 <= n <= len(self.entries):
            self._set_error(f"out of range (1..{len(self.entries)})")
            return
        self.entry_index = n - 1
        self.open_current_entry()

    def _switch_feed(self, name: str) -> None:
        if not name:
            self._set_error("usage: :e <feed>")
            return
        names = [f.name for f in self.feed_summaries]
        if name not in names:
            self._set_error(f"no such feed: {name}")
            return
        self.feed_index = names.index(name)
        self.refresh_entries()
        self.focus_pane = "entries"

    def _cmd_set(self, rest: str) -> None:
        if not rest:
            self.reader_mode = "set"
            return
        if "=" in rest:
            key, _, value = rest.partition("=")
            value = value.strip()
        else:
            key, value = rest, None
        self.rc = set_option(self.rc, key.strip(), value)

    def _cmd_open(self, rest: str) -> None:
        if not rest:
            self.open_source_link()
            return
        if rest.isdigit():
            n = int(rest)
            if not self.doc_links or not 1 <= n <= len(self.doc_links):
                self._set_error(f"no such link: {n}")
                return
            self._launch(self.doc_links[n - 1][1])
            return
        try:
            path = resolve(self.config, rest)
            doc = load_entry(path)
        except ReaderError as exc:
            self._set_error(str(exc))
            return
        if doc.link:
            self._launch(doc.link)
        else:
            self._set_error("entry has no link")


def run_tui(config: Config, reader_config: ReaderConfig) -> int:
    RssdApp(config, reader_config).run()
    return 0
