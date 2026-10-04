"""Opt-in article extraction.

Off by default, and deliberately so: the daemon's normal footprint is one HTTP
request per feed, never one per entry. A subscription has to ask for this
explicitly with <fulltext>true</fulltext>, at which point rssd will fetch the
linked page for entries the feed left empty and run a readability pass over it.

trafilatura is an optional dependency; if it isn't installed this degrades to a
no-op rather than breaking the daemon.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .config import USER_AGENT, Limits

try:  # pragma: no cover - exercised by the extra, not the core test run
    import trafilatura

    AVAILABLE = True
except ImportError:  # pragma: no cover
    trafilatura = None
    AVAILABLE = False


class FulltextError(Exception):
    pass


async def check_public_url(url: str) -> None:
    """SSRF guard: article URLs come from untrusted feeds. Allow only
    http/https, and refuse hosts that resolve to loopback, private,
    link-local, multicast or otherwise non-global addresses. (Resolution and
    connection are separate lookups, so this is best-effort against DNS
    rebinding, not a hard guarantee.)"""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise FulltextError(f"bad article URL: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise FulltextError(f"refusing non-http(s) article URL: {url!r}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise FulltextError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        addr = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise FulltextError(f"unparseable address for {host}: {addr}") from None
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise FulltextError(f"refusing non-public address for {host}")


_MAX_ARTICLE_REDIRECTS = 5


async def fetch_article_html(
    client: httpx.AsyncClient, url: str, *, limits: Limits
) -> str:
    """Fetch a single article page, under the same size cap as a feed.
    Redirects are followed by hand so every hop passes the SSRF guard."""
    try:
        for _hop in range(_MAX_ARTICLE_REDIRECTS + 1):
            await check_public_url(url)
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.1"},
                timeout=limits.request_timeout,
                follow_redirects=False,
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise FulltextError(f"HTTP {response.status_code}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limits.max_body_bytes:
                        raise FulltextError("article exceeds size cap")
                    chunks.append(chunk)
                charset = response.charset_encoding
            break
        else:
            raise FulltextError("too many redirects")
    except FulltextError:
        raise
    except Exception as exc:  # network, TLS, timeout
        raise FulltextError(str(exc)) from exc

    body = b"".join(chunks)
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:  # unknown charset label
        return body.decode("utf-8", errors="replace")


async def extract(
    client: httpx.AsyncClient, url: str, *, limits: Limits
) -> str | None:
    """Return article HTML for `url`, or None if nothing usable came back.

    Returns HTML rather than text so the result can go through the same
    semantic mapper as feed-supplied content -- one content pipeline, not two.
    """
    if not AVAILABLE:
        raise FulltextError("trafilatura is not installed; install the [fulltext] extra")

    html = await fetch_article_html(client, url, limits=limits)

    # trafilatura is synchronous and CPU-bound on large pages.
    def _run() -> str | None:
        return trafilatura.extract(
            html,
            output_format="html",
            include_links=True,
            include_images=True,
            favor_precision=True,
        )

    return await asyncio.to_thread(_run)
