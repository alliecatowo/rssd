"""Reader preferences.

Loaded, never saved. `rss` does not write files -- that is what lets it claim to
be a pure consumer of the daemon's tree. So `:set` in the TUI changes the
running session only, exactly like `:set` in vim, and anything you want to keep
goes in the rc file by hand.

Search order, first hit wins:

    $RSS_CONFIG
    ./rss.toml
    <root>/rss.toml
    $XDG_CONFIG_HOME/rss/config.toml   (default ~/.config/rss/config.toml)
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

SORTS = ("newest", "oldest", "title", "feed")
DATE_FORMATS = ("relative", "iso", "short")


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ReaderConfig:
    #: Column width for rendered article text. 0 means "fit the terminal".
    width: int = 80
    sort: str = "newest"
    #: Show `**bold**` / `` `code` `` markers in rendered text.
    marks: bool = True
    #: Append a numbered link reference list, the way a text browser does.
    links: bool = True
    date_format: str = "relative"
    #: Command used by `o`. Empty means fall back to $BROWSER, then xdg-open.
    browser: str = ""
    #: Show the reading pane in the TUI. Off gives a plain two-column list.
    preview: bool = True
    #: Lines of context kept above/below the cursor, like vim's scrolloff.
    scrolloff: int = 3
    #: Show relative line numbers in the entry list.
    relativenumber: bool = False
    number: bool = False
    #: Blank-line markers below the last entry, like vim's empty buffer tildes.
    tildes: bool = True

    def describe(self) -> list[tuple[str, str]]:
        out = []
        for f in fields(self):
            value = getattr(self, f.name)
            out.append((f.name, "true" if value is True else "false" if value is False else str(value)))
        return out


_BOOL_TRUE = {"true", "yes", "on", "1"}
_BOOL_FALSE = {"false", "no", "off", "0"}


def set_option(config: ReaderConfig, key: str, value: str | None) -> ReaderConfig:
    """Apply one `:set` assignment, vim-style.

    Supports `:set preview`, `:set nopreview`, `:set preview!` and
    `:set width=100`. Raises ConfigError with a readable message rather than
    silently ignoring a typo -- a setting that quietly does nothing is worse
    than one that complains.
    """
    key = key.strip()
    toggle = key.endswith("!")
    if toggle:
        key = key[:-1]
    negate = False
    known = {f.name for f in fields(ReaderConfig)}
    if key not in known and key.startswith("no") and key[2:] in known:
        key, negate = key[2:], True
    if key not in known:
        raise ConfigError(f"unknown option: {key}")

    current = getattr(config, key)

    if isinstance(current, bool):
        if toggle:
            return replace(config, **{key: not current})
        if negate:
            return replace(config, **{key: False})
        if value is None:
            return replace(config, **{key: True})
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE:
            return replace(config, **{key: True})
        if lowered in _BOOL_FALSE:
            return replace(config, **{key: False})
        raise ConfigError(f"{key} expects a boolean, got {value!r}")

    if value is None:
        raise ConfigError(f"{key} needs a value, e.g. :set {key}=...")

    if isinstance(current, int):
        try:
            return replace(config, **{key: int(value)})
        except ValueError as exc:
            raise ConfigError(f"{key} expects a number, got {value!r}") from exc

    if key == "sort" and value not in SORTS:
        raise ConfigError(f"sort must be one of: {', '.join(SORTS)}")
    if key == "date_format" and value not in DATE_FORMATS:
        raise ConfigError(f"date_format must be one of: {', '.join(DATE_FORMATS)}")
    return replace(config, **{key: value})


def config_search_paths(root: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if env := os.environ.get("RSS_CONFIG"):
        paths.append(Path(env))
    paths.append(Path("rss.toml"))
    if root is not None:
        paths.append(root / "rss.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    paths.append(base / "rss" / "config.toml")
    return paths


def load_reader_config(root: Path | None = None) -> tuple[ReaderConfig, Path | None]:
    """Return the effective config and the file it came from, if any.

    A malformed or unreadable config is reported, not swallowed -- but it never
    stops `rss` from starting, because being unable to read your feeds because
    of a typo in a preferences file is a bad trade.
    """
    for path in config_search_paths(root):
        try:
            if not path.is_file():
                continue
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        config = ReaderConfig()
        known = {f.name for f in fields(ReaderConfig)}
        for key, value in data.items():
            if key not in known:
                continue
            try:
                config = replace(config, **{key: value})
            except TypeError:
                continue
        return config, path
    return ReaderConfig(), None
