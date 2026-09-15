# rssd — Specification v1

A file-based RSS/Atom daemon. It watches a folder of XML subscription files, polls
those feeds, and materialises every entry as an individual XML file in a per-feed
folder. **The filesystem is the API** — any program that can read a directory is a
client.

## 0. Priorities

1. **Reads are sacred.** A reader must never observe a partial, corrupt, or
   non-well-formed file. Ever.
2. **The daemon is the product.** The TUI is a visualiser that reads the output
   tree and nothing else — no privileged channel, no IPC. If the TUI can be built
   that way, the file interface is provably sufficient for third parties.
3. **Lifecycle events** stream to an append-only JSONL log anything can `tail -F`.

## 1. Governing invariants

Three rules that resolve most design questions on their own.

**I1. The entry tree is the truth; `var/` is a disposable cache.**
Every entry file embeds its own ID and content hash, so `rm -rf var/` is fully
recoverable by scanning the tree.

**I2. An entry file is written only when its content changes.**
No `last-seen` field, no heartbeat. Otherwise every poll spawns a new revision and
`mtime` stops meaning anything to readers. This matters more here than usual
because rssd keeps every revision.

**I3. Never write a file that is not well-formed XML.**
Every serialised entry is re-parsed with a strict parser before the rename.
Failures degrade to a plain-text body rather than emitting broken XML.

## 2. Locked decisions

| Decision | Choice |
|---|---|
| Language | Python 3.14.7 (pinned via mise), `uv` for packaging |
| Entry content | Semantic vocabulary (`<paragraph>`, `<heading>`, `<link>`…), **not** HTML tag names |
| Events | JSONL log **only**. No `hooks.d/`, no Unix socket |
| Article fetching | **Opt-in per subscription, off by default.** Default = feed URLs only |
| Upstream edits | **Keep every revision.** Files are never mutated; `.rN` + stable symlink |

## 3. Environment (verified)

- Python 3.14.7, `uv` 0.12.7, `mise` 2026.8.15.
- Project dir is **btrfs**; `/tmp` is **tmpfs**; `TMPDIR` unset. `NAME_MAX` = 255.
- `jq`, `tree`, `xmllint` available and used by the demo.
- Dependencies: core `feedparser httpx lxml watchfiles` (11 packages resolved);
  `[tui]` adds `textual`; `[fulltext]` adds `trafilatura`.

## 4. On-disk layout

```
<root>/
├── feeds.d/                     INPUT — one XML file per subscription
│   ├── rust-blog.xml
│   └── lobsters.xml
├── store/                       OUTPUT — contains ONLY feed folders
│   └── rust-blog/
│       ├── feed.xml             stable metadata — rewritten only when it changes
│       ├── status.xml           volatile — last poll, health, failure count
│       └── entries/
│           ├── 20260907T000000Z-9f2c1a3b-rust-debugging-survey.xml   -> symlink
│           ├── 20260907T000000Z-9f2c1a3b-rust-debugging-survey.r1.xml
│           └── 20260907T000000Z-9f2c1a3b-rust-debugging-survey.r2.xml
└── var/                         daemon-private + the event stream
    ├── events.jsonl             the public event API
    ├── seq                      last emitted seq (monotonic across restarts)
    ├── rssd.lock                exclusive flock — refuses a second daemon
    └── state/rust-blog.json     etag, last-modified, body hash, backoff, counters
```

Bookkeeping lives in a **sibling directory, not a dotdir inside `store/`** —
`grep -r`, `find`, and `rsync` all traverse dotfiles, so physical separation is
the only separation that holds. `ls store/` is always purely feed names.

`feed.xml` is split from `status.xml` so a consumer watching `feed.xml` is not
woken every poll by a changed timestamp. This is a reader-first call (priority 1).

### 4.1 Feed folder name

The subscription filename stem: `feeds.d/rust-blog.xml` -> `store/rust-blog/`.
Validated against `^[a-z0-9][a-z0-9._-]{0,63}$`. A `<name>` element in the
subscription overrides it.

### 4.2 Entry filename

```
{published:%Y%m%dT%H%M%SZ}-{id8}-{slug}.xml
```

- Lexicographic sort == chronological sort. Non-negotiable.
- Compact ISO, **no colons** — legal on Linux but hostile to shell completion and `scp`.
- `id8` = first 8 hex chars of the entry ID. Collision-free, and gives readers a
  file -> ID mapping without opening the file.
