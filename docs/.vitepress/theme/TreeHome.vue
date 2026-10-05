<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useData, withBase } from 'vitepress'
import heroLs from './captures/hero-ls.txt?raw'
import heroCat from './captures/hero-cat.txt?raw'
import tui from './captures/tui.txt?raw'
import events from './captures/events.txt?raw'
import opml from './captures/opml.txt?raw'
import imp from './captures/import.txt?raw'

const { isDark } = useData()
const menu = ref(false)
const active = ref('feeds')

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

function shell(text) {
  return text
    .replace(/\n+$/, '')
    .split('\n')
    .map((line) => {
      if (line.startsWith('$ ')) {
        return `<span class="p">$</span> <span class="c">${esc(line.slice(2))}</span>`
      }
      let h = esc(line)
      h = h.replace(/^(l[rwx-]{9}\.?)/, '<span class="ln">$1</span>')
      h = h.replace(/^(d[rwx-]{9}\.?)/, '<span class="dr">$1</span>')
      h = h.replace(/(-&gt; )(\S+)$/, '<span class="ar">$1</span><span class="tg">$2</span>')
      h = h.replace(/^(NAME .*)$/, '<span class="hd">$1</span>')
      return h
    })
    .join('\n')
}

function xml(text) {
  return text
    .replace(/\n+$/, '')
    .split('\n')
    .map((line) => {
      if (line.startsWith('$ ')) return shell(line)
      if (line.startsWith('&lt;?') || line.startsWith('<?')) return `<span class="dim">${esc(line)}</span>`
      return esc(line).replace(
        /&lt;(\/?)([\w:-]+)([^&]*?)(\/?)&gt;/g,
        (m, sl, name, attrs, self) =>
          `&lt;${sl}<span class="tag">${name}</span>` +
          attrs.replace(/ ([\w:-]+)="([^"]*)"/g, ' <span class="at">$1</span>=<span class="av">"$2"</span>') +
          `${self}&gt;`,
      )
    })
    .join('\n')
}

function json(text) {
  return shell(text)
    .split('\n')
    .map((l) =>
      l.startsWith('<span class="p">')
        ? l
        : l.replace(/&quot;event&quot;:&quot;([\w.-]+)&quot;/g, '"event":"<span class="tag">$1</span>"')
           .replace(/"event":"([\w.-]+)"/g, '"event":"<span class="tag">$1</span>"'),
    )
    .join('\n')
}

const heroLsHtml = shell(heroLs)
const heroCatHtml = xml(heroCat)
const eventsHtml = json(events)
const opmlHtml = xml(opml)
const impHtml = shell(imp)
const tuiHtml = esc(tui.replace(/\n+$/, ''))
  .replace(/^( ?&gt; ?\S[^│]*)/gm, '$1')
  .replace(/^(.*?)(-- NORMAL --.*)$/m, '$1<span class="st">$2</span>')

const docs = [
  ['getting-started', '/guide/getting-started'],
  ['opml', '/guide/opml'],
  ['reading', '/guide/reading'],
  ['how-it-works', '/guide/how-it-works'],
  ['entries', '/guide/entries'],
  ['events', '/guide/events'],
  ['cli', '/reference/cli'],
  ['config', '/reference/config'],
  ['spec', '/reference/spec'],
]
const dirs = [
  ['feeds', 'feeds', 'the session'],
  ['entries', 'entries', 'the guarantees'],
  ['reader', 'reader', 'rss tui'],
  ['var', 'var', 'events'],
  ['bin', 'bin', 'install'],
]
const topDirs = computed(() => dirs)

function go() { menu.value = false }
function toggleTheme() { isDark.value = !isDark.value }

let io
onMounted(() => {
  if (!('IntersectionObserver' in window)) return
  io = new IntersectionObserver(
    (es) => es.forEach((e) => { if (e.isIntersecting) active.value = e.target.id }),
    { rootMargin: '-20% 0px -70% 0px' },
  )
  dirs.forEach(([id]) => { const el = document.getElementById(id); if (el) io.observe(el) })
})
onBeforeUnmount(() => io && io.disconnect())
</script>

