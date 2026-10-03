"""Both `rssd tui` and `rss tui` must hand run_tui a Config and a ReaderConfig."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rssd import cli, readercli
from rssd.tui import app as tui_app
from rssd.userconf import ReaderConfig


@pytest.mark.parametrize("main", [cli.main, readercli.main])
def test_tui_entrypoint_passes_reader_config(main, tmp_path, monkeypatch):
    seen = {}

    def fake_run_tui(config, reader_config):
        seen["root"] = config.root
        seen["reader_config"] = reader_config
        return 0

    monkeypatch.setattr(tui_app, "run_tui", fake_run_tui)
    assert main(["tui", "--root", str(tmp_path)]) == 0
    assert seen["root"] == tmp_path
    assert isinstance(seen["reader_config"], ReaderConfig)
