# rssd

A file-based RSS daemon. It watches a folder of XML subscription files, polls
those feeds, and materialises every entry as an individual XML file in a
per-feed folder.

**The filesystem is the API.** There is no database, no socket to connect to and
no client library. Any program that can read a directory is a client — `grep`,
`find`, `fswatch`, a shell script, your editor. The bundled TUI is deliberately
built the same way: it reads the output tree and the event log like anyone else
would, with no privileged channel back to the daemon.

```
demo/
├── feeds.d/                    you write these
│   └── rust-blog.xml
├── store/                      the daemon writes these
│   └── rust-blog/
│       ├── feed.xml
│       ├── status.xml
│       └── entries/
│           ├── 20260907T000000Z-9f2c1a3b-crates-io-update.xml     → newest
│           ├── 20260907T000000Z-9f2c1a3b-crates-io-update.r1.xml
│           └── 20260907T000000Z-9f2c1a3b-crates-io-update.r2.xml
└── var/
    └── events.jsonl            tail -F this
```

## Quick start

```bash
mise install
uv sync --extra tui

uv run rssd init demo/
uv run rssd daemon --root demo --poll-now   # terminal 1
uv run rss tui --root demo                  # terminal 2
tail -F demo/var/events.jsonl | jq -c       # terminal 3
```

Drop a new `.xml` file into `demo/feeds.d/` and the daemon picks it up without a
restart. Delete one and it stops polling — but never deletes what it already
harvested.

### Offline

Every fixture is committed, so the whole thing runs with no network at all:

```bash
uv run rssd init demo/ --fixture-mode
uv run rssd serve-fixtures --dir demo/fixtures    # terminal 0
uv run rssd daemon --root demo --poll-now
```

## What an entry looks like

Feed HTML is mapped onto a small fixed vocabulary rather than passed through, so
every entry has the same shape no matter how the publisher writes their markup.
Wrapper `div`s, `class` attributes and publisher CSS hooks are gone; relative
URLs are resolved; `javascript:` and `data:` URLs are dropped.

```xml
<entry xmlns="https://rssd.dev/entry/1"
       id="sha256:9f2c1a3b…" revision="1" first-seen="2026-09-15T21:14:02Z">
  <source>
    <link>https://blog.rust-lang.org/2026/07/13/crates-io-development-update/</link>
    <feed name="rust-blog" title="Rust Blog">https://blog.rust-lang.org/feed.xml</feed>
    <guid basis="atom-id">https://blog.rust-lang.org/2026/07/13/…</guid>
  </source>
  <title>crates.io: development update</title>
  <published origin="feed">2026-07-13T00:00:00Z</published>
  <content origin="feed:content" hash="sha256:bc167659…">
    <paragraph>Another six months have passed since our
      <link href="https://blog.rust-lang.org/2026/01/21/…">last update</link>.</paragraph>
    <heading level="2">Source Code Viewer</heading>
    <list><item>Browse published crate versions</item></list>
  </content>
</entry>
```

`origin` tells you where the body came from: `feed:content`, `feed:summary`,
`fulltext:trafilatura`, or `none`. rssd never fabricates content — a feed that
ships only a title and a link produces an entry that says so.

## Three invariants

**Reads are sacred.** Every file is written to a temporary name in the same
directory, fsynced, then `rename(2)`d into place. A reader sees the old file or
the new one, never a mix, and never a half-written one. A `SIGKILL` mid-write can
only leave a `.rssd-tmp-*` orphan, which is swept on the next start. Every
document is re-parsed with a strict XML parser *before* the rename, so a
malformed entry can never reach the tree.

**The tree is the truth; `var/` is a disposable cache.** Each entry file embeds
its own ID and content hash, so `rm -rf var/` is fully recoverable — the daemon
rebuilds what it knows by scanning the tree, and writes nothing.

**An entry file is written only when its content changes.** No heartbeat, no
`last-seen` field. If nothing changed upstream, nothing on disk is touched, so
`mtime` means exactly what a reader expects it to mean.

## Revisions

Entry files are never mutated. When a publisher edits a post, the new version
lands beside the old one as `.r2.xml` and a stable symlink is repointed:

