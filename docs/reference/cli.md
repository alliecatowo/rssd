# Commands

Two binaries, on purpose.

| | |
|---|---|
| **`rssd`** | the daemon. Writes the tree. |
| **`rss`** | the reader. Opens no file for writing, ever. |

That split isn't cosmetic. `rss` is the proof that the output tree is a
sufficient API for a program that shares no code path with the daemon — it
discovers everything by listing directories and parsing XML, exactly as your
own script would.

## `rssd` — the daemon

```bash
rssd init <root> [--fixture-mode] [--force]
rssd daemon --root <root> [--poll-now] [--no-fsync]
rssd once --root <root>
rssd poll <feed> --root <root>
rssd validate --root <root>
rssd prune --retired [--yes]
rssd serve-fixtures [--port 8765] [--dir DIR]
rssd demo-mutate --root <root>
```

| Command | |
|---|---|
| `init` | scaffold an instance with the six reference feeds |
| `daemon` | run continuously, watching `feeds.d/` for changes |
| `once` | one poll pass over every feed, then exit — good for cron |
| `poll` | force-poll a single feed now |
| `validate` | check every subscription file parses |
| `prune` | delete retired feed folders. Never automatic; `--yes` to really do it |
| `serve-fixtures` | local HTTP server over the committed fixtures |
| `demo-mutate` | edit a served fixture to force a revision on cue |

`--poll-now` collapses the startup stagger. Without it, feeds are spread over
up to 30 seconds so a restart doesn't hammer seven servers at once.

Only one daemon may run per root; a second refuses via an exclusive `flock`.

## `rss` — the reader

```bash
rss feeds
rss ls [feed] [-n N] [--long]
rss show <ref>
rss cat <ref>
rss link <ref>
rss links <ref>
rss open <ref> [--browser CMD]
rss search <query> [feed] [-n N]
rss revisions <ref>
rss diff <ref> [--rev A:B] [--xml]
rss info <ref>
rss config
rss tui
```

Global flags work before **or** after the subcommand: `--root`, `--json`,
`--no-color`, `--width`.

### Referring to an entry

Anywhere a `<ref>` is accepted, four forms work:

```bash
rss show 9f2c1a3b                 # id8 prefix (any unique prefix ≥ 2 chars)
rss show rust-blog:3              # 3rd newest in a feed
rss show rust-blog                # newest in a feed
rss show demo/store/rust-blog/entries/2026….xml   # a path
```

An ambiguous prefix is an error listing the candidates, never a silent guess.

### Scripting

Everything is pipe-friendly. Colour appears only on a TTY, and `NO_COLOR` is
honoured.

```bash
# open the newest Rust post
rss link rust-blog | xargs xdg-open

# every title from the last day, as JSON
rss ls --json -n 0 | jq -r '.[] | select(.published > "2026-09-15") | .title'

# full-text grep across everything the daemon has ever stored
rss search "async" --json | jq -r '.[] | "\(.feed)  \(.title)"'

# what changed when a post was edited
rss diff 9f2c1a3b
```

`rss ls` defaults to 50 entries; `-n 0` means unlimited. `--long` loads each
entry for author and summary, which costs a file read per row — fine for tens,
avoid for thousands.

### `rss show`

The one you'll use most. Renders the semantic content as readable text, wrapped
to `--width`, then the config's `width`, then your terminal.

```
Announcing Rust 1.98.1
rust-blog  ·  12d ago  ·  The Rust Release Team
https://blog.rust-lang.org/2026/09/03/Rust-1.98.1/

The Rust team has published a new point release of Rust, 1.98.1. Rust is a
programming language that is empowering everyone to build reliable and
efficient software.

    rustup update stable

If you don't have it already, you can get `rustup`[1] from the appropriate
page on our website.

  [1] https://rust-lang.github.io/rustup/
```

Links become numbered references with a list at the end, the way a text browser
does, so URLs stay visible without cluttering the prose. `:links` and `:open N`
in the TUI use the same numbering.

When a feed supplied no body, `rss show` says so and prints the link rather
than showing you an empty page.
