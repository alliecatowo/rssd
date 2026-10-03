"""`rssd --version` and `rss --version` print the installed package version."""

from __future__ import annotations

import pytest

from rssd import __version__, cli, readercli


@pytest.mark.parametrize(
    ("main", "prog"), [(cli.main, "rssd"), (readercli.main, "rss")]
)
def test_version_flag(main, prog, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"{prog} {__version__}"