<template>
  <div class="th">
    <header class="bar">
      <a class="logo" :href="withBase('/')" aria-label="rssd home"><span class="pre">~/</span>rssd<span class="t-cursor"></span></a>
      <div class="bar-r">
        <button class="btn" type="button" :aria-expanded="menu" aria-controls="tree-nav" @click="menu = !menu">
          <span class="m-only">{{ menu ? '[x]' : '[tree]' }}</span><span class="d-only">tree</span>
        </button>
        <button class="btn" type="button" @click="toggleTheme" :aria-label="isDark ? 'switch to light theme' : 'switch to dark theme'">
          [{{ isDark ? 'dark' : 'light' }}]
        </button>
      </div>
    </header>

    <div class="grid">
      <aside class="side" :class="{ open: menu }">
        <nav id="tree-nav" aria-label="site tree">
          <div class="tr-root"><span class="dim">$</span> tree -L 2 ~/rssd</div>
          <ul class="tr">
            <li v-for="([id, name, note], i) in topDirs" :key="id">
              <span class="br">├── </span><a :href="'#' + id" :class="{ on: active === id }" @click="go">{{ name }}/</a><span class="note"> {{ note }}</span>
            </li>
            <li>
              <span class="br">└── </span>
              <details class="docs-dir" open>
                <summary><a :href="withBase('/guide/getting-started')">docs/</a></summary>
                <ul class="tr sub">
                  <li v-for="([n, to], i) in docs" :key="n">
                    <span class="br">{{ i === docs.length - 1 ? '└── ' : '├── ' }}</span><a :href="withBase(to)">{{ n }}</a>
                  </li>
                </ul>
              </details>
            </li>
          </ul>
          <div class="tr-sum dim">{{ dirs.length + 1 }} directories, {{ docs.length }} docs</div>
          <ul class="ext">
            <li><a href="https://github.com/alliecatowo/rssd">github</a></li>
            <li><a href="https://pypi.org/project/rssd-fs/">pypi</a></li>
          </ul>
        </nav>
      </aside>

      <main class="main">
        <details class="dir" id="feeds" open>
          <summary><span class="mk"></span>feeds/ <span class="meta">drwxr-xr-x &nbsp;the session</span></summary>
          <div class="hero">
            <p class="eyebrow">rssd 0.3.0 &middot; a daemon that writes a directory</p>
            <h1>The filesystem<br />is the <span class="hl">API</span>.</h1>
            <p class="lede">
              rssd turns RSS and Atom feeds into a directory tree of plain XML files.
              No database, no socket, no client library. If it can read a folder, it is a client.
            </p>
            <div class="install">
              <div><span class="p">$</span> uv tool install 'rssd-fs[tui]'</div>
              <div><span class="p">$</span> brew install alliecatowo/tap/rssd</div>
            </div>
            <p class="cta">
              <a :href="withBase('/guide/getting-started')">[ get started ]</a>
              <a :href="withBase('/guide/how-it-works')">[ how it works ]</a>
              <a href="https://github.com/alliecatowo/rssd">[ github ]</a>
            </p>
          </div>

          <figure class="term">
            <figcaption><span>session</span><span class="dim">rssd 0.3.0 &middot; real output</span></figcaption>
            <pre class="body" tabindex="0" v-html="heroLsHtml"></pre>
            <pre class="body" tabindex="0" v-html="heroCatHtml"></pre>
          </figure>
          <p class="cap">
            Captured on 2026-10-04: <code>rssd add</code> for blog.rust-lang.org, xkcd.com and lwn.net, one
            <code>rssd once</code>, then these commands. Every entry is a symlink to its newest revision.
            Only the line widths were trimmed, and the pipes that do it are in the commands.
          </p>
        </details>

        <details class="dir" id="entries" open>
          <summary><span class="mk"></span>entries/ <span class="meta">drwxr-xr-x &nbsp;the guarantees</span></summary>
          <ul class="files">
            <li>
              <details open>
                <summary><span class="perm">-r--r--r--</span> atomic-writes.txt</summary>
                <p>Every file is written to a temp name, fsynced, then renamed into place. A reader sees the old file or the new one, never a half-written one. Each document is re-parsed with a strict XML parser before the rename, so malformed output cannot reach the tree.</p>
              </details>
            </li>
            <li>
              <details open>
                <summary><span class="perm">lrwxrwxrwx</span> revisions.txt <span class="ar">-&gt;</span> <span class="tg">*.r2.xml</span></summary>
                <p>Nothing is overwritten. When a publisher edits a post, the new version lands beside the old one as <code>.r2.xml</code> and a stable symlink is repointed. Follow the symlink for current, or list <code>.rN</code> for the whole history.</p>
              </details>
            </li>
            <li>
              <details open>
                <summary><span class="perm">-r--r--r--</span> vocabulary.xml</summary>
                <p>Publisher HTML is mapped onto a small fixed set of tags: <code>paragraph</code>, <code>heading</code>, <code>code</code>, <code>link</code>. Wrapper divs, class attributes and tracking junk are gone, and relative URLs are resolved. Every entry has the same shape, whoever wrote it.</p>
              </details>
            </li>
            <li>
              <details open>
                <summary><span class="perm">-r--r--r--</span> tree-is-truth.txt</summary>
                <p>Each entry embeds its own ID and content hash, so <code>rm -rf var/</code> is fully recoverable. The daemon rebuilds what it knows by scanning the tree, and writes nothing.</p>
              </details>
            </li>
            <li>
              <details open>
                <summary><span class="perm">-r--r--r--</span> rss.readonly</summary>
                <p><code>rss</code> is a separate binary from <code>rssd</code>. It opens no file for writing, anywhere, which is the proof that the output tree is a sufficient API for someone else's program.</p>
              </details>
            </li>
          </ul>
        </details>

        <details class="dir" id="reader" open>
          <summary><span class="mk"></span>reader/ <span class="meta">drwxr-xr-x &nbsp;rss tui</span></summary>
          <p class="pro">A vim-flavoured terminal reader with three panes: feeds, entries, and the article. It reads the filesystem and nothing else, so the daemon does not need to be running.</p>
          <figure class="term">
            <figcaption><span>rss tui</span><span class="dim">100x26, captured from tmux</span></figcaption>
            <pre class="body tui" tabindex="0" v-html="tuiHtml"></pre>
          </figure>
          <p class="cap">
            <code>j</code>/<code>k</code> move, <code>h</code>/<code>l</code> change pane, <code>Enter</code> reads, <code>/</code> searches,
            <code>:links</code> numbers every outbound URL. <a :href="withBase('/guide/reading')">Full key list</a>.
          </p>
        </details>

        <details class="dir" id="var" open>
          <summary><span class="mk"></span>var/ <span class="meta">drwxr-xr-x &nbsp;events.jsonl</span></summary>
          <p class="pro">Every action appends one JSON line. If you see <code>entry.new</code>, the file already exists and is complete.</p>
          <figure class="term">
            <figcaption><span>events</span><span class="dim">the tail of the same run</span></figcaption>
            <pre class="body" tabindex="0" v-html="eventsHtml"></pre>
          </figure>
        </details>

        <details class="dir" id="bin" open>
          <summary><span class="mk"></span>bin/ <span class="meta">drwxr-xr-x &nbsp;install</span></summary>
          <p class="pro">Python 3.12 or newer. Pick one:</p>
          <figure class="term">
            <figcaption><span>install</span><span class="dim">pypi &middot; homebrew</span></figcaption>
            <pre class="body" tabindex="0"><span class="p">$</span> <span class="c">uv tool install 'rssd-fs[tui]'</span>
