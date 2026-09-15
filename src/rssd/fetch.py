"""Conditional GET over httpx: caps, redirects, error classification.

SPEC §10.1. This module is effectful but must never raise -- ``fetch_feed``
catches every transport exception and turns it into a ``FetchResult`` with
``status=0`` so a single bad feed cannot take down the daemon.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx

from rssd.config import ACCEPT, USER_AGENT
from rssd.config import Limits
from rssd.models import ErrorKind, FetchResult


def build_client(limits: Limits) -> httpx.AsyncClient:
    """Build the shared AsyncClient used for all polling."""
    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=limits.max_redirects,
        timeout=limits.request_timeout,
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header: delta-seconds or an HTTP-date."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(int(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = (dt - now).total_seconds()
    return max(0.0, delta)


def parse_max_age(cache_control: str | None) -> int | None:
    """Extract ``max-age=N`` from a ``Cache-Control`` header, if present."""
    if not cache_control:
        return None
    for part in cache_control.split(","):
        part = part.strip()
        if part.lower().startswith("max-age"):
            _, _, raw = part.partition("=")
            raw = raw.strip()
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def classify_error(exc: BaseException) -> ErrorKind:
    """Map a transport exception to an ``ErrorKind``."""
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        if "certificate" in msg or "ssl" in msg or "tls" in msg:
            return "tls"
        if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return "dns"
        return "network"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http"
    if isinstance(exc, (httpx.DecodingError,)):
        return "parse"
    if isinstance(exc, httpx.TransportError):
        return "network"
    return "network"


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    limits: Limits,
) -> FetchResult:
    """Perform one conditional GET, enforcing the body-size cap by streaming.

    Never raises: any transport exception is caught and reported as a
    ``FetchResult`` with ``status=0`` and a populated ``error_kind``.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": ACCEPT,
    }
    if etag is not None:
        headers["If-None-Match"] = etag
    if last_modified is not None:
        headers["If-Modified-Since"] = last_modified

    try:
        async with client.stream("GET", url, headers=headers) as response:
            resolved_url: str | None = None
            if str(response.url) != url:
                resolved_url = str(response.url)

            cache_control = response.headers.get("Cache-Control")
            max_age = parse_max_age(cache_control)
            retry_after = parse_retry_after(response.headers.get("Retry-After"))

            if response.status_code == 304:
                await response.aclose()
                return FetchResult(
                    status=304,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    max_age=max_age,
                    retry_after=retry_after,
                    resolved_url=resolved_url,
                )

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > limits.max_body_bytes:
                        await response.aclose()
                        return FetchResult(
                            status=response.status_code,
                            resolved_url=resolved_url,
                            error="response too large (Content-Length)",
                            error_kind="too-large",
                        )
                except ValueError:
                    pass

            chunks: list[bytes] = []
            total = 0
            too_large = False
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limits.max_body_bytes:
                    too_large = True
                    break
                chunks.append(chunk)

            if too_large:
                return FetchResult(
                    status=response.status_code,
                    resolved_url=resolved_url,
                    error="response exceeded max_body_bytes while streaming",
                    error_kind="too-large",
                )

            body = b"".join(chunks)

            if response.status_code >= 400:
                return FetchResult(
                    status=response.status_code,
                    body=body,
                    content_type=response.headers.get("Content-Type"),
                    max_age=max_age,
                    retry_after=retry_after,
                    resolved_url=resolved_url,
                    error=f"HTTP {response.status_code}",
                    error_kind="http",
                )

            return FetchResult(
                status=response.status_code,
                body=body,
                content_type=response.headers.get("Content-Type"),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                max_age=max_age,
                retry_after=retry_after,
                resolved_url=resolved_url,
            )
    except httpx.HTTPError as exc:
        return FetchResult(
            status=0,
            error=str(exc),
            error_kind=classify_error(exc),
        )
    except Exception as exc:  # pragma: no cover - defense in depth
        return FetchResult(
            status=0,
            error=str(exc),
            error_kind="network",
        )
