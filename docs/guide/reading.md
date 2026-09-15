# Reading feeds

`rss` is a separate binary from `rssd` and **writes nothing, anywhere** — no
cache, no state file, no read/unread tracking. It is a pure window onto the
tree the daemon produced.

That constraint is deliberate. It's what demonstrates the output tree is a
genuine API rather than an implementation detail, and it means you can run `rss`
against a store on a read-only mount, a snapshot, or someone else's backup.

## From the shell

```bash
rss feeds                    # what's subscribed, and is it healthy
rss ls rust-blog             # entries, newest first
rss show rust-blog:1         # read the newest one
rss link rust-blog:1         # just the URL
rss open rust-blog:1         # open it in a browser
rss search "borrow checker"  # across every feed
```

Because the store is just files, the tools you already have work too:

```bash
grep -rl "async" demo/store/*/entries/*.xml
ls -t demo/store/lobsters/entries/ | head
find demo/store -name '*.r2.xml'      # everything that was ever edited
```

## The TUI

```bash
rss tui --root demo
```

Modal, like vim. No header bar, no borders, no mouse required. Three panes —
feeds, entries, and a reading pane — with a status line at the bottom.

### Normal mode

| Key | |
|---|---|
| `h` `j` `k` `l` | `j`/`k` move, `h`/`l` change pane |
| `gg` `G` | first / last |
| `ctrl-d` `ctrl-u` | half page |
| `ctrl-f` `ctrl-b` | full page |
| `Tab` | cycle pane focus |
| `Enter` | open the entry in the reading pane |
| `J` `K` | next / previous entry without leaving the reader |
| `o` | open the source URL in a browser |
| `y` | yank the source URL to the clipboard |
| `/` `?` | search forward / backward |
| `n` `N` | next / previous match |
| `p` | toggle the reading pane |
| `r` | refresh from disk |
| `zz` | centre the cursor line |
| `:` | command mode |
| `q` | quit |

### Ex commands

```vim
:q  :qa            quit
:e rust-blog       switch feed
:feeds             focus the feed pane
:set               list every option and its value
:set nopreview     hide the reading pane
:set width=100     set an option
:sort oldest       newest | oldest | title | feed
:links             every link in the current entry, numbered
:open 3            open link [3] from that list
:N                 jump to entry N
:help              key reference
```

`:set` changes the running session only — exactly like vim. To persist
preferences, write them in `rss.toml`; see
[Configuration](/reference/config#reader-preferences-for-rss).

### Reading an entry

`Enter` renders the article in the reading pane with numbered link references,
so you can see every outbound URL without leaving the terminal. `:links` prints
the same numbering, and `:open 3` opens link 3 — which is why the two agree.

The daemon does not need to be running. The TUI reads the filesystem on a timer
and on `r`, so it works perfectly against a static tree, a snapshot, or a
directory you rsynced from somewhere else.
