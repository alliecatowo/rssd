# OPML import and export

OPML is the file every other feed reader exports. rssd reads and writes it so you
can move in and out without retyping a URL.

## Import

```bash
rssd import subscriptions.opml
```

```
  added hnrss.org  https://hnrss.org/frontpage
  added blog.rust-lang.org  https://blog.rust-lang.org/feed.xml

2 added, 0 skipped (already subscribed or invalid)
```

Every `<outline>` that carries an `xmlUrl` becomes one subscription file in
`feeds.d/`, so import is nothing more than writing the files you could have
written by hand. Things worth knowing:

- Folder structure is flattened. Outlines without an `xmlUrl` (the category
  wrappers) are ignored.
- The feed name is derived from the host (`hnrss.org`). Rename it later by editing
  the `<name>` element in the file, or add the feed yourself with
  `rssd add <url> --name N` first.
- A feed you are already subscribed to is skipped, and a second feed that would
  collide on a name gets a numeric suffix.
- A malformed OPML file is an error and changes nothing. DTDs and external
  entities are never resolved.
- The daemon picks the new files up without a restart; run `rssd once` to fetch
  them right away.

## Export

```bash
rssd export                      # OPML 2.0 on stdout
rssd export -o subscriptions.opml
```

```xml
<?xml version='1.0' encoding='utf-8'?>
<opml version="2.0">
  <head>
    <title>rssd subscriptions</title>
  </head>
  <body>
    <outline type="rss" text="lwn" title="lwn" xmlUrl="https://lwn.net/headlines/rss"/>
    <outline type="rss" text="rust-blog" title="rust-blog" xmlUrl="https://blog.rust-lang.org/feed.xml"/>
    <outline type="rss" text="xkcd" title="xkcd" xmlUrl="https://xkcd.com/atom.xml"/>
  </body>
</opml>
```

Subscriptions are sorted by name. Both commands take `--root DIR` like the rest of
the CLI. Export reads `feeds.d/`, so it works whether or not the daemon is running.

The subscription file format itself also accepts an OPML fragment: the URL
extractor falls back to `//outline/@xmlUrl`. See
[Configuration](/reference/config#subscriptions-for-rssd).
