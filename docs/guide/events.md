# Events

Everything the daemon does appends one JSON object to `var/events.jsonl`.

```json
{"v":1,"seq":42,"ts":"2026-09-15T21:14:02.140Z","event":"entry.new","feed":"rust-blog","data":{"id":"sha256:9f2c…","path":"store/rust-blog/entries/2026…xml","title":"crates.io: development update"}}
```

Key order is stable, because greppability is a feature:

```bash
tail -F demo/var/events.jsonl | jq -c 'select(.event == "entry.new")'
grep '"entry.revised"' demo/var/events.jsonl | wc -l
```

## The guarantee

Events are emitted **after** the corresponding file is on disk and its directory
has been fsynced:

> If you see `entry.new`, the file exists and is complete.

The reverse isn't promised. A crash between the rename and the append loses the
event, so a consumer should reconcile by scanning the tree whenever it sees
`daemon.started`.

## Sequence numbers

`seq` is monotonic across restarts, recovered on boot as the max of a
checkpoint file and the log's own last line. That gives consumers resumable
"everything after N" semantics — a client that was down can pick up exactly
where it left off.

All `path` values are relative to the instance root, so they resolve wherever
the instance lives.

## Taxonomy

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

Event names are validated against this set on emit, so a typo fails loudly in
tests instead of silently producing an event nobody is listening for.

::: warning Use `tail -F`, not `tail -f`
The log rotates at 16 MiB. `tail -f` follows the inode and goes quietly dead the
moment that happens; `-F` follows the name. A `log.rotated` event is written as
the last line of the old file and the first line of the new one.
:::

## Reacting to events

There is no plugin system and no hook directory — a line-oriented log plus the
tools you already have covers it:

```bash
# desktop notification on every new entry
tail -F demo/var/events.jsonl \
  | jq -r --unbuffered 'select(.event=="entry.new") | .data.title' \
  | while read -r title; do notify-send "new: $title"; done
```

```bash
# archive every revision to a git repo
tail -F demo/var/events.jsonl \
  | jq -r --unbuffered 'select(.event=="entry.revised") | .data.path' \
  | while read -r p; do git -C demo add "$p" && git -C demo commit -qm "$p"; done
```
