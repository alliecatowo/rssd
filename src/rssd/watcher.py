"""Hot-reload of feeds.d/.

Reconciliation is declarative rather than event-driven: on any change we re-read
the entire directory and diff the desired set against the running set. Watching
filesystems teaches you quickly that events are dropped, duplicated, and
reordered -- a `git checkout` of feeds.d/ fires dozens at once -- and the only
way to stay correct under that is to never trust an individual event to mean
anything except "look again".
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from watchfiles import awatch

from .config import Config
from .events import EventLog
from .models import Subscription
from .scheduler import Scheduler
from .subscriptions import load_dir


@dataclass
class Reconciler:
    config: Config
    scheduler: Scheduler
    events: EventLog
    #: Names whose subscription file has vanished but which are inside the
    #: grace window. vim and emacs save by unlink-then-create; without this a
    #: routine `:w` would retire a feed mid-demo.
    _pending_removal: dict[str, asyncio.TimerHandle] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._pending_removal = {}
        self._reported_errors: dict[str, str] = {}

    def reconcile(self, *, initial: bool = False) -> None:
        subs, errors = load_dir(self.config.feeds_d)

        for path, message in errors:
            key = str(path)
            # Only report a given file's error once, or a permanently broken
            # file would spam the log on every unrelated change.
            if self._reported_errors.get(key) != message:
                self._reported_errors[key] = message
                self.events.emit(
                    "subscription.invalid", None, path=self.config.relative(path),
                    error=message,
                )
        live_error_paths = {str(p) for p, _ in errors}
        for key in list(self._reported_errors):
            if key not in live_error_paths:
                del self._reported_errors[key]

        desired = set(subs)
        running = set(self.scheduler.runners)

        for name in desired:
            handle = self._pending_removal.pop(name, None)
            if handle is not None:
                handle.cancel()  # it came back inside the grace window

        added = sorted(desired - running)
        for name in added:
            self._add(subs[name], announce=not initial)

        for name in sorted(desired & running):
            existing = self.scheduler.runners[name].sub
            if existing != subs[name]:
                self.scheduler.update(subs[name])
                self.events.emit("subscription.changed", name, url=subs[name].url)

        for name in sorted(running - desired):
            self._schedule_removal(name)

        if initial and added:
            self.scheduler.stagger(added)

    def _add(self, sub: Subscription, *, announce: bool) -> None:
        existed = self.config.feed_dir(sub.name).exists()
        self.scheduler.add(sub)
        if announce:
            self.events.emit("subscription.added", sub.name, url=sub.url)
            self.scheduler.poll_soon(sub.name)
        if not existed:
            self.events.emit("feed.created", sub.name)

    def _schedule_removal(self, name: str) -> None:
        if name in self._pending_removal:
            return
        loop = asyncio.get_running_loop()
        self._pending_removal[name] = loop.call_later(
            self.config.limits.unlink_grace, self._confirm_removal, name
        )

    def _confirm_removal(self, name: str) -> None:
        self._pending_removal.pop(name, None)
        if name not in self.scheduler.runners:
            return
        self.events.emit("subscription.removed", name)
        self.scheduler.retire(name)


async def watch(config: Config, reconciler: Reconciler, stop: asyncio.Event) -> None:
    """Coalesce filesystem noise into reconcile() calls."""
    debounce = config.limits.watch_debounce
    try:
        async for _changes in awatch(
            config.feeds_d,
            stop_event=stop,
            debounce=int(debounce * 1000),
            step=50,
            recursive=False,
        ):
            # One more settle beat: awatch's debounce covers the burst, this
            # covers an editor that writes, renames, then chmods.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(_settle(debounce)), debounce * 2)
            reconciler.reconcile()
    except asyncio.CancelledError:
        raise


async def _settle(seconds: float) -> None:
    await asyncio.sleep(seconds)
