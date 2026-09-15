# Entry format

Every entry is one XML file. The shape is identical regardless of whether the
feed was RSS 2.0, Atom, or RDF, and regardless of how the publisher writes
their markup.

```xml
<?xml version="1.0" encoding="utf-8"?>
<entry xmlns="https://rssd.dev/entry/1"
       id="sha256:9f2c1a3b…" revision="2"
       first-seen="2026-09-15T21:14:02Z" updated="2026-09-15T23:01:44Z">
  <source>
    <link>https://blog.rust-lang.org/2026/09/07/rust-debugging-survey/</link>
    <feed name="rust-blog" title="Rust Blog">https://blog.rust-lang.org/feed.xml</feed>
    <guid basis="atom-id">https://blog.rust-lang.org/2026/09/07/…</guid>
  </source>
  <title>Rust debugging survey 2026 results</title>
  <author>The Rust Team</author>
  <published origin="feed">2026-09-07T00:00:00Z</published>
  <categories><category>rust</category></categories>
  <content origin="feed:content" hash="sha256:bc167659…">
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

## Telling you where things came from

Three attributes exist so a consumer never has to guess:

| Attribute | Values | Means |
|---|---|---|
| `content/@origin` | `feed:content`, `feed:summary`, `fulltext:trafilatura`, `none` | where the body came from |
| `published/@origin` | `feed`, `first-seen`, `clamped` | whether the date is real |
| `source/guid/@basis` | `atom-id`, `guid`, `link`, `content` | what the ID was derived from |

`origin="none"` means the feed shipped a title and a link and nothing else.
rssd **never fabricates a body** — it says so instead.

`origin="clamped"` means the entry claimed a date far in the future, a common
feed bug. Left alone, one such entry pins itself to the top of every `ls`
forever, so it's clamped and labelled rather than trusted or dropped.

## Filenames

```
20260907T000000Z-9f2c1a3b-rust-debugging-survey.r2.xml
└── published ──┘ └─ id8 ─┘ └────── slug ──────┘ └rev┘
```

- Lexicographic sort equals chronological sort. Non-negotiable.
- No colons — legal on Linux, hostile to shell completion and `scp`.
- `id8` maps a file to an entry ID without opening it.
- The name is keyed on `published`, never `updated`, so **it never changes after
  first write**.

## The semantic vocabulary

Block elements: `paragraph`, `heading[level]`, `list[ordered]` / `item`,
`quote`, `code[language]`, `image[src,alt]`, `separator`.

Inline: `link[href]`, `emphasis`, `strong`, `code`, `break`.

The mapping is an allowlist, which means it *is* the sanitiser — there is no
separate bleach pass:

| HTML | becomes |
|---|---|
| `p` | `paragraph` |
| `h1`…`h6` | `heading level="1".."6"` |
| `ul` / `ol` | `list` / `list ordered="true"` |
| `blockquote` | `quote` |
| `pre`, `code` | `code` |
| `div`, `span`, `section`, `article` | **unwrapped** — children hoisted |
| `script`, `style`, `iframe`, `form`, … | **dropped with their subtree** |
| anything else | unwrapped to its text |

`class`, `id`, `style` and `data-*` are dropped everywhere. They're publisher
CSS hooks, meaningless without the publisher's stylesheet.

::: tip Why drop the subtree for `script`?
Unwrapping a `<script>` instead of deleting it emits the script body as visible
text. It's the classic sanitiser footgun, and it's why the distinction between
"unwrap" and "drop" is spelled out rather than left to intuition.
:::

## What still needs explicit guarding

An allowlist handles tags. It doesn't handle everything:

- **URL schemes.** Only `http`, `https` and `mailto` survive. `javascript:` and
  `data:` are dropped and the element unwraps to text — `data:` dies even for
  images, since a 200KB base64 blob is hostile to `grep`.
- **Relative URLs** resolve against Atom's `xml:base`, then the entry link, then
  the feed link.
- **XML-illegal codepoints.** C0 controls *and lone surrogates* are stripped, or
  strict parsers reject the output.
- **Named HTML entities.** `&nbsp;` and `&mdash;` are undefined in XML.
  Serialising from a parsed tree — where they're already characters — and
  re-escaping only the five XML entities makes that bug class structurally
  impossible.
- **Charset.** Decoded as XML declaration → HTTP `Content-Type` → UTF-8. Never
  UTF-8 unconditionally; that's how you get mojibake.

## Revisions

Entry files are never mutated. A changed entry gets a new revision beside the
old one, and the symlink is repointed:

```
20260907T000000Z-9f2c1a3b-crates-io-update.xml     → symlink to .r2
20260907T000000Z-9f2c1a3b-crates-io-update.r1.xml
20260907T000000Z-9f2c1a3b-crates-io-update.r2.xml
```

Follow the symlink for "current"; enumerate `.rN` for history. Revision numbers
are recovered on restart by scanning the directory, because the tree is the
truth.

```bash
rss revisions 9f2c1a3b
rss diff 9f2c1a3b        # what actually changed, in prose
```
