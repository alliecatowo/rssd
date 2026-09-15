"""Persistence for per-feed polling state.

This is a cache, not a source of truth (SPEC invariant I1). If a state file is
missing, truncated, or unparseable we throw it away and start clean: the cost is
one unconditional GET plus a rescan of the entry tree, and the rescan is
idempotent. That tradeoff is why none of this needs a schema version or a
migration story.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .config import Config
from .models import FeedState
from .store import atomic_write

#: Fields we accept off disk. Anything else in the JSON is ignored, so an older
#: or newer daemon writing extra keys is harmless.
_FIELDS = {f.name for f in dataclasses.fields(FeedState)}


def load_state(config: Config, name: str) -> FeedState:
    path = config.state_path(name)
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return FeedState(name=name)
    if not isinstance(raw, dict):
        return FeedState(name=name)
    kwargs = {k: v for k, v in raw.items() if k in _FIELDS}
    kwargs["name"] = name
    try:
        return FeedState(**kwargs)
    except TypeError:
        return FeedState(name=name)


def save_state(config: Config, state: FeedState) -> None:
    path = config.state_path(state.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dataclasses.asdict(state), indent=1, sort_keys=True).encode()
    atomic_write(path, data, fsync=not config.no_fsync)


def prune_revision_log(state: FeedState, today: str, keep_ids: set[str]) -> None:
    """Keep the revision log from growing without bound.

    It only exists to enforce a *daily* cap, so yesterday's timestamps are dead
    weight, as are entries that have fallen out of the feed window.
    """
    for entry_id in list(state.revision_log):
        if entry_id not in keep_ids:
            del state.revision_log[entry_id]
            continue
        kept = [ts for ts in state.revision_log[entry_id] if ts.startswith(today)]
        if kept:
            state.revision_log[entry_id] = kept
        else:
            del state.revision_log[entry_id]
