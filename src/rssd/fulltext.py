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


async def fetch_article_html(
    client: httpx.AsyncClient, url: str, *, limits: Limits
) -> str:
    """Fetch a single article page, under the same size cap as a feed."""
    try:
        async with client.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.1"},
            timeout=limits.request_timeout,
        ) as response:
            if response.status_code != 200:
                raise FulltextError(f"HTTP {response.status_code}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limits.max_body_bytes:
                    raise FulltextError("article exceeds size cap")
                chunks.append(chunk)
    except FulltextError:
        raise
    except Exception as exc:  # network, TLS, timeout, redirect loop
        raise FulltextError(str(exc)) from exc

    body = b"".join(chunks)
    encoding = response.charset_encoding or "utf-8"
    return body.decode(encoding, errors="replace")


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