- `slug` = title, NFKD-normalised, lowercased, non-alphanumerics collapsed to `-`,
  truncated to 48 chars on a word boundary. Empty -> `untitled`.
- **The filename never changes after first write.** It is keyed on `published`,
  never on `updated`.

Date edge cases, both of which otherwise wreck `ls` ordering on real feeds:

- No date -> use first-seen time, record `origin="first-seen"` in the file.
  Never omit the stamp; sortability is non-negotiable.
- Future-dated (a common feed bug) -> clamp to `now + 24h`, record
  `origin="clamped"`. Otherwise one bad entry pins itself to the top forever.

### 4.3 Revisions

Entry content is immutable once written. A changed entry produces a new revision
file beside the old one, and a stable symlink is repointed:

```
<base>.xml      -> symlink to the newest <base>.rN.xml
<base>.r1.xml   first version ever written
<base>.r2.xml   content changed upstream
```

Readers follow the symlink for "current", or enumerate `.rN` for history.
Revision numbers are recovered on restart by scanning the directory (I1).

## 5. Subscription file format

`feeds.d/<name>.xml`:

```xml
<subscription>
  <url>https://blog.rust-lang.org/feed.xml</url>
  <name>rust-blog</name>       <!-- optional; defaults to filename stem -->
  <interval>15m</interval>     <!-- optional; 30s/5m/2h/1d, clamped [60s, 6h] -->
  <fulltext>false</fulltext>   <!-- optional; default false -->
</subscription>
```

The URL extractor is deliberately liberal — users hand-edit these. Try in order:
`/subscription/url`, `/subscription/link`, `//outline/@xmlUrl` (OPML fragments),
`//link[@rel='self']/@href`, then the first `http(s)://` text node in the document.

A file that fails to parse emits `subscription.invalid` and the **previous good
state for that path is retained**. A transient bad read never takes down a
working feed.

## 6. Entry file format

```xml
<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="https://rssd.dev/entry/1"
       id="sha256:9f2c1a3b…" revision="2"
       first-seen="2026-09-15T21:14:02Z" updated="2026-09-15T23:01:44Z">
  <source>
    <link>https://blog.rust-lang.org/2026/09/07/rust-debugging-survey-2026-results/</link>
    <feed name="rust-blog" title="Rust Blog">https://blog.rust-lang.org/feed.xml</feed>
    <guid basis="atom-id">https://blog.rust-lang.org/2026/09/07/…</guid>
  </source>
  <title>Rust debugging survey 2026 results</title>
  <author>The Rust Team</author>
  <published origin="feed">2026-09-07T00:00:00Z</published>
  <categories><category>rust</category></categories>
  <content origin="feed:content" hash="sha256:…">
    <paragraph>One of the biggest challenges Rust developers report in our
      <link href="https://blog.rust-lang.org/2026/03/02/…">annual surveys</link>
      is a subpar debugging experience.</paragraph>
    <heading level="2">Results</heading>
    <list><item>Faster builds</item></list>
    <quote><paragraph>It just works.</paragraph></quote>
    <image src="https://…" alt="chart"/>
  </content>
</entry>
```

- `content/@origin`: `feed:content` | `feed:summary` | `fulltext:trafilatura` | `none`
- `published/@origin`: `feed` | `first-seen` | `clamped`
- `source/guid/@basis`: `atom-id` | `guid` | `link` | `content`
- The in-file `id` and `content/@hash` are what make `var/` disposable (I1).
- An entry with no usable body emits `<content origin="none"/>`. **Never fabricate.**

## 7. Semantic content vocabulary

**Block:** `paragraph`, `heading[level]`, `list[ordered]`/`item`, `quote`,
`code[language]`, `image[src,alt]`, `separator`
**Inline:** `link[href]`, `emphasis`, `strong`, `code`, `break`

### 7.1 Mapping

| HTML | rssd |
|---|---|
| `p` | `paragraph` |
| `h1`…`h6` | `heading level="1".."6"` |
| `ul` / `ol` | `list` / `list ordered="true"` |
| `li` | `item` |
| `blockquote` | `quote` |
| `pre`, `code` | `code` |
| `img` | `image` |
| `hr` | `separator` |
| `a` | `link` |
| `em`, `i` | `emphasis` |
| `strong`, `b` | `strong` |
| `br` | `break` |
| `div`, `span`, `section`, `article`, `main`, `figure` | **unwrap** (hoist children) |
| anything else | **unwrap to text** |

All `class`, `id`, `style`, and `data-*` attributes are dropped — they are
publisher CSS hooks, meaningless without the publisher's stylesheet.

