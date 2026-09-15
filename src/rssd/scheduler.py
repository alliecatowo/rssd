"""Polling orchestration: one min-heap, one timer, N feeds.

Deliberately *not* a task-per-feed with `await sleep(interval)`. That pattern
drifts, leaks tasks on reload, and makes "poll this feed right now" awkward. A
single heap keyed on due time is easier to reason about and lets a feed be
rescheduled, added, or retired without touching anything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import heapq
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from . import fulltext as fulltext_mod
from .config import Config
from .diff import diff_feed, revisions_today
from .events import EventLog
from .fetch import build_client, fetch_feed
from .identity import entry_base_name, slugify
from .models import (
    EntryRecord,
    FeedState,
    ParsedFeed,
    PreparedEntry,
    RenderContext,
    Subscription,
)
from .parse import parse_feed
from .pipeline import prepare_entries
from .render import render_entry, render_feed_meta, render_status
from .schedule import apply_jitter, backoff_delay, next_interval, stagger_offsets
from .semantic import to_semantic
from .state import load_state, prune_revision_log, save_state
from .store import FeedStore
from .timeutil import filename_stamp, iso, now_utc


@dataclass(order=True)
class _Due:
    at: float
    name: str = field(compare=False)


@dataclass
class FeedRunner:
    """Everything needed to poll one feed, and nothing more."""

    config: Config
    sub: Subscription
    state: FeedState
    store: FeedStore
    events: EventLog
    client: httpx.AsyncClient
    #: Cached scan of what's already on disk. Rebuilt from the tree on start
    #: (invariant I1) and maintained incrementally thereafter.
    seen: dict[str, EntryRecord] = field(default_factory=dict)
    last_feed_xml: bytes | None = None

    @property
    def name(self) -> str:
        return self.sub.name

    @property
    def url(self) -> str:
        return self.state.resolved_url or self.sub.url

    # ── the poll ─────────────────────────────────────────────────────────

    async def poll(self) -> None:
        limits = self.config.limits
        self.state.poll_count += 1
        self.state.last_poll_at = iso(now_utc())
        self.events.emit("feed.poll-started", self.name, url=self.url)

        result = await fetch_feed(
            self.client,
            self.url,
            etag=self.state.etag,
            last_modified=self.state.last_modified,
            limits=limits,
        )

        if result.error_kind == "too-large":
            self.events.emit("feed.too-large", self.name, error=result.error)
            self._fail(result.error or "response too large")
            return

        if result.status in (429, 503) and result.retry_after is not None:
            # Cooperative throttling is not a failure -- the server told us
            # exactly what to do, so do it and don't count a strike.
            delay = min(result.retry_after, limits.backoff_cap)
            self.state.next_poll_at = time.monotonic() + delay
            self.events.emit("feed.throttled", self.name, retry_after=delay)
            save_state(self.config, self.state)
            return

        if result.status == 0 or (result.status >= 400 and not result.ok):
            self._fail(result.error or f"HTTP {result.status}")
            return

        if result.resolved_url and result.resolved_url != self.url:
            # Follow the redirect for polling, but keep the folder bound to the
            # original subscription: renaming a directory out from under readers
            # and their watches is worse than a slightly stale name.
            self.state.resolved_url = result.resolved_url
            self.events.emit("feed.redirected", self.name, to=result.resolved_url)

        self.state.failures = 0

        if result.not_modified:
            self._succeed(result, parsed=None, entries_written=0)
            self.events.emit("feed.unchanged", self.name, reason="304")
            return

        assert result.body is not None
        body_hash = hashlib.sha256(result.body).hexdigest()
        has_validators = bool(result.etag or result.last_modified)

        if not has_validators and body_hash == self.state.body_hash:
            # go.dev sends neither ETag nor Last-Modified. Hashing the body
            # doesn't save bandwidth, but it does save parsing 326KB of XML.
            self._succeed(result, parsed=None, entries_written=0, body_hash=body_hash)
            self.events.emit("feed.unchanged", self.name, reason="body-hash")
            return

        parsed = await asyncio.to_thread(
            parse_feed, result.body, result.content_type, self.url
        )
        if parsed.bozo:
            self.events.emit(
                "feed.poll-succeeded", self.name, bozo=True, note=parsed.bozo_message
            )

        written = await self._apply(parsed)
        self._succeed(result, parsed=parsed, entries_written=written, body_hash=body_hash)

    # ── writing ──────────────────────────────────────────────────────────

    async def _apply(self, parsed: ParsedFeed) -> int:
        limits = self.config.limits
        now = now_utc()

        prepared = prepare_entries(
            parsed.entries,
            feed_name=self.name,
            feed_link=parsed.meta.link,
            first_seen=now,
            limits=limits,
            identity_override=self.state.identity_override,
        )

        if self.sub.fulltext:
            prepared = await self._augment_fulltext(prepared, parsed)

        diff = diff_feed(
            prepared,
            self.seen,
            poll_count=self.state.poll_count,
            limits=limits,
            identity_override=self.state.identity_override,
        )

        if diff.anomaly:
            # A feed that suddenly claims hundreds of new entries is far more
            # likely to be broken than newsworthy. Write nothing.
            self.state.health = "degraded"
            self.events.emit("feed.anomaly", self.name, reason=diff.anomaly)
            return 0

        if diff.downgrade_identity and self.state.identity_override != "content":
            self.state.identity_override = "content"
            self.events.emit("feed.identity-downgraded", self.name)

        ctx = RenderContext(
            feed_name=self.name, feed_url=self.sub.url, feed_title=parsed.meta.title
        )
        today = iso(now)[:10]
        written = 0
        pending: list[tuple[str, str, str, int]] = []  # event, id, path, revision

        for item in diff.new:
            base = entry_base_name(
                filename_stamp(item.published), item.id8,
                slugify(item.parsed.title, limits.slug_max_len),
            )
            data = render_entry(item, ctx, revision=1, first_seen=now, updated=item.parsed.updated)
            path = self.store.write_entry(base_name=base, data=data, revision=1, fsync=False)
            self.seen[item.id] = EntryRecord(
                id=item.id, id8=item.id8, base_name=base, revision=1,
                content_hash=item.content_hash, first_seen=now,
            )
            pending.append(("entry.new", item.id, self.config.relative(path), 1))
            written += 1

        for item, record in diff.revised:
            if revisions_today(self.state.revision_log, item.id, today) >= limits.max_revisions_per_entry_per_day:
                # Keeping every revision is the headline feature; letting a
                # flapping feed write eight hundred of them is not.
                self.state.health = "degraded"
                self.events.emit("entry.revision-suppressed", self.name, id=item.id)
                continue
            revision = record.revision + 1
            data = render_entry(
                item, ctx, revision=revision, first_seen=record.first_seen, updated=now
            )
            path = self.store.write_entry(
                base_name=record.base_name, data=data, revision=revision, fsync=False
            )
            self.seen[item.id] = EntryRecord(
                id=item.id, id8=item.id8, base_name=record.base_name, revision=revision,
                content_hash=item.content_hash, first_seen=record.first_seen,
            )
            self.state.revision_log.setdefault(item.id, []).append(iso(now))
            pending.append(("entry.revised", item.id, self.config.relative(path), revision))
            written += 1

        if written:
            # One directory fsync for the whole batch. Per-file fsync on btrfs
            # is the difference between milliseconds and hundreds of them.
            self.store.fsync_entries()

        feed_bytes = render_feed_meta(parsed.meta, name=self.name, url=self.sub.url)
        if self.store.write_feed_xml(feed_bytes):
            self.events.emit("feed.metadata-changed", self.name)

        prune_revision_log(self.state, today, set(self.seen))

        # Events strictly last: if you see entry.new, the file is already there.
        for event, entry_id, path, revision in pending:
            title = next(
                (p.parsed.title for p in prepared if p.id == entry_id), ""
            )
            self.events.emit(
                event, self.name, id=entry_id, path=path, revision=revision, title=title
            )
        return written

    async def _augment_fulltext(
        self, prepared: list[PreparedEntry], parsed: ParsedFeed
    ) -> list[PreparedEntry]:
        """Fill in bodies the feed left empty. Opt-in only."""
        import dataclasses

        from .identity import content_hash
        from .semantic import canonical_xml

        out: list[PreparedEntry] = []
        for item in prepared:
            if item.parsed.content_origin != "none" or not item.parsed.link:
                out.append(item)
                continue
            try:
                html = await fulltext_mod.extract(
                    self.client, item.parsed.link, limits=self.config.limits
                )
            except fulltext_mod.FulltextError as exc:
                self.events.emit(
                    "fulltext.error", self.name, link=item.parsed.link, error=str(exc)
                )
                out.append(item)
                continue
            if not html:
                out.append(item)
                continue
            content = to_semantic(html, item.parsed.link)
            out.append(
                dataclasses.replace(
                    item,
                    content=content,
                    content_hash=content_hash(
                        item.parsed.title, item.parsed.author, canonical_xml(content)
                    ),
                    parsed=dataclasses.replace(
                        item.parsed, content_origin="fulltext:trafilatura"
                    ),
                )
            )
            self.events.emit("fulltext.ok", self.name, link=item.parsed.link)
        return out

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _succeed(self, result, *, parsed, entries_written: int, body_hash: str | None = None) -> None:
        limits = self.config.limits
        if result.etag:
            self.state.etag = result.etag
        if result.last_modified:
            self.state.last_modified = result.last_modified
        if body_hash:
            self.state.body_hash = body_hash
        self.state.last_error = None
        self._set_health("ok")

        interval = next_interval(
            sub_interval=self.sub.interval,
            max_age=result.max_age,
            ttl_seconds=parsed.meta.ttl_seconds if parsed else None,
            has_validators=bool(result.etag or result.last_modified),
            limits=limits,
        )
        self.state.next_poll_at = time.monotonic() + apply_jitter(interval)
        self.store.write_status_xml(
            render_status(self.state, entry_count=self.store.entry_count())
        )
        save_state(self.config, self.state)
        if parsed is not None:
            self.events.emit(
                "feed.poll-succeeded", self.name,
                entries=len(parsed.entries), written=entries_written,
                next_poll_in=round(interval),
            )

    def _fail(self, message: str) -> None:
        limits = self.config.limits
        self.state.failures += 1
        self.state.last_error = message
        base = self.sub.interval or limits.default_interval
        delay = backoff_delay(self.state.failures, base, limits)
        self.state.next_poll_at = time.monotonic() + delay
        self.events.emit(
            "feed.poll-failed", self.name, error=message,
            failures=self.state.failures, retry_in=round(delay),
        )
        if self.state.failures >= limits.max_failures_before_failing:
            self._set_health("failing")
        else:
            self._set_health("degraded")
        self.store.write_status_xml(
            render_status(self.state, entry_count=self.store.entry_count())
        )
        save_state(self.config, self.state)

    def _set_health(self, health) -> None:
        """Health changes are edge-triggered. A feed that has been down for a
        week should not emit an event every poll about it."""
        if self.state.health != health:
            self.state.health = health
            self.state.health_reported = False
        if not self.state.health_reported:
            self.events.emit("feed.health-changed", self.name, health=health)
            self.state.health_reported = True


class Scheduler:
    def __init__(self, config: Config, events: EventLog) -> None:
        self.config = config
        self.events = events
        self.runners: dict[str, FeedRunner] = {}
        self._heap: list[_Due] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._global = asyncio.Semaphore(config.limits.global_concurrency)
        self._hosts: dict[str, asyncio.Semaphore] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self.client = build_client(config.limits)

    # ── membership ───────────────────────────────────────────────────────

    def add(self, sub: Subscription, *, stagger: float = 0.0) -> FeedRunner:
        store = FeedStore(self.config, sub.name)
        store.ensure_dirs()
        state = load_state(self.config, sub.name)
        runner = FeedRunner(
            config=self.config, sub=sub, state=state, store=store,
            events=self.events, client=self.client,
        )
        # The tree is the truth: rebuild what we know from disk, not from state.
        runner.seen = store.scan()
        if state.health == "retired":
            state.health = "pending"
        self.runners[sub.name] = runner
        self._schedule(sub.name, stagger)
        return runner

    def update(self, sub: Subscription) -> None:
        runner = self.runners.get(sub.name)
        if runner is None:
            self.add(sub)
            return
        if runner.sub.url != sub.url:
            # A changed URL invalidates every cache validator we hold.
            runner.state.etag = None
            runner.state.last_modified = None
            runner.state.body_hash = None
            runner.state.resolved_url = None
            self._schedule(sub.name, 0.0)
        runner.sub = sub

    def retire(self, name: str) -> None:
        runner = self.runners.pop(name, None)
        if runner is None:
            return
        task = self._inflight.pop(name, None)
        if task is not None:
            task.cancel()
        runner.state.health = "retired"
        save_state(self.config, runner.state)
        runner.store.write_status_xml(
            render_status(runner.state, entry_count=runner.store.entry_count())
        )
        # Entries are never touched. Deleting harvested data because a config
        # file vanished is the one thing a file-watching daemon must not do.
        self.events.emit("feed.retired", name)

    def poll_soon(self, name: str) -> None:
        self._schedule(name, 0.0)

    def _schedule(self, name: str, delay: float) -> None:
        heapq.heappush(self._heap, _Due(time.monotonic() + delay, name))
        self._wake.set()

    def stagger(self, names: list[str]) -> None:
        offsets = stagger_offsets(len(names), self.config.limits)
        for name, offset in zip(names, offsets, strict=False):
            self._schedule(name, 0.0 if self.config.poll_now else offset)

    # ── the loop ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        while not self._stopping:
            timeout = self._next_timeout()
            if timeout is None:
                await self._sleep_until_wake(None)
                continue
            if timeout > 0:
                await self._sleep_until_wake(timeout)
                continue
            due = heapq.heappop(self._heap)
            runner = self.runners.get(due.name)
            if runner is None or due.name in self._inflight:
                continue
            task = asyncio.create_task(self._run_one(runner), name=f"poll:{due.name}")
            self._inflight[due.name] = task

    def _next_timeout(self) -> float | None:
        while self._heap and self._heap[0].name not in self.runners:
            heapq.heappop(self._heap)
        if not self._heap:
            return None
        return self._heap[0].at - time.monotonic()

    async def _sleep_until_wake(self, timeout: float | None) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)

    async def _run_one(self, runner: FeedRunner) -> None:
        host = urlsplit(runner.url).hostname or ""
        per_host = self._hosts.setdefault(
            host, asyncio.Semaphore(self.config.limits.per_host_concurrency)
        )
        try:
            async with self._global, per_host:
                await runner.poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bug in one feed must not stop the daemon
            self.events.emit("feed.poll-failed", runner.name, error=f"internal: {exc!r}")
            runner.state.next_poll_at = time.monotonic() + 300
        finally:
            self._inflight.pop(runner.name, None)
            if runner.name in self.runners:
                delay = max(runner.state.next_poll_at - time.monotonic(), 1.0)
                self._schedule(runner.name, delay)

    async def drain(self) -> None:
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in list(self._inflight.values()):
            task.cancel()
        await self.drain()
        await self.client.aclose()
