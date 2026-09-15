"""A Textual visualiser for a running rssd instance.

The important thing about this module is what it does NOT import: nothing from
scheduler, fetch, or store. It discovers everything by listing directories and
tailing var/events.jsonl, exactly as any third-party consumer would. If this can
be built that way, the file interface is sufficient -- that is the claim the
whole design rests on, and this is the proof.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from ..config import Config

HEALTH_STYLE = {
    "ok": "green",
    "pending": "yellow",
    "degraded": "dark_orange",
    "failing": "red",
    "retired": "grey50",
}

EVENT_STYLE = {
    "entry.new": "bold green",
    "entry.revised": "bold cyan",
    "entry.revision-suppressed": "yellow",
    "feed.poll-failed": "red",
    "feed.anomaly": "bold red",
    "feed.throttled": "yellow",
    "feed.unchanged": "grey50",
    "feed.poll-started": "grey50",
    "subscription.invalid": "red",
    "feed.identity-downgraded": "bold yellow",
}


@dataclass
class FeedView:
    name: str
    health: str
    entries: int
    last_poll: str
    error: str | None


def _text(path: Path, tag: str) -> str | None:
    """Pull one element's text out of a status/feed document.

    Deliberately a regex rather than an XML parse: the TUI must tolerate reading
    a file the daemon is in the middle of replacing. It cannot see a torn file
    (the store renames atomically) but it can see an old one, and that is fine.
    """
    import re

    try:
        data = path.read_text(errors="replace")
    except OSError:
        return None
    match = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", data)
    return match.group(1).strip() if match else None


def scan_feeds(config: Config) -> list[FeedView]:
    views: list[FeedView] = []
    if not config.store.exists():
        return views
    for feed_dir in sorted(config.store.iterdir()):
        if not feed_dir.is_dir():
            continue
        status = feed_dir / "status.xml"
        entries_dir = feed_dir / "entries"
        count = 0
        if entries_dir.exists():
            # Count symlinks, not revision files: one per logical entry.
            count = sum(1 for p in entries_dir.iterdir() if p.is_symlink())
        views.append(
            FeedView(
                name=feed_dir.name,
                health=_text(status, "health") or "pending",
                entries=count,
                last_poll=(_text(status, "last-poll-at") or "—")[11:19] or "—",
                error=_text(status, "last-error"),
            )
        )
    return views


class EventTail:
    """Follows events.jsonl by offset, surviving rotation.

    Tracks the inode so that a rotated-and-reopened log is detected and read
    from the start rather than silently going dead -- the same trap that makes
    `tail -f` the wrong tool and `tail -F` the right one.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.inode: int | None = None

    def read(self) -> list[dict]:
        try:
            stat = self.path.stat()
        except OSError:
            return []
        if self.inode is not None and stat.st_ino != self.inode:
            self.offset = 0  # rotated out from under us
        self.inode = stat.st_ino
        if stat.st_size < self.offset:
            self.offset = 0  # truncated
        if stat.st_size == self.offset:
            return []
        out: list[dict] = []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
                # Only consume up to the last complete line.
                cut = data.rfind(b"\n")
                if cut == -1:
                    return []
                self.offset += cut + 1
                for line in data[:cut].split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out


class FeedTable(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("feed", "health", "entries", "last poll")


class RssdApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #left { width: 46; border-right: solid $panel; }
    #right { width: 1fr; }
    FeedTable { height: 1fr; }
    #events { height: 1fr; border: none; }
    #detail { height: auto; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [("q", "quit", "quit"), ("r", "refresh", "refresh")]

    total_entries: reactive[int] = reactive(0)

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.tail = EventTail(config.events_path)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield FeedTable(id="feeds")
                yield Static("", id="detail")
            with Vertical(id="right"):
                yield RichLog(id="events", markup=True, wrap=False, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "rssd"
        self.sub_title = str(self.config.root)
        self.set_interval(1.0, self.refresh_feeds)
        self.set_interval(0.25, self.drain_events)
        self.refresh_feeds()

    def action_refresh(self) -> None:
        self.refresh_feeds()

    def refresh_feeds(self) -> None:
        table = self.query_one("#feeds", FeedTable)
        views = scan_feeds(self.config)
        row = table.cursor_row
        table.clear()
        total = 0
        for view in views:
            total += view.entries
            colour = HEALTH_STYLE.get(view.health, "white")
            table.add_row(
                view.name,
                f"[{colour}]{view.health}[/{colour}]",
                str(view.entries),
                view.last_poll,
                key=view.name,
            )
        self.total_entries = total
        if row is not None and row < table.row_count:
            table.move_cursor(row=row)
        errs = [v for v in views if v.error]
        detail = self.query_one("#detail", Static)
        if errs:
            detail.update(f"[red]{errs[0].name}: {errs[0].error}[/red]")
        else:
            detail.update(f"{len(views)} feeds · {total} entries")

    def drain_events(self) -> None:
        log = self.query_one("#events", RichLog)
        for event in self.tail.read():
            name = event.get("event", "?")
            style = EVENT_STYLE.get(name, "white")
            ts = str(event.get("ts", ""))[11:23]
            feed = event.get("feed") or ""
            data = event.get("data") or {}
            detail = ""
            if title := data.get("title"):
                detail = f" {title[:60]}"
            elif error := data.get("error"):
                detail = f" [red]{str(error)[:60]}[/red]"
            elif (written := data.get("written")) is not None:
                detail = f" entries={data.get('entries')} written={written}"
            elif reason := data.get("reason"):
                detail = f" ({reason})"
            log.write(
                f"[grey50]{ts}[/grey50] [{style}]{name:<26}[/{style}] "
                f"[bold]{feed:<12}[/bold]{detail}"
            )


def run_tui(config: Config) -> int:
    RssdApp(config).run()
    return 0
