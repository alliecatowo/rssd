---
layout: home

hero:
  name: rssd
  text: The filesystem is the API
  tagline: A daemon that turns RSS and Atom feeds into a directory tree. No database, no socket, no client library — if it can read a folder, it's a client.
  actions:
    - theme: brand
      text: Get started
      link: /guide/getting-started
    - theme: alt
      text: How it works
      link: /guide/how-it-works
    - theme: alt
      text: GitHub
      link: https://github.com/alliecatowo/rssd

features:
  - title: Reads are sacred
    details: Every file is written to a temp name, fsynced, then renamed into place. A reader sees the old file or the new one — never a mix, never a half-written one. Every document is re-parsed with a strict XML parser before the rename, so malformed output can't reach the tree.
  - title: Nothing is ever overwritten
    details: When a publisher edits a post, the new version lands beside the old one as .r2.xml and a stable symlink is repointed. Follow the symlink for current, or list .rN for the whole history.
  - title: One vocabulary, every feed
    details: Publisher HTML is mapped onto a small fixed set of semantic tags. Wrapper divs, class attributes and tracking junk are gone; relative URLs are resolved. Every entry has the same shape no matter who wrote it.
  - title: The tree is the truth
    details: Each entry embeds its own ID and content hash, so rm -rf var/ is fully recoverable. The daemon rebuilds what it knows by scanning the tree — and writes nothing.
  - title: Events you can tail
    details: Every action appends one JSON object to var/events.jsonl, with a sequence number monotonic across restarts. If you see entry.new, the file already exists and is complete.
  - title: A reader that writes nothing
    details: rss is a separate binary from rssd. It opens no file for writing, anywhere — which is the proof that the output tree really is a sufficient API for someone else's program.
---

## Thirty seconds

```bash
uv run rssd init demo/
uv run rssd daemon --root demo --poll-now
```

```
demo/store/rust-blog/
├── feed.xml
├── status.xml
└── entries/
    ├── 20260907T000000Z-9f2c1a3b-crates-io-update.xml     → newest
    ├── 20260907T000000Z-9f2c1a3b-crates-io-update.r1.xml
    └── 20260907T000000Z-9f2c1a3b-crates-io-update.r2.xml
```

Filenames sort chronologically under a plain `ls`. The `id8` segment maps a file
to an entry ID without opening it. Then read it however you like:

```bash
rss ls rust-blog
rss show 9f2c1a3b
rss link 9f2c1a3b | xargs xdg-open
grep -rl "async" demo/store/*/entries/
```

Or open the vim-flavoured TUI:

```bash
rss tui --root demo
```