```bash
uv run rssd demo-mutate --root demo   # forces a revision on demand
```

Follow the symlink for "current", or list `.rN` for the full history. Because
the change is detected by hashing the *rendered semantic content* rather than
the raw HTML, a reordered attribute or a rotating ad token can't manufacture a
revision. Three further guards — a daily per-entry cap, rotating-GUID detection,
and a flat cap on new entries per poll — keep a misbehaving feed from filling
the disk.

## Events

Everything the daemon does is appended to `var/events.jsonl`, one JSON object
per line, with a sequence number that is monotonic across restarts:

```json
{"v":1,"seq":42,"ts":"2026-09-15T21:14:02.140Z","event":"entry.new","feed":"rust-blog","data":{"id":"sha256:9f2c…","path":"store/rust-blog/entries/2026…xml","title":"crates.io: development update"}}
```

Events are emitted *after* the corresponding file is on disk, so **if you see
`entry.new`, the file exists and is complete**. The converse isn't promised — a
crash between the rename and the append loses the event — so consumers reconcile
by scanning the tree when they see `daemon.started`.

Use `tail -F`, not `tail -f`: the log rotates, and `-f` follows the inode and
goes quietly dead.

## Politeness

Conditional GET via `ETag`/`Last-Modified`; `Cache-Control: max-age` raises the
poll interval; `Retry-After` on 429/503 is honoured and isn't counted as a
failure. Feeds that send no validators at all are compared by body hash and
polled less often, since there's no cheap way to ask. Failures back off
exponentially with jitter, per feed, and one broken feed never affects another.

Fetching article pages is **opt-in per subscription** and off by default — the
normal footprint is one request per feed, never one per entry.

## Commands

Two binaries, on purpose. `rssd` writes the tree; `rss` only reads it.

```
rssd init <root>              scaffold an instance  [--fixture-mode]
rssd daemon --root <root>     run it  [--poll-now]
rssd once --root <root>       one poll pass, then exit
rssd poll <feed> --root <r>   force-poll one feed
rssd validate --root <root>   check every subscription parses
rssd prune --retired --yes    delete retired folders (never automatic)
rssd serve-fixtures           local HTTP server over the fixtures
rssd demo-mutate --root <r>   force a revision on demand
```

```
rss feeds                     what's subscribed, and is it healthy
rss ls [feed]                 entries, newest first
rss show <ref>                read an entry as wrapped text
rss link <ref>                print just the URL
rss open <ref>                open it in a browser
rss links <ref>               every link in an entry
rss search <query> [feed]     match titles and body text
rss revisions <ref>           every stored version
rss diff <ref>                what changed when it was edited
rss info <ref>                id, dates, origin, hash, path
rss config                    effective settings and their source
rss tui                       the terminal reader
```

Refer to an entry by id prefix, position, feed, or path — all four work:

```bash
rss show 9f2c1a3b            # unique id prefix
rss show rust-blog:3         # 3rd newest in a feed
rss show rust-blog           # newest in a feed
rss show demo/store/rust-blog/entries/2026….xml
```

An ambiguous prefix lists the candidates rather than guessing.

## The TUI

```bash
rss tui --root demo
```

Modal, like vim. Three panes — feeds, entries, reader — a status line, and no
chrome. `hjkl` to move, `Enter` to read, `o` to open in a browser, `y` to yank
the URL, `/` to search, `:` for ex commands (`:e rust-blog`, `:set width=100`,
`:links`, `:open 3`, `:q`).

`:set` changes the session only, exactly like vim; persistent preferences go in
`rss.toml`.

The daemon doesn't need to be running — the TUI reads the filesystem, so it
works fine against a static tree or a directory you rsynced from elsewhere.

## Development

```bash
uv sync --extra tui
uv run pytest            # 322 tests, no network required
```

The pure modules — `semantic`, `identity`, `parse`, `render`, `schedule`,
`diff`, `timeutil`, `subscriptions` — hold everything that can actually be
*wrong*, and are tested against committed byte-exact captures of six real feeds
chosen to cover the awkward cases: rich Atom with `xml:base`, RSS with
`content:encoded`, CRLF line endings, an image-only body, and one feed that
sends no cache validators at all.

`SPEC.md` is the full specification.
