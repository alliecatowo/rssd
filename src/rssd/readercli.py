"""`rss` — the reader.

A pure consumer of the tree `rssd` writes. This module opens no file for
writing, ever: no caches, no state files, no read-tracking. The daemon's
output tree is the whole API, and this CLI is the proof that the tree is
sufficient on its own.

Pipe-friendly by default: colour and box-drawing only when stdout is a tty
and `NO_COLOR` is unset. Every listing/showing command supports `--json` for
scripting.
"""

from __future__ import annotations

import argparse
import difflib
import json as jsonlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .reader import (
    EntryDoc,
    EntryRef,
    ReaderError,
    extract_links,
    feed_names,
    iter_entry_refs,
    list_feeds,
    load_entry,
    plain_summary,
    resolve,
    revisions,
    to_text,
)
from .userconf import ConfigError, ReaderConfig, load_reader_config

_REV_NUM_RE = re.compile(r"\.r(\d+)\.xml$")


# ── small formatting helpers ────────────────────────────────────────────────


def _use_color(args: argparse.Namespace) -> bool:
    if args.no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _color(args: argparse.Namespace, text: str, code: str) -> str:
    if not _use_color(args):
        return text
    return f"\033[{code}m{text}\033[0m"


def _dt_json(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_date(dt: datetime | None, fmt: str) -> str:
    if dt is None:
        return "-"
    dt = dt.astimezone(UTC)
    if fmt == "iso":
        return dt.strftime("%Y-%m-%d %H:%M")
    if fmt == "short":
        return dt.strftime("%Y-%m-%d")
    # relative
    now = datetime.now(UTC)
    secs = (now - dt).total_seconds()
    if secs < 0:
        return "future"
    if secs < 60:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo ago"
    years = days / 365
    return f"{int(years)}y ago"


def _effective_width(args: argparse.Namespace, reader_config: ReaderConfig) -> int:
    if args.width and args.width > 0:
        return args.width
    if reader_config.width and reader_config.width > 0:
        return reader_config.width
    return shutil.get_terminal_size(fallback=(80, 24)).columns


def _emit_json(obj: object) -> None:
    print(jsonlib.dumps(obj, indent=2, ensure_ascii=False))


def _load(args: argparse.Namespace) -> tuple[Config, ReaderConfig]:
    root = Path(args.root)
    config = Config(root=root)
    reader_config, _source = load_reader_config(root)
    return config, reader_config


def _rev_num(path: Path) -> int:
    match = _REV_NUM_RE.search(path.name)
    return int(match.group(1)) if match else 1


def _ref_json(ref: EntryRef, config: Config) -> dict:
    return {
        "id8": ref.id8,
        "feed": ref.feed,
        "published": _dt_json(ref.published),
        "title": ref.title_hint,
        "path": config.relative(ref.path),
    }


def _doc_json(doc: EntryDoc, config: Config) -> dict:
    return {
        "id": doc.id,
        "id8": doc.id8,
        "revision": doc.revision,
        "feed": doc.feed,
        "feed_title": doc.feed_title,
        "feed_url": doc.feed_url,
        "title": doc.title,
        "author": doc.author,
        "link": doc.link,
        "guid": doc.guid,
        "published": _dt_json(doc.published),
        "published_origin": doc.published_origin,
        "first_seen": _dt_json(doc.first_seen),
        "updated": _dt_json(doc.updated),
        "categories": list(doc.categories),
        "content_origin": doc.content_origin,
        "content_hash": doc.content_hash,
        "path": config.relative(doc.path),
    }


# ── commands ─────────────────────────────────────────────────────────────


def cmd_feeds(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    feeds = list_feeds(config)
    if args.json:
        _emit_json(
            [
                {
                    "name": f.name,
                    "title": f.title,
                    "url": f.url,
                    "health": f.health,
                    "entries": f.entries,
                    "last_poll": _dt_json(f.last_poll),
                    "last_error": f.last_error,
                    "state": f.state,
                }
                for f in feeds
            ]
        )
        return 0
    if not feeds:
        print("no feeds yet -- run `rssd init` and `rssd once` first")
        return 0
    health_color = {
        "ok": "32",
        "pending": "33",
        "degraded": "38;5;208",
        "failing": "31",
        "retired": "90",
    }
    name_w = max(4, max(len(f.name) for f in feeds))
    print(f"{'NAME':<{name_w}}  {'HEALTH':<9} {'ENTRIES':>7}  {'LAST POLL':<10}  TITLE")
    for f in feeds:
        health = _color(args, f"{f.health:<9}", health_color.get(f.health, "0"))
        last_poll = _format_date(f.last_poll, "short") if f.last_poll else "-"
        title = f.title or ""
        print(f"{f.name:<{name_w}}  {health} {f.entries:>7}  {last_poll:<10}  {title}")
    return 0


def _all_refs(config: Config, feed: str | None) -> list[EntryRef]:
    if feed is not None:
        if feed not in feed_names(config):
            raise ReaderError(f"no such feed: {feed!r}")
        return iter_entry_refs(config, feed)
    refs: list[EntryRef] = []
    for name in feed_names(config):
        refs.extend(iter_entry_refs(config, name))
    refs.sort(key=lambda r: (r.published, r.id8), reverse=True)
    return refs


#: `ls` and `search` are bounded by definition (a specific feed, a limited
#: count), so loading the sliced-to-size set of documents to get a real title
#: is a few milliseconds and clearly the right trade -- unlike an unbounded
#: listing, where `title_hint` earns its keep. See reader.py's EntryRef
#: docstring for the rationale this default preserves.
DEFAULT_LIST_LIMIT = 50


def _load_docs(refs: list[EntryRef]) -> list[tuple[EntryRef, EntryDoc | None]]:
    """Load a document for each ref, falling back to None (and title_hint at
    the call site) if a file turns out to be unreadable."""
    docs = []
    for ref in refs:
        try:
            docs.append((ref, load_entry(ref.path)))
        except ReaderError:
            docs.append((ref, None))
    return docs


def cmd_ls(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    refs = _all_refs(config, args.feed)
    limit = args.n if args.n is not None else DEFAULT_LIST_LIMIT
    if limit > 0:
        refs = refs[:limit]

    # Slice first, load second: the whole point of EntryRef is that listing
    # is free, but once we know exactly which (bounded) set of entries will
    # be printed, loading them for a real title is cheap and worth it --
    # a slug like "announcing-rust-1-98-1" is not a title.
    docs = _load_docs(refs)

    if args.json:
        out = []
        for ref, doc in docs:
            entry = _ref_json(ref, config)
            if doc is not None:
                entry["id"] = doc.id
                entry["title"] = doc.title
                entry["link"] = doc.link
                entry["author"] = doc.author
                entry["content_origin"] = doc.content_origin
                entry["summary"] = plain_summary(doc.content)
            out.append(entry)
        _emit_json(out)
        return 0

    if not docs:
        print("no entries")
        return 0

    for ref, doc in docs:
        date = _format_date(ref.published, reader_config.date_format)
        title = doc.title if doc is not None else ref.title_hint
        id8 = _color(args, ref.id8, "36")
        print(f"{id8}  {ref.feed:<16} {date:<10}  {title}")
        if args.long and doc is not None:
            author = f"by {doc.author}" if doc.author else ""
            summary = plain_summary(doc.content, limit=120)
            extra = "  ·  ".join(p for p in (author, summary) if p)
            if extra:
                print(f"          {extra}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    path = resolve(config, args.ref)
    doc = load_entry(path)
    width = _effective_width(args, reader_config)
    body = to_text(
        doc.content, width=width, marks=reader_config.marks, link_refs=reader_config.links
    )

    if args.json:
        out = _doc_json(doc, config)
        out["text"] = body
        _emit_json(out)
        return 0

    print(_color(args, doc.title, "1"))
    print(f"{doc.feed}  ·  {_format_date(doc.published, reader_config.date_format)}", end="")
    if doc.author:
        print(f"  ·  {doc.author}", end="")
    print()
    if doc.link:
        print(_color(args, doc.link, "34"))
    print()
    if doc.content_origin == "none":
        print("(this feed supplied no body for this entry)")
        if doc.link:
            print(f"read it at: {doc.link}")
        return 0
    print(body)
    return 0


def cmd_cat(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    path = resolve(config, args.ref)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReaderError(f"{path}: {exc}") from exc
    sys.stdout.buffer.write(data)
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    path = resolve(config, args.ref)
    doc = load_entry(path)
    if not doc.link:
        raise ReaderError(f"{args.ref}: entry has no link")
    if args.json:
        _emit_json({"link": doc.link})
    else:
        print(doc.link)
    return 0


def cmd_links(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    path = resolve(config, args.ref)
    doc = load_entry(path)
    links = extract_links(doc.content)
    if args.json:
        _emit_json([{"label": label, "href": href} for label, href in links])
        return 0
    if not links:
        print("no links in this entry")
        return 0
    for label, href in links:
        print(f"{href}\t{label}")
    return 0


def _resolve_browser(args: argparse.Namespace, reader_config: ReaderConfig) -> list[str] | None:
    candidate = args.browser or reader_config.browser or os.environ.get("BROWSER")
    if candidate:
        return shlex.split(candidate)
    if shutil.which("xdg-open"):
        return ["xdg-open"]
    return None


def cmd_open(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    path = resolve(config, args.ref)
    doc = load_entry(path)
    if not doc.link:
        raise ReaderError(f"{args.ref}: entry has no link")
    cmd = _resolve_browser(args, reader_config)
    if cmd is None:
        print(doc.link)
        print("rss: no browser configured ($BROWSER, --browser, or xdg-open)", file=sys.stderr)
        return 1
    try:
        subprocess.Popen(
            [*cmd, doc.link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        print(doc.link)
        print(f"rss: could not launch {cmd[0]!r}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        _emit_json({"link": doc.link, "browser": cmd[0]})
    else:
        print(f"opening {doc.link}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    if args.feed and args.feed not in feed_names(config):
        raise ReaderError(f"no such feed: {args.feed!r}")
    query = args.query.lower()
    limit = args.n if args.n is not None else DEFAULT_LIST_LIMIT

    # Matching needs a full-text look at each candidate, so unlike `ls` the
    # -n/--limit can't skip the load entirely -- but scanning newest-first
    # and stopping as soon as we have enough matches still means we stop
    # opening files instead of reading all of them just to discard the rest.
    candidates = _all_refs(config, args.feed)
    matches: list[tuple[EntryRef, EntryDoc]] = []
    for ref in candidates:
        if limit > 0 and len(matches) >= limit:
            break
        try:
            doc = load_entry(ref.path)
        except ReaderError:
            continue
        body = "".join(doc.content.itertext()) if doc.content is not None else ""
        if query in doc.title.lower() or query in body.lower():
            matches.append((ref, doc))

    if args.json:
        out = []
        for ref, doc in matches:
            entry = _ref_json(ref, config)
            entry["id"] = doc.id
            entry["title"] = doc.title
            entry["link"] = doc.link
            entry["summary"] = plain_summary(doc.content)
            out.append(entry)
        _emit_json(out)
        return 0

    if not matches:
        print("no matches")
        return 0
    for ref, doc in matches:
        date = _format_date(ref.published, reader_config.date_format)
        id8 = _color(args, ref.id8, "36")
        print(f"{id8}  {ref.feed:<16} {date:<10}  {doc.title}")
    return 0


def cmd_revisions(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    path = resolve(config, args.ref)
    revs = revisions(path)
    if args.json:
        out = []
        for rev_path in revs:
            try:
                size = rev_path.stat().st_size
            except OSError:
                size = None
            out.append(
                {
                    "revision": _rev_num(rev_path),
                    "path": config.relative(rev_path),
                    "size": size,
                    "current": rev_path.name == path.resolve().name,
                }
            )
        _emit_json(out)
        return 0
    resolved_name = path.resolve().name
    for rev_path in revs:
        marker = "*" if rev_path.name == resolved_name else " "
        try:
            size = rev_path.stat().st_size
        except OSError:
            size = 0
        print(f"{marker} r{_rev_num(rev_path)}  {size:>8} bytes  {rev_path.name}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    path = resolve(config, args.ref)
    revs = revisions(path)
    if len(revs) < 2:
        raise ReaderError(f"{args.ref}: only one revision stored, nothing to diff")

    by_num = {_rev_num(p): p for p in revs}
    if args.rev:
        try:
            a_str, b_str = args.rev.split(":", 1)
            a_num, b_num = int(a_str), int(b_str)
        except ValueError as exc:
            raise ReaderError(f"--rev must look like A:B, got {args.rev!r}") from exc
        if a_num not in by_num or b_num not in by_num:
            available = ", ".join(str(n) for n in sorted(by_num))
            raise ReaderError(f"revision(s) not found; available: {available}")
    else:
        nums = sorted(by_num)
        a_num, b_num = nums[-2], nums[-1]

    a_path, b_path = by_num[a_num], by_num[b_num]

    if args.xml:
        a_text = a_path.read_text(errors="replace")
        b_text = b_path.read_text(errors="replace")
    else:
        width = _effective_width(args, reader_config)
        a_doc = load_entry(a_path)
        b_doc = load_entry(b_path)
        a_text = to_text(a_doc.content, width=width, marks=reader_config.marks, link_refs=False)
        b_text = to_text(b_doc.content, width=width, marks=reader_config.marks, link_refs=False)

    diff_lines = list(
        difflib.unified_diff(
            a_text.splitlines(),
            b_text.splitlines(),
            fromfile=f"r{a_num}/{a_path.name}",
            tofile=f"r{b_num}/{b_path.name}",
            lineterm="",
        )
    )
    diff_text = "\n".join(diff_lines)

    if args.json:
        _emit_json(
            {
                "from": a_num,
                "to": b_num,
                "changed": bool(diff_lines),
                "diff": diff_text,
            }
        )
        return 0

    if not diff_lines:
        print(f"r{a_num} and r{b_num} render identically")
        return 0
    print(diff_text)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    config, _rc = _load(args)
    path = resolve(config, args.ref)
    doc = load_entry(path)
    if args.json:
        _emit_json(_doc_json(doc, config))
        return 0
    rows = [
        ("id", doc.id),
        ("id8", doc.id8),
        ("feed", doc.feed),
        ("revision", str(doc.revision)),
        ("title", doc.title),
        ("author", doc.author or "-"),
        ("link", doc.link or "-"),
        ("guid", doc.guid or "-"),
        ("published", f"{_dt_json(doc.published)} ({doc.published_origin})"),
        ("first-seen", _dt_json(doc.first_seen) or "-"),
        ("updated", _dt_json(doc.updated) or "-"),
        ("categories", ", ".join(doc.categories) or "-"),
        ("content-origin", doc.content_origin),
        ("content-hash", doc.content_hash or "-"),
        ("path", config.relative(doc.path)),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    root = Path(args.root)
    reader_config, source = load_reader_config(root)
    if args.json:
        _emit_json(
            {
                "source": str(source) if source else None,
                "settings": dict(reader_config.describe()),
            }
        )
        return 0
    print(f"source: {source if source else '(defaults; no config file found)'}")
    for key, value in reader_config.describe():
        print(f"  {key:<16} {value}")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    config, reader_config = _load(args)
    try:
        from .tui.app import run_tui
    except ImportError:
        print("rss: the TUI needs the [tui] extra -- run `uv sync --extra tui`", file=sys.stderr)
        return 1
    return run_tui(config, reader_config)


# ── argument parsing ─────────────────────────────────────────────────────


#: Global flags need to work in both positions -- `rss --root demo feeds` and
#: `rss feeds --root demo`, matching `rssd`'s own `--root`-after-subcommand
#: convention. That means the same four flags are declared on the top-level
#: parser AND on every subparser (`parents=[_global]`). The naive version of
#: that double-declaration has a classic argparse trap: both parsers write
#: into the same Namespace, and whichever one parses second silently
#: stomps the first with its own default -- so `--root demo feeds` would
#: read the subparser's default ('.') right over the value the top-level
#: parser just set. The fix is `default=argparse.SUPPRESS` on the shared
#: parser: an unset flag then adds nothing to the Namespace at all, so it
#: can never overwrite a value the other parser already set. Only the
#: top-level parser supplies the real defaults via `set_defaults`, applied
#: before any parsing happens, and a flag actually given after the
#: subcommand still wins because it runs last.
def _global_flags() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="instance root directory")
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit machine-readable JSON"
    )
    common.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="disable ANSI colour"
    )
    common.add_argument(
        "--width",
        type=int,
        default=argparse.SUPPRESS,
        help="wrap width (0 = config, then terminal size)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_flags()

    parser = argparse.ArgumentParser(
        prog="rss",
        description="Read what rssd wrote. Opens nothing for writing, ever.",
        parents=[common],
    )
    parser.set_defaults(root=".", json=False, no_color=False, width=0)

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_, parents=[_global_flags()])

    add("feeds", "list feeds with health, entry count, last poll").set_defaults(func=cmd_feeds)

    p_ls = add("ls", "list entries, newest first")
    p_ls.add_argument("feed", nargs="?", default=None, help="restrict to one feed")
    p_ls.add_argument(
        "-n",
        "--limit",
        dest="n",
        type=int,
        default=None,
        help=f"limit to N entries, 0 for unlimited (default: {DEFAULT_LIST_LIMIT})",
    )
    p_ls.add_argument("--long", action="store_true", help="load each entry for author/summary")
    p_ls.set_defaults(func=cmd_ls)

    p_show = add("show", "render an entry's article text")
    p_show.add_argument("ref", help="path, id8 prefix, 'feed:N', or bare feed name")
    p_show.set_defaults(func=cmd_show)

    p_cat = add("cat", "print an entry's raw XML, unmodified")
    p_cat.add_argument("ref")
    p_cat.set_defaults(func=cmd_cat)

    p_link = add("link", "print the entry's source URL, nothing else")
    p_link.add_argument("ref")
    p_link.set_defaults(func=cmd_link)

    p_links = add("links", "list every link found in an entry")
    p_links.add_argument("ref")
    p_links.set_defaults(func=cmd_links)

    p_open = add("open", "open the entry's source URL in a browser")
    p_open.add_argument("ref")
    p_open.add_argument("--browser", default=None, help="override the browser command")
    p_open.set_defaults(func=cmd_open)

    p_search = add("search", "match titles and body text")
    p_search.add_argument("query")
    p_search.add_argument("feed", nargs="?", default=None)
    p_search.add_argument(
        "-n",
        "--limit",
        dest="n",
        type=int,
        default=None,
        help=f"stop after N matches, 0 for unlimited (default: {DEFAULT_LIST_LIMIT})",
    )
    p_search.set_defaults(func=cmd_search)

    p_revs = add("revisions", "list stored revisions of an entry")
    p_revs.add_argument("ref")
    p_revs.set_defaults(func=cmd_revisions)

    p_diff = add("diff", "unified diff between two revisions")
    p_diff.add_argument("ref")
    p_diff.add_argument("--rev", default=None, help="A:B revision numbers (default: last two)")
    p_diff.add_argument("--xml", action="store_true", help="diff raw XML instead of rendered text")
    p_diff.set_defaults(func=cmd_diff)

    p_info = add("info", "metadata: id, feed, dates, origin, hash, path")
    p_info.add_argument("ref")
    p_info.set_defaults(func=cmd_info)

    add("config", "effective settings and which file they came from").set_defaults(
        func=cmd_config
    )

    add("tui", "launch the terminal visualiser").set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (ReaderError, ConfigError) as exc:
        print(f"rss: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