### 7.2 The mapper is the sanitiser

Because the transform is an allowlist by construction, no separate
`nh3`/`bleach` pass is needed. What still requires explicit guarding:

- **`script`, `style`, `textarea`, `noscript`, `iframe`, `object`, `embed`,
  `form`, `template` must be dropped WITH THEIR SUBTREE.** Unwrapping them
  instead emits script bodies as visible text. Classic sanitiser footgun.
- **URL schemes:** only `http`, `https`, `mailto` survive. `javascript:` and
  `data:` are dropped and the element unwraps to text. `data:` dies even for
  images — a 200KB base64 blob is hostile to `grep`.
- **Relative URLs** resolve against Atom `xml:base` -> entry link -> feed link.
  The rust-lang feed sets `xml:base` per entry, so this is load-bearing.
- **XML-illegal codepoints:** strip C0 controls *and lone surrogates* before
  serialising, or strict parsers reject the output. Legal set is
  `\x09 \x0A \x0D \x20-퟿ -� \U00010000-\U0010FFFF`.
- **Named HTML entities** (`&nbsp;`, `&mdash;`) are undefined in XML.
  Serialising from an lxml tree — where they are already decoded to characters —
  and re-escaping only the five XML entities makes this bug class structurally
  impossible. Worth a code comment saying so.
- **Charset:** decode bytes as XML declaration `encoding=` (sniffed from the
  first 1024 bytes) -> HTTP `Content-Type; charset=` -> UTF-8. Never decode as
  UTF-8 unconditionally.
- **Empty elements** are dropped; whitespace-only text between blocks collapses.

## 8. Atomic writes

```python
def atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rssd-tmp-")  # SAME DIR
    os.write(fd, data); os.fsync(fd); os.close(fd)
    os.replace(tmp, path)   # atomic on btrfs
    # fsync_dir(path.parent) — batched once per poll, not per file
```

**The temp file must live in the destination directory.** Verified live risk:
`/tmp` is tmpfs and the project is btrfs, so `tempfile.mkstemp()` without `dir=`
fails with `EXDEV` on rename.

Temp names carry both a `.` prefix and a non-`.xml` suffix so that neither `ls`,
nor `*.xml` globs, nor `find -name '*.xml'` can ever see them.

Directory fsync is batched **once per poll**, not per file — on btrfs that is the
difference between milliseconds and hundreds of milliseconds.

Symlink swaps use the same trick: `os.symlink(target, tmp)` then
`os.replace(tmp, link)`.

### 8.1 Write ordering per poll

```
write all entry temps (fsync each)
rename all temps into place
fsync entries/ directory
update var/state/<feed>.json
rewrite status.xml (and feed.xml only if metadata changed)
emit events                      <-- STRICTLY LAST
```

This yields a documented contract:

> If you see `entry.new`, the file exists and is complete.

The converse is **not** guaranteed — a crash between rename and append loses
events — so consumers reconcile by scanning the tree on `daemon.started`.
A contract you cannot keep is worse than a weaker one.

### 8.2 Crash recovery

A SIGKILL can only leave `.rssd-tmp-*` orphans; a `*.xml` path is never partial
because it only ever comes into being via `os.replace` of a fully-fsynced file.

On startup: sweep temps older than 60s, then rebuild revision counters and the
seen-set by scanning `entries/` filenames — cheap, since `id8` and revision parse
straight out of the name. **Disk is authoritative.** Unparseable state JSON is
discarded, costing one unconditional GET.

## 9. Entry identity and revisions

### 9.1 Stable ID

Precedence, yielding `(raw, basis)`:

1. Atom `<id>` -> `atom-id`
2. RSS `<guid>` -> `guid` (`isPermaLink="false"` is fine)
3. Canonicalised `<link>` -> `link` (strip `utm_*`, `fbclid`, `gclid`, `ref`, fragment)
4. `sha256(title + "\0" + published + "\0" + first 512 chars of text)` -> `content`

Final: `id = "sha256:" + sha256(feed_name + "\0" + basis + "\0" + raw).hexdigest()`

Namespacing by feed means the same article in two feeds is correctly two entries
in two folders. The raw value is preserved as `<guid basis="…">` for traceability.

### 9.2 Content hash

`content_hash = sha256(title + "\0" + author + "\0" + canonical_content_xml)`

