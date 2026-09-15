# How it works

Three invariants drive most of the design. Nearly every question about rssd's
behaviour is answered by one of them.

## I1 — The tree is the truth, `var/` is a cache

Every entry file embeds its own ID and content hash. That single decision is
what makes daemon state disposable:

```bash
rm -rf demo/var
uv run rssd once --root demo
```

The daemon rebuilds everything it knows by scanning the tree, recognises all
100 entries, and writes nothing. No reindex, no repair mode, no resync.

The cost is one unconditional HTTP request per feed, and the rescan is
idempotent — which is why a corrupt or truncated state file is simply discarded
rather than carefully recovered.

## I2 — A file is written only when its content changes

There is no `last-seen` field and no heartbeat. If nothing changed upstream,
nothing on disk is touched, so `mtime` means exactly what a reader assumes it
means.

This matters more here than it would elsewhere, because rssd keeps every
revision: a heartbeat field would spawn a fresh `.rN` for every entry on every
poll and bury the tree within a day.

Change detection hashes the **rendered semantic content**, not the source HTML.
Since the semantic mapping has already discarded wrapper `div`s, `class`
attributes and whitespace churn, a publisher reordering their markup can't
manufacture a revision.

## I3 — Never write a file that isn't well-formed XML

Every document is re-parsed with a strict parser *before* the rename. If the
content subtree can't be made well-formed it degrades to a plain-text body and
an `entry.content-degraded` event — but what lands in the tree always parses.

```bash
xmllint --noout demo/store/*/entries/*.xml && echo "all well-formed"
```

Readers get to assume this. It's the whole reason the format is XML rather than
"mostly XML".

## Atomic writes

```python
fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rssd-tmp-")
os.write(fd, data); os.fsync(fd); os.close(fd)
os.replace(tmp, path)
```

The temp file **must** live in the destination directory — `os.replace` across
filesystems raises `EXDEV`, and `/tmp` is very often a different filesystem from
your store. Temp names carry both a dot prefix and a non-`.xml` suffix so no
`ls`, glob, or `find -name '*.xml'` can ever see one.

A `SIGKILL` mid-write can only leave a `.rssd-tmp-*` orphan, swept on next
start. A real entry path is never partial, because it only comes into being via
a rename of a fully-fsynced file.

Directory fsync is batched once per poll rather than per file. On btrfs that's
the difference between milliseconds and hundreds of them.

## Ordering, and the promise that follows

Within one poll:

```
write entry temps → rename them → fsync the directory
→ update state → rewrite status.xml → emit events
```

Events strictly last. That buys a contract worth relying on:

> If you see `entry.new`, the file exists and is complete.

The converse is **not** promised. A crash between the rename and the append
loses the event, so consumers reconcile by scanning the tree when they see
`daemon.started`. A contract you can't keep is worse than a weaker one.

## Politeness

Conditional GET via `ETag` / `Last-Modified`. `Cache-Control: max-age` raises
the poll interval. `Retry-After` on 429 and 503 is honoured and explicitly *not*
counted as a failure — it's cooperative, not an error.

Feeds that send no validators at all get their response body hashed instead, so
an unchanged feed skips parsing entirely, and their interval floor is raised —
if you can't check cheaply, check less often.

Failures back off exponentially with jitter, per feed. One broken feed never
affects another, and a feed that's been down for a week reports it once, not on
every poll.

Fetching article pages is **opt-in per subscription** and off by default. The
normal footprint is one request per feed, never one per entry.

## Guards

Keeping every revision is the sharpest edge in the design, so three guards sit
behind it:

- **A daily per-entry revision cap.** A flapping feed gets eight revisions a
  day, not eight hundred.
- **Rotating-GUID detection.** A feed whose IDs all change every poll while the
  content stays identical gets permanently pinned to content-derived identity.
- **A flat cap on new entries per poll**, skipped on a feed's first poll —
  adopting a feed's whole window is the point of subscribing.
