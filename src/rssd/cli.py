"""Command line surface."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from .config import Config, Limits
from .models import Subscription

#: The reference feeds from SPEC §14. Between them they exercise every code
#: path: high churn, rich Atom content, thin RSS, an image-only body, and one
#: feed that sends no cache validators at all.
REFERENCE_FEEDS: list[tuple[str, str, str]] = [
    ("lobsters", "https://lobste.rs/rss", "high churn, ETag + Last-Modified"),
    ("rust-blog", "https://blog.rust-lang.org/feed.xml", "Atom, full content, xml:base"),
    ("simonw", "https://simonwillison.net/atom/everything/", "rich escaped HTML"),
    ("hn", "https://hnrss.org/frontpage", "title + link only, no body"),
    ("xkcd", "https://xkcd.com/rss.xml", "body is a single image"),
    ("godev", "https://go.dev/blog/feed.atom", "no cache validators at all"),
]

MUTABLE_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>rssd demo feed</title>
    <link>http://127.0.0.1:8765/</link>
    <description>A feed you can edit on purpose.</description>
    <item>
      <title>The entry that gets revised</title>
      <link>http://127.0.0.1:8765/demo/1</link>
      <guid isPermaLink="false">rssd-demo-1</guid>
      <pubDate>Mon, 15 Sep 2026 12:00:00 GMT</pubDate>
      <description>&lt;p&gt;Revision {n}. Every version is kept on disk.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""


def subscription_xml(name: str, url: str, comment: str, *, fulltext: bool = False) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<!-- {comment} -->\n"
        "<subscription>\n"
        f"  <url>{url}</url>\n"
        f"  <name>{name}</name>\n"
        f"  <fulltext>{'true' if fulltext else 'false'}</fulltext>\n"
        "</subscription>\n"
    )


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root)
    config = Config(root=root)
    config.ensure_dirs()

    base = args.fixture_base.rstrip("/") if args.fixture_mode else None
    for name, url, comment in REFERENCE_FEEDS:
        target = config.feeds_d / f"{name}.xml"
        if target.exists() and not args.force:
            continue
        effective = f"{base}/{name}.xml" if base else url
        target.write_text(subscription_xml(name, effective, comment))

    fixtures = root / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    mutable = fixtures / "mutable.xml"
    if not mutable.exists():
        mutable.write_text(MUTABLE_FIXTURE.format(n=1))
    if args.fixture_mode:
        src = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "feeds"
        for name, _, _ in REFERENCE_FEEDS:
            candidate = src / f"{name}.xml"
            if candidate.exists():
                shutil.copy2(candidate, fixtures / f"{name}.xml")
        target = config.feeds_d / "mutable.xml"
        if not target.exists():
            target.write_text(
                subscription_xml("mutable", f"{base}/mutable.xml", "editable demo feed")
            )

    print(f"initialised {root}")
    print(f"  feeds.d/   {len(list(config.feeds_d.glob('*.xml')))} subscriptions")
    print(f"  store/     (empty until first poll)")
    print(f"  var/       events.jsonl appears on first run")
    print()
    print(f"next:  rssd daemon --root {root} --poll-now")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from .subscriptions import load_dir

    config = _config(args)
    subs, errors = load_dir(config.feeds_d)
    for name in sorted(subs):
        sub = subs[name]
        extra = "  [fulltext]" if sub.fulltext else ""
        print(f"  ok      {name:<14} {sub.url}{extra}")
    for path, message in errors:
        print(f"  INVALID {path.name:<14} {message}")
    print(f"\n{len(subs)} valid, {len(errors)} invalid")
    return 1 if errors else 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete retired feed folders. Explicit, never automatic.

    The daemon itself will not remove harvested data under any circumstance;
    that decision belongs to a human who typed this command.
    """
    from .state import load_state

    config = _config(args)
    removed = 0
    for feed_dir in sorted(config.store.iterdir()) if config.store.exists() else []:
        if not feed_dir.is_dir():
            continue
        state = load_state(config, feed_dir.name)
        if state.health != "retired":
            continue
        if not args.yes:
            print(f"  would remove {feed_dir}")
            removed += 1
            continue
        shutil.rmtree(feed_dir)
        config.state_path(feed_dir.name).unlink(missing_ok=True)
        print(f"  removed {feed_dir}")
        removed += 1
    if removed and not args.yes:
        print(f"\n{removed} retired feed(s). Re-run with --yes to delete.")
    elif not removed:
        print("nothing to prune")
    return 0