Computed over the **rendered semantic XML**, not the raw HTML. The semantic
mapping already discards wrapper divs, attribute noise, and whitespace churn, so
a reordered `class` attribute or an ad token cannot spawn a spurious `.r2`.
This is the primary defence against revision churn, which is *the* failure mode
for a keep-every-revision design.

### 9.3 New vs revised

- id unseen -> **create** `.r1.xml` + symlink, emit `entry.new`
- id seen, hash equal -> **no-op**. No write, no mtime change, no event. (I2)
- id seen, hash differs -> **new revision** `.rN.xml`, repoint symlink,
  emit `entry.revised`

Entries falling out of the upstream window are **not** deletions. Nothing is
ever removed.

### 9.4 Churn guards

Unbounded revisions are the sharpest edge in this design. Three guards:

- **`max_revisions_per_entry_per_day`** (default 8). Exceeded -> stop writing
  revisions for that entry, emit `entry.revision-suppressed`, flag in `status.xml`.
- **Rotating-guid detection.** If after 2 polls >=80% of IDs are new *and* >50% of
  those "new" entries have a content hash matching an existing entry, permanently
  flip the feed to `basis="content"` identity (sticky, recorded in state) and emit
  `feed.identity-downgraded`.
- **`max_new_entries_per_poll`** (default 100), a *flat* cap. Exceeded ->
  write nothing, emit `feed.anomaly`, mark health degraded. A broken feed must
  not be able to write ten thousand files during a demo.

  The cap must not be scaled by the feed's own size (an earlier draft said
  `max(item_count, 100)`): the count of new entries is bounded by the item
  count, so such a cap can never be exceeded and the guard would be dead code.

  It is also **skipped on a feed's first poll**, when nothing is known yet.
  Adopting a feed's entire window is the point of subscribing. Firing there
  would deadlock a large feed permanently -- an anomaly writes nothing, so the
  seen-set would stay empty and every later poll would trip the guard again.

Seen-set is capped at `max_tracked_entries = 50_000` per feed.

## 10. Polling engine

A single min-heap of `(due_at, feed_name)` with one rearmed timer — not a
task-per-feed with `sleep`, which drifts and fights hot-reload.

Concurrency: global `asyncio.Semaphore(6)` plus a per-host `Semaphore(2)` so
twenty subscriptions to one host do not hammer it.

Startup staggers feeds by ~750ms each, capped at 30s total. `--poll-now`
collapses the stagger for demos.

### 10.1 HTTP

```
User-Agent: rssd/0.1 (+https://github.com/allie/rssd)
Accept: application/atom+xml, application/rss+xml, application/rdf+xml;q=0.9,
        application/xml;q=0.8, text/xml;q=0.7, */*;q=0.1
If-None-Match: <etag>              (only when server-provided)
If-Modified-Since: <last-modified>
```

- 30s total timeout; `follow_redirects=True`, `max_redirects=5`.
- **5 MiB response cap**, enforced by streaming and counting bytes (check
  `Content-Length` first for an early reject). Exceeded -> `feed.too-large`.
- On a permanent redirect, record `resolved_url` in state and poll that, but
  **keep the folder bound to the original subscription** — renaming a directory
  out from under readers and their watches is worse than a stale name.

### 10.2 Interval resolution

1. Subscription `<interval>`
2. `Cache-Control: max-age=N`
3. RSS `<ttl>` or `<sy:updatePeriod>`/`<sy:updateFrequency>`
4. Default 15m

Each clamped to `[60s, 6h]`, then always multiplied by jitter
`(0.9 + random() * 0.2)`.

### 10.3 Errors

- **429/503 with `Retry-After`** (seconds or HTTP-date): honour it, clamp to 6h,
  emit `feed.throttled`, and **do not** increment the failure counter — it is
  cooperative, not an error.
- Otherwise `delay = min(base * 2**min(failures, 8), 6h) * (0.75 + random()*0.5)`.
  Reset `failures` on any 2xx or 304.
- 10 consecutive failures -> `health="failing"`, emitted **edge-triggered once**,
  not on every poll.

### 10.4 Feeds with no validators

`go.dev/blog/feed.atom` sends neither ETag nor Last-Modified. Three-part answer:

1. Store `sha256` of the response body. Unchanged -> skip parsing entirely, emit
   `feed.unchanged`.
2. Raise the interval floor for such feeds to 30m — if we cannot check cheaply,
   check less often.
3. Still send `If-Modified-Since` from the last successful fetch time; harmless,
   and some origins honour it anyway. **Never fabricate an ETag.**

### 10.5 feedparser sharp edges