<span class="p">$</span> <span class="c">brew install alliecatowo/tap/rssd</span>

<span class="p">$</span> <span class="c">rssd add https://blog.rust-lang.org/feed.xml --name rust-blog</span>
<span class="p">$</span> <span class="c">rssd once</span>
<span class="p">$</span> <span class="c">rss tui</span></pre>
          </figure>
          <p class="pro">Bring your subscriptions from any other reader with OPML, and take them back out the same way:</p>
          <figure class="term">
            <figcaption><span>opml</span><span class="dim">real output</span></figcaption>
            <pre class="body" tabindex="0" v-html="impHtml"></pre>
            <pre class="body" tabindex="0" v-html="opmlHtml"></pre>
          </figure>
          <p class="cap"><a :href="withBase('/guide/opml')">OPML in detail</a> &middot; <a :href="withBase('/guide/getting-started')">getting started</a> &middot; <a :href="withBase('/reference/cli')">every command</a></p>
        </details>

        <footer class="foot">
          <div><span class="p">$</span> <span class="c">cat LICENSE | head -n 1</span></div>
          <div class="dim">MIT License &middot; &copy; 2026 Allie Coleman &middot; <a href="https://github.com/alliecatowo/rssd">source</a></div>
          <div><span class="p">$</span> <span class="t-cursor"></span></div>
        </footer>
      </main>
    </div>
  </div>