def cmd_serve_fixtures(args: argparse.Namespace) -> int:
    from . import fixtures

    directory = Path(args.dir) if args.dir else fixtures.FIXTURE_DIR
    server = fixtures.serve(args.port, directory)
    print(f"serving {directory} on http://127.0.0.1:{args.port}")
    for path in sorted(directory.glob("*.xml")):
        print(f"  /{path.name}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def cmd_demo_mutate(args: argparse.Namespace) -> int:
    """Force a revision on demand.

    Waiting for a real publisher to fix a typo is not a demo. This edits the
    served fixture so the next poll sees changed content for an entry it has
    already stored, producing a .r2 file and an entry.revised event.
    """
    import re

    path = Path(args.root) / "fixtures" / "mutable.xml"
    if not path.exists():
        print(f"rssd: {path} not found; run `rssd init --fixture-mode` first")
        return 1
    text = path.read_text()
    match = re.search(r"Revision (\d+)\.", text)
    n = int(match.group(1)) + 1 if match else 2
    path.write_text(MUTABLE_FIXTURE.format(n=n))
    print(f"mutable.xml now at revision {n} — next poll will write .r{n}.xml")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run_daemon

    return asyncio.run(run_daemon(_config(args)))


def cmd_once(args: argparse.Namespace) -> int:
    from .daemon import run_once

    return asyncio.run(run_once(_config(args)))


def cmd_poll(args: argparse.Namespace) -> int:
    from .daemon import poll_one

    return asyncio.run(poll_one(_config(args), args.feed))


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .tui.app import run_tui
    except ImportError:
        print("rssd: the TUI needs the [tui] extra — run `uv sync --extra tui`")
        return 1
    return run_tui(_config(args))


def _config(args: argparse.Namespace) -> Config:
    return Config(
        root=Path(args.root),
        limits=Limits(),
        poll_now=getattr(args, "poll_now", False),
        fixture_mode=getattr(args, "fixture_mode", False),
        no_fsync=getattr(args, "no_fsync", False),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rssd", description="A file-based RSS daemon. The filesystem is the API."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_root(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--root", default=".", help="instance root directory")
        return p

    p_init = sub.add_parser("init", help="scaffold a new instance")
    p_init.add_argument("root", nargs="?", default=".")
    p_init.add_argument("--force", action="store_true", help="overwrite existing subscriptions")
    p_init.add_argument("--fixture-mode", action="store_true",
                        help="point subscriptions at the local fixture server")
    p_init.add_argument("--fixture-base", default="http://127.0.0.1:8765")
    p_init.set_defaults(func=cmd_init)

    p_daemon = with_root(sub.add_parser("daemon", help="run the daemon"))
    p_daemon.add_argument("--poll-now", action="store_true", help="skip the startup stagger")
    p_daemon.add_argument("--fixture-mode", action="store_true")
    p_daemon.add_argument("--no-fsync", action="store_true", help="unsafe; for slow filesystems")
    p_daemon.set_defaults(func=cmd_daemon)

    p_once = with_root(sub.add_parser("once", help="one poll pass, then exit"))
    p_once.add_argument("--no-fsync", action="store_true")
    p_once.set_defaults(func=cmd_once)

    p_poll = with_root(sub.add_parser("poll", help="force-poll a single feed"))
    p_poll.add_argument("feed")
    p_poll.set_defaults(func=cmd_poll)

    with_root(sub.add_parser("tui", help="terminal visualiser")).set_defaults(func=cmd_tui)
    with_root(sub.add_parser("validate", help="check every subscription parses")).set_defaults(
        func=cmd_validate
    )

    p_prune = with_root(sub.add_parser("prune", help="delete retired feed folders"))
    p_prune.add_argument("--retired", action="store_true", default=True)
    p_prune.add_argument("--yes", action="store_true", help="actually delete")
    p_prune.set_defaults(func=cmd_prune)

    p_serve = sub.add_parser("serve-fixtures", help="local HTTP server over fixtures")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--dir", default=None)
    p_serve.set_defaults(func=cmd_serve_fixtures)

    p_mutate = with_root(sub.add_parser("demo-mutate", help="force a revision on demand"))
    p_mutate.set_defaults(func=cmd_demo_mutate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