- It is **synchronous** — run it in `asyncio.to_thread()`. github.blog is 646KB.
- It sets `bozo`/`bozo_exception` on malformed feeds but still returns usable
  partial data. Log it, do not abort.
- `published_parsed` is a UTC `struct_time` needing explicit `timezone.utc`.
- Its module-level `SANITIZE_HTML` and `RESOLVE_RELATIVE_URIS` globals must be
  turned **off** so our own mapper sees the original markup.

## 11. Subscription hot-reload

`watchfiles.awatch(feeds_d)` with a 300ms debounce, ignoring dotfiles, `~`, `.swp`.

Reconciliation is **declarative, not event-driven**: re-read the whole directory,
compute the desired feed set, diff against the running set. That is the only way
to stay idempotent under dropped or duplicated events — a `git checkout` of
`feeds.d/` fires dozens.

Three layers against partial reads: debounce -> transactional parse (either a
complete subscription or a failure) -> keep-last-good on failure.

**Unlink gets a 5-second grace timer.** vim and emacs atomic-save as
unlink-then-create; without the grace window a routine `:w` retires a feed
mid-demo. If a matching add arrives inside the window, carry on as if nothing
happened.

### 11.1 Deletion policy

**Deleting a subscription never deletes harvested data.** On confirmed unlink:
stop polling, write `<state>retired</state>` into `feed.xml`, emit
`subscription.removed`. The `entries/` tree is untouched.

Rationale: deletion is irreversible; the tree may be the input to a downstream
indexer or the user's archive; filesystem events are unreliable; and `rm -rf`
triggered by a file watcher is the most dangerous thing a daemon can do, with a
failure mode that is silent and total. GC is an explicit human command:
`rssd prune --retired`.

Bonus: re-adding a subscription un-retires the folder with every entry intact.

**Never watch `store/`** — the daemon owns it, and watching it is a feedback loop.

## 12. Event log

Append-only JSONL, one event per line, stable key order (greppability is a
feature):

```json
{"v":1,"seq":10421,"ts":"2026-09-15T21:14:02.140Z","event":"entry.new","feed":"rust-blog","data":{"id":"sha256:9f2c…","path":"store/rust-blog/entries/2026…xml","title":"Rust debugging survey 2026 results"}}
```

- Written as a **single `os.write()`** to an `O_APPEND` fd — the kernel will not
  interleave a single sub-page write, so concurrent tasks cannot tear each
  other's lines. Never assemble a line incrementally.
- `seq` is **monotonic across restarts**: persisted in `var/seq`, recovered on
  boot as `max(var/seq, seq of last line)`. This gives consumers resumable
  "everything after N" semantics.
- `var/seq` is checkpointed every 64 events, plus on rotation and on close.
  Because recovery takes the max of the checkpoint and the log's own last line,
  a stale checkpoint costs nothing -- whereas fsyncing it on every event would
  mean 100 synchronous writes to publish one poll's worth of entries.
- All `path` values are relative to the root.

### 12.1 Taxonomy

```
daemon.started  daemon.stopping  daemon.reconciled
subscription.added  subscription.changed  subscription.removed  subscription.invalid
feed.created  feed.retired  feed.redirected  feed.poll-started
feed.poll-succeeded  feed.unchanged  feed.poll-failed  feed.throttled
feed.too-large  feed.identity-downgraded  feed.anomaly  feed.health-changed
feed.metadata-changed
entry.new  entry.revised  entry.revision-suppressed  entry.content-degraded
fulltext.ok  fulltext.error  log.rotated
```

### 12.2 Rotation

Size-based at 16 MiB -> rename to `events-<ISO>.jsonl`, reopen, keep 10.
Emit `log.rotated` as the last line of the old file and the first of the new.

Readers must use `tail -F`, not `tail -f` — the latter follows the inode and
silently goes dead at rotation. Copy-truncate is rejected: it can tear a line.

*(Deferred, not built: `hooks.d/` executables and a Unix socket both layer
cleanly on this same bus later.)*

## 13. Module layout

