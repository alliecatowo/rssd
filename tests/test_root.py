"""Default instance root resolution and first-run auto-init."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rssd import cli, readercli
from rssd.root import ensure_instance, is_instance, resolve_root


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("RSSD_ROOT", raising=False)
    monkeypatch.delenv("RSS_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def test_default_is_user_data_dir(isolated):
    r = resolve_root(None)
    assert r.path == isolated / ".local" / "share" / "rssd"
    assert not r.explicit


def test_env_overrides_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RSSD_ROOT", str(tmp_path / "envroot"))
    r = resolve_root(None)
    assert r.path == tmp_path / "envroot"
    assert r.explicit


def test_flag_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RSSD_ROOT", str(tmp_path / "envroot"))
    assert resolve_root(str(tmp_path / "flag")).path == tmp_path / "flag"


def test_cwd_is_never_the_default(tmp_path):
    assert resolve_root(None).path != tmp_path


def test_default_root_autoinit(isolated, capsys):
    root = resolve_root(None).path
    assert ensure_instance(resolve_root(None)) is True
    assert is_instance(root)
    assert (root / "store").is_dir() and (root / "var").is_dir()
    assert list((root / "feeds.d").iterdir()) == []
    assert "rssd add" in capsys.readouterr().err
    # second call is a no-op
    assert ensure_instance(resolve_root(None)) is False


def test_explicit_missing_and_empty_are_initialised(tmp_path):
    missing = tmp_path / "new"
    assert ensure_instance(resolve_root(str(missing)), quiet=True)
    assert is_instance(missing)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ensure_instance(resolve_root(str(empty)), quiet=True)
    assert is_instance(empty)


def test_explicit_populated_dir_is_left_alone(tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    assert ensure_instance(resolve_root(str(busy)), quiet=True) is False
    assert sorted(p.name for p in busy.iterdir()) == ["notes.txt"]


def test_env_root_populated_dir_is_left_alone(tmp_path, monkeypatch):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    monkeypatch.setenv("RSSD_ROOT", str(busy))
    assert ensure_instance(resolve_root(None), quiet=True) is False


@pytest.mark.parametrize("main", [cli.main, readercli.main])
def test_both_binaries_use_the_same_default(main, isolated, monkeypatch):
    seen = {}
    pytest.importorskip("textual")
    from rssd.tui import app as tui_app

    monkeypatch.setattr(tui_app, "run_tui", lambda c, rc: seen.setdefault("root", c.root) and 0)
    assert main(["tui"]) == 0
    assert seen["root"] == isolated / ".local" / "share" / "rssd"
    assert is_instance(seen["root"])


def test_rss_list_on_fresh_home(isolated, capsys):
    assert readercli.main(["feeds"]) == 0
    assert is_instance(isolated / ".local" / "share" / "rssd")


def test_rss_config_shows_where_file_would_go(isolated, capsys):
    assert readercli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "no config file found" in out
    assert str(isolated / ".config" / "rss" / "config.toml") in out
    assert readercli.main(["config", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] is None
    assert data["default_path"].endswith("rss/config.toml")


def test_rssd_add_writes_subscription(isolated, capsys):
    assert cli.main(["add", "https://www.example.com/feed.xml"]) == 0
    root = isolated / ".local" / "share" / "rssd"
    assert (root / "feeds.d" / "example.com.xml").exists()
    assert cli.main(["add", "https://www.example.com/feed.xml"]) == 1
    assert cli.main(["add", "ftp://x"]) == 2
    assert cli.main(["validate"]) == 0


def test_init_defaults_to_user_data_dir(isolated):
    assert cli.main(["init"]) == 0
    assert (isolated / ".local" / "share" / "rssd" / "feeds.d" / "lobsters.xml").exists()
def test_rss_list_alias(isolated, capsys):
    assert readercli.main(["list"]) == 0
