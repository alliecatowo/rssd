"""Regression tests for the 0.3.0 hardening pass."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest

from rssd import fulltext
from rssd.cli import main
from rssd.config import Config, Limits
from rssd.daemon import LockedError, RootLock, run_once
from rssd.events import EventLog
from rssd.fetch import build_client, fetch_feed, parse_retry_after
from rssd.parse import parse_feed
from rssd.scheduler import Scheduler
from rssd.subscriptions import SubscriptionError, parse_subscription
from rssd.userconf import load_reader_config

LIMITS = Limits()


def _mock_client(handler) -> httpx.AsyncClient:
    client = build_client(LIMITS)
    client._transport = httpx.MockTransport(handler)
    return client


# ── DNS rebinding: connect to the address that was checked ──────────────────


def test_fulltext_connects_to_the_checked_ip(monkeypatch):
    answers = iter(["93.184.216.34", "127.0.0.1"])  # rebinding server

    async def fake_getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_getaddrinfo)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"<html>hi</html>")

    async def run():
        client = _mock_client(handler)
        try:
            return await fulltext.fetch_article_html(
                client, "https://rebind.example/a?x=1", limits=LIMITS
            )
        finally:
            await client.aclose()

    assert asyncio.run(run()) == "<html>hi</html>"
    assert seen[0].url.host == "93.184.216.34"  # not re-resolved to 127.0.0.1
    assert seen[0].headers["host"] == "rebind.example"
    assert seen[0].extensions["sni_hostname"] == "rebind.example"


# ── fetch ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected", [("-5", 0.0), ("1.5", 1.5), ("30", 30.0), ("nan", None), ("junk", None)]
)
def test_retry_after_clamped(value, expected):
    assert parse_retry_after(value) == expected


def test_temporary_redirect_is_not_pinned():
    def handler(request):
        if request.url.path == "/feed":
            return httpx.Response(302, headers={"Location": "http://e.test/tmp"})
        return httpx.Response(200, content=b"<rss/>")

    async def run():
        client = _mock_client(handler)
        try:
            return await fetch_feed(client, "http://e.test/feed", limits=LIMITS)
        finally:
            await client.aclose()

    assert asyncio.run(run()).resolved_url is None


# ── parse / subscriptions ────────────────────────────────────────────────────


def test_html_page_is_not_a_feed():
    assert not parse_feed(b"<html><body>Sign in</body></html>", "text/html", "http://e/").recognized
    assert parse_feed(
        b'<rss version="2.0"><channel><title>t</title></channel></rss>', None, "http://e/"
    ).recognized


def test_subscription_rejects_non_http_scheme(tmp_path):
    with pytest.raises(SubscriptionError):
        parse_subscription(b"<subscription><url>file:///etc/passwd</url></subscription>", tmp_path / "a.xml")


def test_subscription_does_not_expand_entities(tmp_path):
    xml = (
        b'<!DOCTYPE s [<!ENTITY x "http://evil.example/">]>'
        b"<subscription><url>http://ok.example/f&x;</url></subscription>"
    )
    try:
        sub = parse_subscription(xml, tmp_path / "a.xml")
    except SubscriptionError:
        return
    assert "evil.example" not in sub.url


# ── reader config ────────────────────────────────────────────────────────────


def test_reader_config_bad_values_reported_and_defaulted(tmp_path, monkeypatch, capsys):
    f = tmp_path / "rss.toml"
    f.write_text('width = "wide"\nsort = "sideways"\npreview = 3\nscrolloff = 5\n')
    monkeypatch.setenv("RSS_CONFIG", str(f))
    cfg, _ = load_reader_config(None)
    err = capsys.readouterr().err
    assert cfg.width == 80 and cfg.sort == "newest" and cfg.preview is True
    assert cfg.scrolloff == 5
    assert "width" in err and "sort" in err and "preview" in err


def test_reader_config_malformed_toml_reported(tmp_path, monkeypatch, capsys):
    f = tmp_path / "rss.toml"
    f.write_text("width = = 3")
    monkeypatch.setenv("RSS_CONFIG", str(f))
    load_reader_config(None)
    assert "malformed" in capsys.readouterr().err


# ── scheduler ────────────────────────────────────────────────────────────────


def _sched(tmp_path):
    config = Config(root=tmp_path)
    config.ensure_dirs()
    events = EventLog(config)
    events.open()
    return config, events


def test_each_feed_has_one_live_due_time(tmp_path):
    async def run():
        config, events = _sched(tmp_path)
        s = Scheduler(config, events)
        from rssd.models import Subscription

        sub = Subscription(name="a", url="http://127.0.0.1:1/f", source_path=tmp_path / "a.xml")
        s.add(sub)
        s.stagger(["a"])
        s.poll_soon("a")
        live = [d for d in s._heap if s._due.get(d.name) == d.at]
        assert len(live) == 1
        await s.client.aclose()

    asyncio.run(run())


def test_anomaly_response_is_not_cached(tmp_path):
    def feed(n, start=0):
        items = "".join(
            f"<item><title>t{i}</title><link>http://e/{i}</link><guid>g{i}</guid></item>"
            for i in range(start, start + n)
        )
        return f'<rss version="2.0"><channel><title>x</title>{items}</channel></rss>'.encode()

    state = {"body": feed(1), "etag": '"one"'}

    def handler(request):
        return httpx.Response(200, content=state["body"], headers={"ETag": state["etag"]})

    async def run():
        config, events = _sched(tmp_path)
        s = Scheduler(config, events)
        from rssd.models import Subscription

        runner = s.add(Subscription(name="a", url="http://e.test/f", source_path=tmp_path / "a.xml"))
        runner.client._transport = httpx.MockTransport(handler)
        await runner.poll()
        assert runner.state.etag == '"one"' and len(runner.seen) == 1
        old_hash = runner.state.body_hash
        state.update(body=feed(400, 1), etag='"many"')
        await runner.poll()
        assert runner.state.etag == '"one"' and runner.state.body_hash == old_hash
        assert runner.state.health == "degraded"
        assert len(runner.seen) == 1
        await s.client.aclose()

    asyncio.run(run())


def test_non_feed_200_counts_as_failure(tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"<html>captive portal</html>")

    async def run():
        config, events = _sched(tmp_path)
        s = Scheduler(config, events)
        from rssd.models import Subscription

        runner = s.add(Subscription(name="a", url="http://e.test/f", source_path=tmp_path / "a.xml"))
        runner.client._transport = httpx.MockTransport(handler)
        await runner.poll()
        assert runner.state.failures == 1 and "not a feed" in runner.state.last_error
        assert runner.state.body_hash is None
        await s.client.aclose()

    asyncio.run(run())


# ── daemon ───────────────────────────────────────────────────────────────────


def test_once_refuses_while_daemon_holds_the_lock(tmp_path):
    config = Config(root=tmp_path)
    config.ensure_dirs()
    with RootLock(config.lock_path):
        assert asyncio.run(run_once(config)) == 1
    assert config.lock_path.exists()  # not unlinked on release
    RootLock(config.lock_path).acquire()  # and it is free again


def test_lock_is_exclusive(tmp_path):
    a, b = RootLock(tmp_path / "l"), RootLock(tmp_path / "l")
    a.acquire()
    with pytest.raises(LockedError):
        b.acquire()
    a.release()


# ── OPML ─────────────────────────────────────────────────────────────────────

OPML = b"""<?xml version="1.0"?>
<opml version="2.0"><body>
  <outline text="Tech"><outline type="rss" text="A" xmlUrl="https://a.example/feed?x=1&amp;y=2"/>
  <outline type="rss" text="B" xmlUrl="https://a.example/other"/></outline>
  <outline type="rss" text="bad" xmlUrl="file:///etc/passwd"/>
</body></opml>"""


def test_opml_roundtrip(tmp_path, capsys):
    f = tmp_path / "in.opml"
    f.write_bytes(OPML)
    root = str(tmp_path / "r")
    assert main(["import", str(f), "--root", root]) == 0
    assert main(["import", str(f), "--root", root]) == 0  # idempotent
    assert main(["validate", "--root", root]) == 0
    capsys.readouterr()
    assert main(["export", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "https://a.example/feed?x=1&amp;y=2" in out and "https://a.example/other" in out
    assert "passwd" not in out
