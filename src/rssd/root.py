"""Where an instance lives when the user did not say.

Resolution order for the instance root:

    --root <dir>        explicit; always wins
    $RSSD_ROOT          explicit via the environment
    <user data dir>/rssd   e.g. ~/.local/share/rssd (platformdirs)

Both `rssd` and `rss` go through `resolve_root`, so they always agree on which
instance they mean. The current directory is never an instance by accident.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

from .config import Config

ENV_ROOT = "RSSD_ROOT"

ADD_HINT = "no feeds yet -- add one:  rssd add <url>"


def default_root() -> Path:
    """`$RSSD_ROOT` if set and non-empty, else the platform user data dir."""
    env = os.environ.get(ENV_ROOT, "").strip()
    if env:
        return Path(env).expanduser()
    return Path(user_data_dir("rssd", appauthor=False))


@dataclass(frozen=True, slots=True)
class ResolvedRoot:
    path: Path
    #: True when the user named it (flag or $RSSD_ROOT); False for the built-in
    #: default.
    explicit: bool


def resolve_root(arg: str | os.PathLike[str] | None) -> ResolvedRoot:
    if arg is not None and str(arg) != "":
        return ResolvedRoot(Path(arg).expanduser(), True)
    return ResolvedRoot(default_root(), bool(os.environ.get(ENV_ROOT, "").strip()))


def is_instance(root: Path) -> bool:
    return (root / "feeds.d").is_dir()


def _is_empty_or_missing(root: Path) -> bool:
    if not root.exists():
        return True
    try:
        return root.is_dir() and not any(root.iterdir())
    except OSError:
        return False


def ensure_instance(resolved: ResolvedRoot, *, quiet: bool = False) -> bool:
    """Create an empty instance at `resolved.path` if it is warranted.

    The built-in default (and `$RSSD_ROOT`) is initialised whenever it is not
    yet an instance. A root passed with `--root` is only initialised if it is
    missing or an empty directory, so pointing at some unrelated populated
    directory never scatters files into it. Returns True if it created one.
    """
    root = resolved.path
    if is_instance(root):
        return False
    if resolved.explicit and not _is_empty_or_missing(root):
        return False
    try:
        Config(root=root).ensure_dirs()
    except OSError as exc:
        print(f"rssd: cannot create {root}: {exc}", file=sys.stderr)
        return False
    if not quiet:
        print(f"rssd: created empty instance at {root}\n      {ADD_HINT}", file=sys.stderr)
    return True


def open_root(arg: str | os.PathLike[str] | None, *, quiet: bool = False) -> Path:
    """Resolve the root and auto-initialise it if appropriate."""
    resolved = resolve_root(arg)
    ensure_instance(resolved, quiet=quiet)
    return resolved.path
