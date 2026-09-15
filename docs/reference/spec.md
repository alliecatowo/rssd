# Specification

The full specification lives in
[`SPEC.md`](https://github.com/alliecatowo/rssd/blob/main/SPEC.md) in the
repository. It was written before the implementation and is the document the
code was built against.

It covers:

| Section | |
|---|---|
| §1 | The three governing invariants |
| §4 | On-disk layout, feed folder names, entry filenames, revisions |
| §5 | Subscription file format |
| §6 | Entry document format |
| §7 | The semantic content vocabulary and what it guards against |
| §8 | Atomic writes, write ordering, crash recovery |
| §9 | Entry identity, content hashing, revision churn guards |
| §10 | The polling engine, conditional GET, backoff, feedparser's sharp edges |
| §11 | Subscription hot-reload and the deletion policy |
| §12 | The JSONL event log |
| §13 | Module layout, pure vs effectful |
| §14 | The reference feeds and what each one exercises |

## It was wrong in four places

Worth saying plainly, because a spec presented as infallible is less useful than
one with its scars visible. Implementing it surfaced four real errors, all since
corrected in both the spec and the code:

1. **The anomaly cap could never fire.** It was specified as
   `max(item_count, 100)`, but the count of new entries is bounded by the item
   count, so the guard was dead code. The obvious repair was worse: it deadlocks
   a large feed permanently, because an anomaly writes nothing, so the seen-set
   stays empty and every subsequent poll trips the same guard. It is now a flat
   cap, skipped on a feed's first poll.

2. **A reference feed was mischaracterised.** The spec claimed `hnrss.org`
   entries carry no body at all. They carry a `<description>` of link metadata,
   so `feed:summary` is the correct classification — and hn is precisely the
   feed that wants `<fulltext>true</fulltext>`.

3. **An event name was missing from the taxonomy.** `feed.metadata-changed` was
   emitted but never declared. The validation assert caught it on the first real
   run, which is what it was for.

4. **`var/seq` was fsynced on every event**, so publishing one poll's worth of
   entries meant a hundred synchronous writes. It is now checkpointed
   periodically; recovery already takes the max of the checkpoint and the log's
   last line, so a stale checkpoint costs nothing.

## Reference feeds

The test suite runs against byte-exact captures of six real feeds, chosen so
that between them they exercise every code path:

| Feed | Exercises |
|---|---|
| `lobste.rs/rss` | high churn, ETag + Last-Modified |
| `blog.rust-lang.org/feed.xml` | Atom, full `<content>`, per-entry `xml:base` |
| `simonwillison.net/atom/everything/` | rich escaped HTML, CRLF line endings |
| `hnrss.org/frontpage` | metadata-only body, no ETag |
| `xkcd.com/rss.xml` | body is a single image |
| `go.dev/blog/feed.atom` | **no cache validators at all** → body-hash path |

The fixtures are committed with `-text` in `.gitattributes`, because git
normalising their line endings would silently defeat the charset tests.