</template>

<style>
.th {
  min-height: 100vh;
  background: var(--t-paper);
  color: var(--t-ink);
  font-family: var(--vp-font-family-base);
  font-size: 14px;
  line-height: 1.7;
}
.th a { color: var(--t-link); text-decoration: underline dashed; text-underline-offset: 3px; }
.th a:hover { text-decoration-style: solid; }
.th code { background: var(--vp-code-bg); color: var(--t-accent-2); padding: 0.05em 0.35em; font-size: 0.92em; }
.th .dim { color: var(--t-dim); }
.th .p { color: var(--t-accent); text-shadow: var(--t-glow); }
.th .c { color: var(--t-ink); font-weight: 500; }

/* bar */
.bar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px; height: 56px;
  background: var(--t-paper);
  border-bottom: 1px dashed var(--t-line);
}
.logo {
  font-family: var(--t-display); font-size: 36px; line-height: 1;
  color: var(--t-accent) !important; text-decoration: none !important; text-shadow: var(--t-glow);
}
.logo .pre { color: var(--t-dim); text-shadow: none; }
.t-cursor {
  display: inline-block; width: 0.55em; height: 0.85em; margin-left: 0.2em;
  background: var(--t-accent); vertical-align: -0.05em;
  animation: t-blink 1.1s steps(1) infinite;
}
.bar-r { display: flex; gap: 6px; }
.btn {
  font: inherit; font-size: 13px; color: var(--t-accent); background: transparent;
  border: 1px solid var(--t-line); padding: 4px 10px; cursor: pointer; min-height: 36px;
}
.btn:hover, .btn:focus-visible { border-color: var(--t-accent); }
.d-only { display: none; }

/* grid */
.grid { display: block; max-width: 1240px; margin: 0 auto; }
.side { display: none; padding: 16px; border-bottom: 1px dashed var(--t-line); background: var(--t-paper); }
.side.open { display: block; position: sticky; top: 56px; z-index: 15; max-height: calc(100vh - 56px); overflow-y: auto; }
.tr-root { margin-bottom: 4px; }
.tr, .ext { list-style: none; margin: 0; padding: 0; }
.tr li { white-space: pre; }
.tr .br { color: var(--t-dim); }
.tr .note { color: var(--t-dim); font-size: 12px; padding-left: 1.5ch; }
.tr a { padding: 3px 0; display: inline-block; }
.tr a.on { color: var(--t-ink); font-weight: 700; text-decoration-style: solid; }
.tr a.on::before { content: '> '; color: var(--t-accent); }
.docs-dir { display: inline-block; vertical-align: top; }
.docs-dir > summary { list-style: none; cursor: pointer; display: inline; }
.docs-dir > summary::-webkit-details-marker { display: none; }
.tr.sub { margin-top: 0; }
.tr-sum { margin: 8px 0 12px; }
.ext { display: flex; gap: 16px; border-top: 1px dashed var(--t-line); padding-top: 10px; }

/* main */
.main { padding: 8px 16px 48px; min-width: 0; }
.dir { margin: 18px 0 0; border-top: 1px dashed var(--t-line); padding-top: 14px; scroll-margin-top: 70px; }
.dir > summary {
  list-style: none; cursor: pointer; font-weight: 700; font-size: 17px; color: var(--t-accent);
  text-shadow: var(--t-glow); display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 10px;
}
.dir > summary::-webkit-details-marker, .files summary::-webkit-details-marker { display: none; }
.dir > summary .mk::before { content: '[-]'; color: var(--t-dim); text-shadow: none; margin-right: 10px; font-weight: 400; }
.dir:not([open]) > summary .mk::before { content: '[+]'; }
.meta { color: var(--t-dim); font-size: 12px; font-weight: 400; text-shadow: none; }
.pro { margin: 12px 0 8px; color: var(--t-ink-2); max-width: 70ch; }
.cap { color: var(--t-dim); font-size: 12.5px; max-width: 80ch; margin: 8px 0 0; }

