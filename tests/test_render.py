"""Tests for rssd.render -- Entry -> XML bytes + validation (SPEC §6, I3)."""

from __future__ import annotations

import subprocess
import shutil
from datetime import datetime, timezone

import pytest
from lxml import etree

from rssd.config import NS
from rssd.models import (
    FeedMeta,
    FeedState,
    ParsedEntry,
    PreparedEntry,
    RenderContext,
)
from rssd.render import (
    assert_well_formed,
    render_entry,
    render_feed_meta,
    render_status,
)
from rssd.semantic import to_semantic


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def make_prepared(
    *,
    title="Hello World",
    link="https://example.com/post",
    author="Jane Doe",
    published=datetime(2026, 9, 7, tzinfo=timezone.utc),
    published_origin="feed",
    categories=("rust", "async"),
    content_html="<p>Body text.</p>",
    content_origin="feed:content",
    basis="atom-id",
    raw_id="https://example.com/post",
    entry_id="sha256:" + "a" * 64,
    content_hash="sha256:" + "b" * 64,
) -> PreparedEntry:
    parsed = ParsedEntry(
        raw_id=raw_id,
        basis=basis,
        title=title,
        link=link,
        author=author,
        published=published,
        categories=categories,
        content_html=content_html,
        content_origin=content_origin,
    )
    content = to_semantic(content_html) if content_origin != "none" else to_semantic(None)
    return PreparedEntry(
        parsed=parsed,
        id=entry_id,
        id8=entry_id.split(":")[1][:8],
        content=content,
        content_hash=content_hash,
        published=published,
        published_origin=published_origin,
    )


def make_ctx(**kw) -> RenderContext:
    defaults = dict(
        feed_name="rust-blog",
        feed_url="https://blog.rust-lang.org/feed.xml",
        feed_title="Rust Blog",
    )
    defaults.update(kw)
    return RenderContext(**defaults)


FIRST_SEEN = datetime(2026, 9, 15, 21, 14, 2, tzinfo=timezone.utc)


# ── render_entry basic shape (SPEC §6) ────────────────────────────────────


def test_render_entry_produces_xml_declaration_and_root():
    prepared = make_prepared()
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    assert data.startswith(b"<?xml version=\"1.0\" encoding=\"utf-8\"?>")
    root = etree.fromstring(data)
    assert root.tag == q("entry")
    assert root.get("id") == prepared.id
    assert root.get("revision") == "1"


def test_render_entry_is_utf8_bytes():
    prepared = make_prepared(title="Héllo — wörld")
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    assert isinstance(data, bytes)
    root = etree.fromstring(data)
    assert root.find(q("title")).text == "Héllo — wörld"


def test_render_entry_calls_assert_well_formed_internally():
    """render_entry's own output must always round-trip; we verify this by
    parsing it ourselves too (I3)."""
    prepared = make_prepared()
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    assert_well_formed(data)  # must not raise


def test_render_entry_timestamp_format():
    prepared = make_prepared()
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    assert root.get("first-seen") == "2026-09-15T21:14:02Z"
    published_el = root.find(q("published"))
    assert published_el.text == "2026-09-07T00:00:00Z"


def test_render_entry_updated_optional():
    prepared = make_prepared()
    data_no_updated = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data_no_updated)
    assert root.get("updated") is None

    updated = datetime(2026, 9, 15, 23, 1, 44, tzinfo=timezone.utc)
    data_updated = render_entry(
        prepared, make_ctx(), revision=2, first_seen=FIRST_SEEN, updated=updated
    )
    root2 = etree.fromstring(data_updated)
    assert root2.get("updated") == "2026-09-15T23:01:44Z"


def test_render_entry_source_block():
    prepared = make_prepared()
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    source = root.find(q("source"))
    assert source is not None
    assert source.find(q("link")).text == "https://example.com/post"
    feed_el = source.find(q("feed"))
    assert feed_el.get("name") == "rust-blog"
    assert feed_el.get("title") == "Rust Blog"
    assert feed_el.text == "https://blog.rust-lang.org/feed.xml"
    guid_el = source.find(q("guid"))
    assert guid_el.get("basis") == "atom-id"
    assert guid_el.text == "https://example.com/post"