```
src/rssd/
  __main__.py     python -m rssd
  cli.py          daemon | tui | once | init | validate | prune | poll | serve-fixtures
  config.py       root paths, defaults, limits
  models.py       shared dataclasses — THE CONTRACT between modules
  subscriptions.py  parse feeds.d/*.xml                  PURE
  timeutil.py     date parsing, UTC, clamping, stamps    PURE
  identity.py     stable IDs, content hashing, slugs     PURE
  semantic.py     HTML -> semantic XML tree              PURE
  parse.py        bytes+content-type -> ParsedFeed       PURE
  render.py       Entry -> XML bytes + validation        PURE
  schedule.py     next_interval, backoff, jitter         PURE
  diff.py         seen+parsed -> new/revised/unchanged   PURE
  fetch.py        httpx conditional GET, caps, redirects effectful
  store.py        atomic writes, revisions, symlinks     effectful
  events.py       JSONL log, seq recovery, rotation      effectful
  watcher.py      watchfiles -> declarative reconcile    effectful
  scheduler.py    min-heap, concurrency, per-feed state  effectful
  daemon.py       wiring, flock, signals, recovery       effectful
  fulltext.py     opt-in trafilatura extraction          effectful
  fixtures.py     local fixture HTTP server (demo insurance)
  tui/app.py      Textual app — reads tree + event log only
```

**The split is the point.** Identity, dedupe, scheduling math, date handling,
semantic mapping, and XML correctness — everything that can actually be *wrong* —
lives in the PURE modules and is tested against committed fixture bytes with zero
mocks. Effectful modules stay thin and are integration-tested against a tmpdir
root and a local fixture server.

`tui/` imports nothing from `scheduler`, `fetch`, or `store`.

## 14. Reference feeds

All probed and verified. Chosen so every code path is exercised.

| Feed | URL | Exercises |
|---|---|---|
| `lobsters` | `https://lobste.rs/rss` | high churn, ETag+Last-Modified |
| `rust-blog` | `https://blog.rust-lang.org/feed.xml` | Atom, full `<content>`, `xml:base` |
| `simonw` | `https://simonwillison.net/atom/everything/` | rich escaped HTML, Last-Modified only |
| `hn` | `https://hnrss.org/frontpage` | metadata-only body, no ETag |
| `xkcd` | `https://xkcd.com/rss.xml` | image-only body |
| `godev` | `https://go.dev/blog/feed.atom` | **no validators at all** -> body-hash path |

## 15. CLI

```
rssd init <root>              scaffold feeds.d/ with the reference feeds + fixtures
rssd daemon --root <root>     run the daemon  [--poll-now] [--fixture-mode]
rssd once --root <root>       one poll pass over every feed, then exit
rssd poll <feed> --root <r>   force-poll a single feed
rssd tui --root <root>        Textual visualiser
rssd validate --root <root>   check every subscription file parses
rssd prune --retired          delete retired feed folders (explicit, never automatic)
rssd serve-fixtures [--port]  local HTTP server over tests/fixtures/feeds
rssd demo-mutate              mutate a served fixture to force a revision
```

## 16. Demo script

```bash
mise install && uv sync --extra tui
uv run rssd init demo/
uv run rssd daemon --root demo --poll-now   # terminal 1
uv run rssd tui    --root demo              # terminal 2
tail -F demo/var/events.jsonl | jq -c       # terminal 3
```

Then live: `tree demo/store` · `cat` an entry · `xmllint --noout
demo/store/*/entries/*.xml && echo "all well-formed"` · drop a new file into
`feeds.d/` and watch it get picked up with no restart.

**Demo insurance**, built early rather than last:

- `rssd serve-fixtures` + `--fixture-mode` — if venue wifi dies or a feed 500s,
  the whole demo still runs offline.
- `rssd demo-mutate` — forces a revision on demand. Waiting for a real upstream
  typo-fix is not filmable, and "every revision is kept" is the headline
  behaviour.

## 17. Verification

- `uv run pytest` — pure-pipeline tests over the committed fixture bytes, plus
  adversarial fixtures: `control-chars.xml`, `lone-surrogate.xml`,
  `unescaped-amp.xml`, `latin1-declared.xml`, `no-guid.xml`, `rotating-guid.xml`,
  `rdf.xml`, `future-dated.xml`, `empty-items.xml`.
- **Well-formedness gate** — every produced entry round-trips through `xmllint --noout`.
- **Atomicity** — hammer `write_entry()` while a reader process parses every file
  it sees; assert zero parse errors and zero `.rssd-tmp-*` sightings.
- **Crash** — SIGKILL mid-poll, restart, assert no temps survive, no duplicates,
  revision counters resume, and `rm -rf var/` still recovers fully from the tree.
- **Churn** — replay `rotating-guid.xml` 10 times; assert identity downgrade
  fires and no `.r3` is ever written.
- **Offline** — the whole suite runs against `rssd serve-fixtures`, never the network.
