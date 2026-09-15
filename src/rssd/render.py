"""Entry -> XML bytes + validation (SPEC §6). PURE.

Renders the document shape from SPEC §6 and enforces invariant I3: every
serialised entry is re-parsed with a strict parser before the caller may
persist it. If the content subtree cannot be made well-formed, degrade to a
plain-text ``<content unparsable="true">`` rather than ever returning
malformed XML.

Deliberately does NOT import rssd.timeutil (owned by another module) --
timestamps are formatted inline as ``YYYY-MM-DDTHH:MM:SSZ``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from rssd.config import NS
from rssd.models import FeedMeta, FeedState, PreparedEntry, RenderContext
from rssd.semantic import canonical_xml

_NSMAP = {None: NS}


def _fmt_ts(dt: datetime) -> str:
    """Render a timestamp as ``2026-09-15T21:14:02Z`` -- UTC, second
    precision, Z suffix."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _el(tag: str, text: str | None = None, **attrs: str | None) -> etree._Element:
    e = etree.Element(f"{{{NS}}}{tag}", nsmap=_NSMAP)
    for k, v in attrs.items():
        if v is not None:
            e.set(k.rstrip("_").replace("_", "-"), v)
    if text is not None:
        e.text = text
    return e


def _sub(parent: etree._Element, tag: str, text: str | None = None, **attrs) -> etree._Element:
    e = _el(tag, text, **attrs)
    parent.append(e)
    return e


def render_entry(
    prepared: PreparedEntry,
    ctx: RenderContext,
    *,
    revision: int,
    first_seen: datetime,
    updated: datetime | None = None,
) -> bytes:
    parsed = prepared.parsed

    root = etree.Element(
        f"{{{NS}}}entry",
        nsmap=_NSMAP,
        attrib={
            "id": prepared.id,
            "revision": str(revision),
            "first-seen": _fmt_ts(first_seen),
        },
    )
    if updated is not None:
        root.set("updated", _fmt_ts(updated))

    source = _sub(root, "source")
    if parsed.link:
        _sub(source, "link", parsed.link)
    _sub(source, "feed", ctx.feed_url, name=ctx.feed_name, title=ctx.feed_title)
    _sub(source, "guid", parsed.raw_id, basis=parsed.basis)

    _sub(root, "title", parsed.title or "")

    if parsed.author:
        _sub(root, "author", parsed.author)

    _sub(root, "published", _fmt_ts(prepared.published), origin=prepared.published_origin)

    if parsed.categories:
        cats = _sub(root, "categories")
        for c in parsed.categories:
            _sub(cats, "category", c)

    content_el = _build_content_element(prepared)
    root.append(content_el)

    data = _serialize(root)

    try:
        assert_well_formed(data)
    except ValueError:
        # Degrade: replace <content> with a plain-text, unparsable version.
        text = _extract_text(prepared.content)
        degraded = _el(
            "content",
            unparsable="true",
            origin=(content_el.get("origin") or "none"),
        )
        if text:
            degraded.text = text
        # Rebuild root with the degraded content in place.
        for child in list(root):
            if child.tag == f"{{{NS}}}content":
                root.remove(child)
        root.append(degraded)
        data = _serialize(root)
        assert_well_formed(data)

    return data


def _build_content_element(prepared: PreparedEntry) -> etree._Element:
    origin = prepared.parsed.content_origin
    content = prepared.content
    el = etree.Element(f"{{{NS}}}content", nsmap=_NSMAP)
    el.set("origin", origin)
    if origin != "none":
        el.set("hash", prepared.content_hash)
    for child in content:
        el.append(_copy_el(child))
    if content.text and content.text.strip():
        el.text = content.text
    return el


def _copy_el(el: etree._Element) -> etree._Element:
    """Deep-copy an element into the render namespace tree (elements from
    semantic.py are already in the rssd NS, so this is a plain deepcopy)."""
    import copy

    return copy.deepcopy(el)


def _extract_text(content: etree._Element) -> str:
    try:
        return " ".join(content.itertext()).strip()
    except Exception:
        return ""


def render_feed_meta(
    meta: FeedMeta,
    *,
    name: str,
    url: str,
    state: str = "active",
    retired_at: datetime | None = None,
) -> bytes:
    root = etree.Element(f"{{{NS}}}feed", nsmap=_NSMAP, attrib={"name": name})
    _sub(root, "url", url)
    if meta.title:
        _sub(root, "title", meta.title)
    if meta.link:
        _sub(root, "link", meta.link)
    if meta.description:
        _sub(root, "description", meta.description)
    if meta.language:
        _sub(root, "language", meta.language)
    if meta.ttl_seconds is not None:
        _sub(root, "ttl-seconds", str(meta.ttl_seconds))
    _sub(root, "state", state)
    if retired_at is not None:
        _sub(root, "retired-at", _fmt_ts(retired_at))

    data = _serialize(root)
    assert_well_formed(data)
    return data


def render_status(state: FeedState, *, entry_count: int) -> bytes:
    root = etree.Element(f"{{{NS}}}status", nsmap=_NSMAP, attrib={"name": state.name})
    _sub(root, "health", state.health)
    _sub(root, "entry-count", str(entry_count))
    _sub(root, "poll-count", str(state.poll_count))
    _sub(root, "failures", str(state.failures))
    if state.last_poll_at:
        _sub(root, "last-poll-at", state.last_poll_at)
    if state.last_error:
        _sub(root, "last-error", state.last_error)
    if state.identity_override:
        _sub(root, "identity-override", state.identity_override)

    data = _serialize(root)
    assert_well_formed(data)
    return data


def _serialize(root: etree._Element) -> bytes:
    # lxml always emits a single-quoted XML declaration; SPEC §6 shows
    # double quotes, so build it ourselves and append the (declaration-less)
    # body serialisation.
    body = etree.tostring(root, xml_declaration=False, encoding="utf-8")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + body


def assert_well_formed(data: bytes) -> None:
    """Re-parse ``data`` with a strict parser (invariant I3). Raises
    ValueError with a useful message on any failure."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"rendered document is not well-formed XML: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"rendered document is not well-formed XML: {exc}") from exc