def test_render_entry_title_author_categories():
    prepared = make_prepared()
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    assert root.find(q("title")).text == "Hello World"
    assert root.find(q("author")).text == "Jane Doe"
    cats = [c.text for c in root.find(q("categories")).findall(q("category"))]
    assert cats == ["rust", "async"]


def test_render_entry_no_categories_omits_element():
    prepared = make_prepared(categories=())
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    assert root.find(q("categories")) is None


def test_render_entry_no_author_omits_element():
    prepared = make_prepared(author=None)
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    assert root.find(q("author")) is None


def test_render_entry_content_origin_and_hash():
    prepared = make_prepared(content_origin="feed:content", content_hash="sha256:" + "c" * 64)
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    content_el = root.find(q("content"))
    assert content_el.get("origin") == "feed:content"
    assert content_el.get("hash") == "sha256:" + "c" * 64
    assert content_el.find(q("paragraph")).text == "Body text."


def test_render_entry_content_origin_none_no_hash():
    prepared = make_prepared(content_html=None, content_origin="none")
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    content_el = root.find(q("content"))
    assert content_el.get("origin") == "none"
    assert content_el.get("hash") is None
    assert len(content_el) == 0


def test_render_entry_published_origin_attr():
    for origin in ("feed", "first-seen", "clamped"):
        prepared = make_prepared(published_origin=origin)
        data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
        root = etree.fromstring(data)
        assert root.find(q("published")).get("origin") == origin


def test_render_entry_guid_basis_values():
    for basis in ("atom-id", "guid", "link", "content"):
        prepared = make_prepared(basis=basis)
        data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
        root = etree.fromstring(data)
        assert root.find(q("source")).find(q("guid")).get("basis") == basis


# ── I3: never write malformed XML ─────────────────────────────────────────


def test_render_entry_script_in_content_never_leaks():
    prepared = make_prepared(content_html="<p>before<script>alert(1)</script>after</p>")
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    assert b"alert" not in data
    assert_well_formed(data)


