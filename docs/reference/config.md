# Configuration

There are two separate things to configure, and they belong to different
programs.

## Subscriptions — for `rssd`

One XML file per feed in `feeds.d/`. Only `<url>` is required.

```xml
<subscription>
  <url>https://blog.rust-lang.org/feed.xml</url>
  <name>rust-blog</name>
  <interval>15m</interval>
  <fulltext>false</fulltext>
</subscription>
```

| Element | Default | Notes |
|---|---|---|
| `url` | — | required |
| `name` | the filename stem | must match `^[a-z0-9][a-z0-9._-]{0,63}$` |
| `interval` | adaptive | `30s`, `15m`, `2h`, `1d`, or bare seconds. Clamped to 60s–6h |
| `fulltext` | `false` | fetch the article page for entries with no body |

The URL extractor is deliberately liberal, because people hand-edit these files.
It tries `/subscription/url`, then `/subscription/link`, then `//outline/@xmlUrl`
(so an OPML fragment works), then `//link[@rel='self']/@href`, then the first
`http(s)://` it can find anywhere in the document.

A file that fails to parse produces a `subscription.invalid` event and **the
previous good version stays loaded**. A fat-fingered save never takes a working
feed offline.

### Full-text extraction

Off by default, and worth keeping that way: the normal footprint is one request
per feed, but a feed with `<fulltext>true</fulltext>` costs one extra request
per *entry*, to a different third-party site each time.

It needs the extra:

```bash
uv sync --extra fulltext
```

Good candidates are feeds like Hacker News, whose body is link metadata rather
than prose.

## Reader preferences — for `rss`

`rss` **never writes files**, so there is no `rss config set`. Preferences are
read from a TOML file you edit yourself, and `:set` in the TUI changes the
running session only — exactly like `:set` in vim.

Search order, first hit wins:

```
$RSS_CONFIG
./rss.toml
<root>/rss.toml
${XDG_CONFIG_HOME:-~/.config}/rss/config.toml
```

```toml
# ~/.config/rss/config.toml
width = 88              # 0 = fit the terminal
sort = "newest"         # newest | oldest | title | feed
marks = true            # **bold** and `code` markers in rendered text
links = true            # numbered [1] link references, like a text browser
date_format = "relative"# relative | iso | short
browser = ""            # empty = $BROWSER, then xdg-open
preview = true          # show the reading pane
scrolloff = 3           # lines of context kept around the cursor
number = false
relativenumber = false
tildes = true           # ~ filler rows below the last entry
```

An unreadable or malformed config is skipped rather than fatal. Being unable to
read your feeds because of a typo in a preferences file is a bad trade.

### `:set` in the TUI

All the vim forms work:

```vim
:set preview        " on
:set nopreview      " off
:set preview!       " toggle
:set width=100      " assign
:set                " list every option and its value
```

An unknown option or a bad value shows an error on the status line. It never
silently does nothing — a setting that quietly fails is worse than one that
complains.

## Instance layout

Every path derives from one root, so an instance is a single directory you can
move, tar, or delete.

```
<root>/
├── feeds.d/                 subscriptions (you)
├── store/                   output — only ever feed folders
│   └── <feed>/
│       ├── feed.xml         stable metadata
│       ├── status.xml       volatile: health, last poll, failures
│       └── entries/
└── var/                     daemon-private
    ├── events.jsonl
    ├── seq
    ├── rssd.lock            exclusive flock; one daemon per root
    └── state/<feed>.json    etag, last-modified, body hash, backoff
```

`feed.xml` is split from `status.xml` on purpose: a consumer watching `feed.xml`
for real metadata changes shouldn't be woken every sixty seconds by a poll
timestamp.
