"""Process lifecycle: the lock, the signals, and the recovery pass."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
from pathlib import Path

from .config import Config
from .events import EventLog
from .scheduler import Scheduler
from .store import sweep_temps
from .watcher import Reconciler, watch


class LockedError(RuntimeError):
    pass


class RootLock:
    """One daemon per root.

    Two daemons sharing a tree would interleave revisions and fight over state
    files, and the damage would be silent. An exclusive flock is cheap enough
    that there is no reason not to.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise LockedError(
                f"another rssd daemon holds {self.path}; refusing to start"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            self.path.unlink()

    def __enter__(self) -> RootLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


async def run_daemon(config: Config) -> int:
    config.ensure_dirs()
    lock = RootLock(config.lock_path)
    try:
        lock.acquire()
    except LockedError as exc:
        print(f"rssd: {exc}")
        return 1

    events = EventLog(config)
    events.open()
    stop = asyncio.Event()

    try:
        # A SIGKILL mid-write can only ever leave temp files behind; a real
        # entry path is never partial. Clear them before anything reads the tree.
        swept = sweep_temps(config.store)
        events.emit("daemon.started", None, pid=os.getpid(), root=str(config.root),
                    swept_temps=swept)

        scheduler = Scheduler(config, events)
        reconciler = Reconciler(config=config, scheduler=scheduler, events=events)
        reconciler.reconcile(initial=True)

        tracked = sum(len(r.seen) for r in scheduler.runners.values())
        events.emit(
            "daemon.reconciled", None,
            feeds=len(scheduler.runners), entries=tracked,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        tasks = [
            asyncio.create_task(scheduler.run(), name="scheduler"),
            asyncio.create_task(watch(config, reconciler, stop), name="watcher"),
            asyncio.create_task(stop.wait(), name="stop"),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        events.emit("daemon.stopping", None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await scheduler.stop()
        return 0
    finally:
        events.close()
        lock.release()


async def run_once(config: Config) -> int:
    """One poll pass over every subscription, then exit.

    Useful in cron-shaped deployments, and it is what the integration tests
    drive so they never have to reason about timers.
    """
    config.ensure_dirs()
    events = EventLog(config)
    events.open()
    try:
        sweep_temps(config.store)
        events.emit("daemon.started", None, pid=os.getpid(), mode="once")
        scheduler = Scheduler(config, events)
        reconciler = Reconciler(config=config, scheduler=scheduler, events=events)
        reconciler.reconcile(initial=True)
        runners = list(scheduler.runners.values())
        sem = asyncio.Semaphore(config.limits.global_concurrency)

        async def _one(runner):
            async with sem:
                try:
                    await runner.poll()
                except Exception as exc:
                    events.emit("feed.poll-failed", runner.name, error=f"internal: {exc!r}")

        await asyncio.gather(*(_one(r) for r in runners))
        events.emit("daemon.stopping", None)
        await scheduler.client.aclose()
        return 0
    finally:
        events.close()


async def poll_one(config: Config, name: str) -> int:
    config.ensure_dirs()
    events = EventLog(config)
    events.open()
    try:
        scheduler = Scheduler(config, events)
        reconciler = Reconciler(config=config, scheduler=scheduler, events=events)
        reconciler.reconcile(initial=True)
        runner = scheduler.runners.get(name)
        if runner is None:
            print(f"rssd: no such feed: {name}")
            return 1
        await runner.poll()
        await scheduler.client.aclose()
        return 0
    finally:
        events.close()
