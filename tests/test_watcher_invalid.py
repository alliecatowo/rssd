"""A subscription file that goes invalid must not retire its running feed."""

from __future__ import annotations

import asyncio

from rssd.config import Config
from rssd.events import EventLog
from rssd.scheduler import Scheduler
from rssd.watcher import Reconciler

GOOD = "<subscription><url>http://127.0.0.1:1/f.xml</url><name>a</name></subscription>"


def test_invalid_edit_keeps_feed_running(tmp_path):
    async def run():
        config = Config(root=tmp_path)
        config.ensure_dirs()
        events = EventLog(config)
        events.open()
        sched = Scheduler(config, events)
        rec = Reconciler(config, sched, events)
        path = config.feeds_d / "a.xml"
        path.write_text(GOOD)
        rec.reconcile(initial=True)
        assert "a" in sched.runners

        path.write_text("<subscription><oops>")  # typo mid-edit
        rec.reconcile()
        assert rec._pending_removal == {}
        assert "a" in sched.runners

        path.unlink()  # genuinely removed: still retired
        rec.reconcile()
        assert "a" in rec._pending_removal
        for h in rec._pending_removal.values():
            h.cancel()

    asyncio.run(run())
