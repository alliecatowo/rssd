# Getting started

## Requirements

Python 3.14 and [uv](https://docs.astral.sh/uv/). If you use
[mise](https://mise.jdx.dev/), the pinned versions come from `mise.toml`.

```bash
git clone https://github.com/alliecatowo/rssd
cd rssd
mise install          # optional, pins python 3.14.7 + uv
uv sync --extra tui
```

## Your first instance

An *instance* is one directory. Everything rssd knows lives under it, so you can
move it, tar it, or delete it with no other state to clean up.

```bash
uv run rssd init demo/
```

That scaffolds six reference feeds chosen to exercise every code path — a
high-churn one, a rich Atom one, a thin RSS one, an image-only one, and one that
sends no cache validators at all.

```bash
uv run rssd daemon --root demo --poll-now
```

`--poll-now` skips the startup stagger, which you want for a demo and don't want
in production.

In two more terminals:

```bash
rss tui --root demo                      # the reader
tail -F demo/var/events.jsonl | jq -c    # the raw event stream
```

## Adding a feed

A subscription is one XML file. Drop it in and the daemon picks it up without a
restart.

```bash
cat > demo/feeds.d/lwn.xml <<'XML'
<subscription>
  <url>https://lwn.net/headlines/rss</url>
  <name>lwn</name>
  <interval>30m</interval>
</subscription>
XML
```

Only `<url>` is required. `<name>` defaults to the filename stem, `<interval>`
to an adaptive default, and `<fulltext>` to `false`.

Deleting the file stops polling but **never deletes what was already
harvested** — the folder is marked retired and left alone. Cleanup is an
explicit `rss`-free command:

```bash
uv run rssd prune --retired --yes
```

## Running offline

Every test fixture is committed, so the whole system runs with no network:

```bash
uv run rssd init demo/ --fixture-mode
uv run rssd serve-fixtures --dir demo/fixtures    # separate terminal
uv run rssd daemon --root demo --poll-now
```

Useful on a plane, and useful when you'd rather a live demo not depend on
somebody else's uptime.

## Where things go

```
demo/
├── feeds.d/     you write these
├── store/       the daemon writes these — only ever feed folders
└── var/         daemon-private state, plus events.jsonl
```

`store/` contains nothing but feed directories, so `ls store/` is always
meaningful. Bookkeeping lives in a sibling directory rather than a dotfile
inside it, because `grep -r`, `find` and `rsync` all traverse dotfiles anyway.
