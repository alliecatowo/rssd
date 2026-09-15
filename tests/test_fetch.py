"""Tests for rssd.fetch. All network access goes through httpx.MockTransport
-- nothing here ever touches the real network."""

from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from rssd.config import Limits
from rssd.fetch import (
    build_client,
    classify_error,
    fetch_feed,
    parse_max_age,
    parse_retry_after,
)


def make_client(handler, limits: Limits) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    client = build_client(limits)
    client._transport = transport  # swap in the mock transport
    return client


LIMITS = Limits()


async def test_200_ok_returns_body_and_validators():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"].startswith("rssd/")
        assert "Accept" in request.headers
        return httpx.Response(
            200,
            content=b"<rss></rss>",
            headers={
                "ETag": '"abc123"',
                "Last-Modified": "Tue, 15 Sep 2026 12:00:00 GMT",
                "Cache-Control": "max-age=600",
                "Content-Type": "application/rss+xml",
            },
        )

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.ok
    assert result.status == 200
    assert result.body == b"<rss></rss>"
    assert result.etag == '"abc123"'
    assert result.last_modified == "Tue, 15 Sep 2026 12:00:00 GMT"
    assert result.max_age == 600


async def test_sends_conditional_headers_only_when_supplied():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["inm"] = request.headers.get("If-None-Match")
        seen["ims"] = request.headers.get("If-Modified-Since")
        return httpx.Response(200, content=b"<rss></rss>")

    client = make_client(handler, LIMITS)
    try:
        await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()
    assert seen["inm"] is None
    assert seen["ims"] is None

    client2 = make_client(handler, LIMITS)
    try:
        await fetch_feed(
            client2,
            "http://example.test/feed.xml",
            etag='"xyz"',
            last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
            limits=LIMITS,
        )
    finally:
        await client2.aclose()
    assert seen["inm"] == '"xyz"'
    assert seen["ims"] == "Mon, 01 Jan 2026 00:00:00 GMT"


async def test_304_not_modified_has_no_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={"ETag": '"abc123"'})

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(
            client, "http://example.test/feed.xml", etag='"abc123"', limits=LIMITS
        )
    finally:
        await client.aclose()

    assert result.status == 304
    assert result.not_modified
    assert result.body is None


async def test_429_with_integer_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"})

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.status == 429
    assert result.retry_after == 120.0


async def test_503_with_http_date_retry_after():
    future = datetime.now(timezone.utc) + timedelta(seconds=90)
    http_date = format_datetime(future, usegmt=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": http_date})

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.status == 503
    assert result.retry_after is not None
    assert 80 <= result.retry_after <= 100


async def test_redirect_chain_populates_resolved_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.xml":
            return httpx.Response(
                301, headers={"Location": "http://example.test/mid.xml"}
            )
        if request.url.path == "/mid.xml":
            return httpx.Response(
                302, headers={"Location": "http://example.test/new.xml"}
            )
        return httpx.Response(200, content=b"<rss></rss>")

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/old.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.ok
    assert result.resolved_url == "http://example.test/new.xml"


async def test_oversize_via_content_length_is_rejected_early():
    big = LIMITS.max_body_bytes + 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": str(big)},
            content=b"x" * 100,  # body itself is small; header lies, but we reject early
        )

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.status != 200 or result.error_kind == "too-large"
    assert result.error_kind == "too-large"
    assert result.body is None


async def test_oversize_streamed_without_content_length_is_caught():
    """The naive `response.content` read would buffer this fully; we must
    abort mid-stream by counting bytes as they arrive."""
    chunk = b"a" * (256 * 1024)
    chunks_needed = LIMITS.max_body_bytes // len(chunk) + 2

    def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            for _ in range(chunks_needed):
                yield chunk

        return httpx.Response(200, content=gen())

    small_limits = Limits(max_body_bytes=1024 * 1024)  # 1 MiB, smaller for a fast test
    client = make_client(handler, small_limits)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=small_limits)
    finally:
        await client.aclose()

    assert result.error_kind == "too-large"
    assert result.body is None


async def test_transport_error_never_raises_and_reports_status_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.status == 0
    assert result.error_kind is not None
    assert result.body is None


async def test_timeout_classified_correctly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = make_client(handler, LIMITS)
    try:
        result = await fetch_feed(client, "http://example.test/feed.xml", limits=LIMITS)
    finally:
        await client.aclose()

    assert result.status == 0
    assert result.error_kind == "timeout"


def test_parse_retry_after_integer():
    assert parse_retry_after("30") == 30.0


def test_parse_retry_after_http_date():
    future = datetime.now(timezone.utc) + timedelta(seconds=60)
    http_date = format_datetime(future, usegmt=True)
    value = parse_retry_after(http_date)
    assert value is not None
    assert 30 <= value <= 90


def test_parse_retry_after_none():
    assert parse_retry_after(None) is None
    assert parse_retry_after("not a date") is None


def test_parse_max_age():
    assert parse_max_age("max-age=600") == 600
    assert parse_max_age("no-cache, max-age=120") == 120
    assert parse_max_age(None) is None
    assert parse_max_age("no-cache") is None


def test_classify_error_kinds():
    assert classify_error(httpx.ReadTimeout("x", request=None)) == "timeout"
    assert classify_error(httpx.ConnectTimeout("x", request=None)) == "timeout"
    assert classify_error(httpx.TransportError("x", request=None)) == "network"


async def test_transport_error_message_is_never_empty():
    """Several httpx exceptions -- ConnectError especially -- carry no message,
    so str(exc) is "". A caller that falls back on the status code then reports
    "HTTP 0", which tells an operator nothing about whether they have a DNS
    problem, a dead server, or no network at all.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_feed(client, "https://example.com/feed.xml", limits=Limits())

    assert result.status == 0
    assert result.error, "transport failure reported an empty message"
    assert "ConnectError" in result.error