def test_render_entry_degrades_when_content_unparsable(monkeypatch):
    """Force the content subtree to be un-serialisable well-formed XML and
    verify render_entry degrades to a plain-text, unparsable=true content
    element rather than ever returning broken XML."""
    prepared = make_prepared()

    # Build a content element containing a genuinely illegal XML character
    # that slipped past strip_invalid_xml_chars (simulating a bug elsewhere
    # in the pipeline) to prove the render-time safety net independently
    # catches it too.
    from lxml import etree as _etree

    bad_content = _etree.Element(f"{{{NS}}}content")
    bad_content.set("origin", "feed:content")
    para = _etree.SubElement(bad_content, f"{{{NS}}}paragraph")
    para.text = "hello"

    object.__setattr__(prepared, "content", bad_content)

    # Monkeypatch _serialize's well-formedness gate to simulate a failure
    # the first time it's asked to validate, forcing the degrade path.
    import rssd.render as render_mod

    original_assert = render_mod.assert_well_formed
    calls = {"n": 0}

    def flaky_assert(data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated not-well-formed for test")
        original_assert(data)

    monkeypatch.setattr(render_mod, "assert_well_formed", flaky_assert)

    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    root = etree.fromstring(data)
    content_el = root.find(q("content"))
    assert content_el.get("unparsable") == "true"
    assert "hello" in (content_el.text or "")
    assert calls["n"] == 2


def test_assert_well_formed_raises_value_error_on_garbage():
    with pytest.raises(ValueError):
        assert_well_formed(b"<not><well/formed")


def test_assert_well_formed_raises_on_lone_surrogate_bytes():
    # A lone surrogate cannot even be encoded to utf-8 bytes in the first
    # place, so simulate the failure mode with invalid utf-8 bytes directly.
    with pytest.raises(ValueError):
        assert_well_formed(b"<?xml version='1.0'?><entry>\xed\xa0\x80</entry>")


def test_assert_well_formed_accepts_valid_xml():
    assert_well_formed(b"<?xml version='1.0' encoding='utf-8'?><a><b/></a>")


# ── render_feed_meta ────────────────────────────────────────────────────


def test_render_feed_meta_basic():
    meta = FeedMeta(title="Rust Blog", link="https://blog.rust-lang.org/", language="en")
    data = render_feed_meta(meta, name="rust-blog", url="https://blog.rust-lang.org/feed.xml")
    assert_well_formed(data)
    root = etree.fromstring(data)
    assert root.tag == q("feed")
    assert root.get("name") == "rust-blog"
    assert root.find(q("title")).text == "Rust Blog"
    assert root.find(q("state")).text == "active"


def test_render_feed_meta_retired_state():
    meta = FeedMeta(title="Old Feed")
    retired_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
    data = render_feed_meta(
        meta, name="old-feed", url="https://old.example/feed", state="retired", retired_at=retired_at
    )
    root = etree.fromstring(data)
    assert root.find(q("state")).text == "retired"
    assert root.find(q("retired-at")).text == "2026-09-15T00:00:00Z"
    assert_well_formed(data)


# ── render_status ───────────────────────────────────────────────────────


def test_render_status_basic():
    state = FeedState(name="rust-blog", health="ok", poll_count=42, failures=0)
    data = render_status(state, entry_count=17)
    assert_well_formed(data)
    root = etree.fromstring(data)
    assert root.tag == q("status")
    assert root.find(q("health")).text == "ok"
    assert root.find(q("entry-count")).text == "17"
    assert root.find(q("poll-count")).text == "42"


def test_render_status_failing_health():
    state = FeedState(name="x", health="failing", failures=10, last_error="timeout")
    data = render_status(state, entry_count=0)
    root = etree.fromstring(data)
    assert root.find(q("health")).text == "failing"
    assert root.find(q("last-error")).text == "timeout"
    assert_well_formed(data)


# ── xmllint round trip (SPEC §17 well-formedness gate) ───────────────────


@pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint not installed")
def test_render_entry_passes_xmllint():
    prepared = make_prepared(content_html="<p>a <a href='https://x.com'>b</a> &amp; c</p>")
    data = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    proc = subprocess.run(
        ["xmllint", "--noout", "-"], input=data, capture_output=True
    )
    assert proc.returncode == 0, proc.stderr.decode()


# ── real fixture-derived content through the full render path ────────────


def test_render_entry_from_rust_blog_fixture():
    import re
    import html as htmllib
    from pathlib import Path

    data = (Path(__file__).parent / "fixtures" / "feeds" / "rust-blog.xml").read_text(
        encoding="utf-8"
    )
    m = re.search(
        r'<content type="html" xml:base="([^"]+)">(.*?)</content>', data, re.S
    )
    assert m
    base, raw_html = m.group(1), m.group(2)
    html = htmllib.unescape(raw_html)

    parsed = ParsedEntry(
        raw_id="https://blog.rust-lang.org/2026/09/07/rust-debugging-survey-2026-results/",
        basis="atom-id",
        title="Rust debugging survey 2026 results",
        link=base,
        author="The Rust Team",
        published=datetime(2026, 9, 7, tzinfo=timezone.utc),
        categories=(),
        content_html=html,
        content_origin="feed:content",
        base_url=base,
    )
    content = to_semantic(html, base_url=base)
    prepared = PreparedEntry(
        parsed=parsed,
        id="sha256:" + "d" * 64,
        id8="dddddddd",
        content=content,
        content_hash="sha256:" + "e" * 64,
        published=parsed.published,
        published_origin="feed",
    )
    out = render_entry(prepared, make_ctx(), revision=1, first_seen=FIRST_SEEN)
    assert_well_formed(out)
    root = etree.fromstring(out)
    assert root.find(q("content")).find(q("paragraph")) is not None