/* hero */
.hero { padding: 18px 0 8px; }
.eyebrow { color: var(--t-dim); margin: 0 0 12px; font-size: 12.5px; }
.hero h1 {
  font-size: clamp(30px, 8.4vw, 56px); line-height: 1.08; font-weight: 700; margin: 0 0 18px;
  letter-spacing: -0.02em; color: var(--t-ink); border: 0;
}
.hero h1 .hl { color: var(--t-accent); text-shadow: var(--t-glow); }
.lede { font-size: 15px; max-width: 62ch; margin: 0 0 18px; color: var(--t-ink-2); }
.install { border-left: 2px solid var(--t-accent); padding: 4px 0 4px 12px; margin: 0 0 14px; overflow-x: auto; white-space: nowrap; }
.cta { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 22px; }
.cta a { text-decoration: none; font-weight: 700; padding: 4px 0; }
.cta a:hover { text-decoration: underline; }

/* terminal */
.term { margin: 14px 0 0; border: 1px solid var(--t-line); background: var(--t-panel); }
.term figcaption {
  display: flex; justify-content: space-between; gap: 12px; padding: 6px 12px;
  border-bottom: 1px solid var(--t-line); font-size: 12px; color: var(--t-accent);
}
:root:not(.dark) .term { background: var(--t-panel); }
.term .body {
  margin: 0; padding: 12px; overflow-x: auto; font: inherit; font-size: 12.5px; line-height: 1.6;
  color: var(--t-ink); white-space: pre; tab-size: 4;
}
.term .body + .body { border-top: 1px dashed var(--t-line); }
:root:not(.dark) .term .body {
  background-image: linear-gradient(transparent 50%, var(--t-band) 50%);
  background-size: 100% calc(2 * 12.5px * 1.6);
  background-position: 0 12px;
}
.term .ln, .term .dr { color: var(--t-accent-2); }
.term .ar { color: var(--t-accent-2); }
.term .tg { color: var(--t-accent); }
.term .hd { color: var(--t-accent); font-weight: 700; }
.term .tag { color: var(--t-accent); }
.term .at { color: var(--t-accent-2); }
.term .av { color: var(--t-ink); }
.term .st { background: var(--t-accent); color: var(--t-paper); font-weight: 700; }
.term .tui { line-height: 1.35; }

/* files */
.files { list-style: none; padding: 0; margin: 10px 0 0; }
.files > li { border-bottom: 1px dashed var(--t-line); }
.files details > summary { list-style: none; cursor: pointer; padding: 8px 0; font-weight: 500; color: var(--t-ink); display: flex; flex-wrap: wrap; gap: 0 12px; }
.files details > summary::before { content: '[-]'; color: var(--t-dim); font-weight: 400; }
.files details:not([open]) > summary::before { content: '[+]'; }
.files details > summary .perm { color: var(--t-dim); font-weight: 400; }
.files details > summary .ar { color: var(--t-accent-2); margin-right: -6px; }
.files p { margin: 0 0 14px; padding-left: 12px; border-left: 2px solid var(--t-line); color: var(--t-ink-2); max-width: 72ch; }

/* footer */
.foot { margin-top: 40px; padding-top: 14px; border-top: 1px dashed var(--t-line); font-size: 13px; }

@media (max-width: 389px) {
  .th .term .body { font-size: 12px; }
}

@media (min-width: 900px) {
  .bar { display: none; }
  .grid { display: grid; grid-template-columns: 300px minmax(0, 1fr); }
  .side, .side.open {
    display: block; position: sticky; top: 0; height: 100vh; overflow-y: auto; max-height: none;
    border-bottom: 0; border-right: 1px dashed var(--t-line); padding: 24px 20px;
  }
  .side::before {
    content: 'rssd'; display: block; font-family: var(--t-display); font-size: 64px; line-height: 0.9;
    color: var(--t-accent); text-shadow: var(--t-glow); margin-bottom: 22px;
  }
  .main { padding: 24px 40px 80px; }
  .install { white-space: normal; }
}
</style>
