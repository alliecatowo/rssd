from __future__ import annotations

import asyncio

import pytest

from rssd.fulltext import FulltextError, check_public_url
from rssd.userconf import ReaderConfig, is_openable_link, load_reader_config


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ],
)
def test_ssrf_guard_rejects(url):
    with pytest.raises(FulltextError):
        asyncio.run(check_public_url(url))


def test_ssrf_guard_allows_public_ip_literal():
    asyncio.run(check_public_url("http://93.184.216.34/"))


@pytest.mark.parametrize(
    "link,ok",
    [
        ("https://e.com/a", True), ("HTTP://e.com", True), ("mailto:a@b.c", True),
        ("file:///etc/passwd", False), ("smb://host/share", False),
        ("--help", False), ("javascript:alert(1)", False), ("", False), (None, False),
    ],
)
def test_is_openable_link(link, ok):
    assert is_openable_link(link) is ok


def test_cwd_config_cannot_set_browser(tmp_path, monkeypatch):
    (tmp_path / "rss.toml").write_text('browser = "sh"\nwidth = 70\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RSS_CONFIG", raising=False)
    rc, src = load_reader_config(None)
    assert src is not None and rc.width == 70
    assert rc.browser == ReaderConfig().browser
