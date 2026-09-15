from __future__ import annotations

from pathlib import Path

import pytest

from rssd import parse

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"

ALL_FIXTURES = [
    "rust-blog.xml",
    "github-blog.xml",
    "simonw.xml",
    "lobsters.xml",
    "hn.xml",
    "xkcd.xml",
    "godev.xml",
]

EXPECTED_COUNTS = {
    "rust-blog.xml": 10,
    "github-blog.xml": 10,
    "simonw.xml": 30,
    "lobsters.xml": 25,
    "hn.xml": 20,
    "xkcd.xml": 4,
    "godev.xml": 10,
}


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ── sniff_charset / decode_body ──────────────────────────────────────────


def test_sniff_charset_from_xml_declaration():
    body = b"<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?><root/>"
    assert parse.sniff_charset(body, None).lower() == "iso-8859-1"


def test_sniff_charset_from_content_type_header():
    body = b"<root/>"
    assert parse.sniff_charset(body, "text/xml; charset=windows-1252").lower() == (
        "windows-1252"
    )


def test_sniff_charset_defaults_to_utf8():
    body = b"<root/>"
    assert parse.sniff_charset(body, None) == "utf-8"


def test_sniff_charset_xml_declaration_wins_over_content_type():
    body = b"<?xml version=\"1.0\" encoding=\"latin-1\"?><root/>"
    assert parse.sniff_charset(body, "text/xml; charset=utf-8").lower() == "latin-1"


def test_decode_body_latin1_declared():
    # Build a latin-1 document with a high byte (e.g. 0xE9 = "é" in latin-1)
    # that would mojibake or fail under a naive UTF-8 decode.
    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
        "<rss><channel><item><title>Caf\xe9</title></item></channel></rss>"
    )
    body = xml.encode("iso-8859-1")

    # Sanity check: naive UTF-8 decoding of this body must fail or mangle it.
    with pytest.raises(UnicodeDecodeError):
        body.decode("utf-8")

    decoded = parse.decode_body(body, None)
    assert "Café" in decoded


def test_decode_body_utf8_default():
    xml = "<rss><channel><item><title>héllo</title></item></channel></rss>"
    body = xml.encode("utf-8")
    decoded = parse.decode_body(body, None)
    assert "héllo" in decoded


# ── parse_feed over real fixtures ────────────────────────────────────────


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_entry_counts(filename):
    result = parse.parse_feed(load(filename), "application/xml", f"https://example.com/{filename}")
    assert len(result.entries) == EXPECTED_COUNTS[filename]


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_never_aborts_records_bozo_instead(filename):
    result = parse.parse_feed(load(filename), "application/xml", "https://example.com/feed")
    assert isinstance(result.bozo, bool)


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_every_entry_has_raw_id_and_plausible_basis(filename):
    result = parse.parse_feed(load(filename), "application/xml", "https://example.com/feed")
    for entry in result.entries:
        assert entry.basis in ("atom-id", "guid", "link", "content")
        if entry.basis in ("atom-id", "guid"):
            assert entry.raw_id  # non-empty when parse.py resolved it directly
        # every entry should be resolvable to a non-empty raw id via identity.py
        from rssd.identity import derive_raw_id

        raw, basis = derive_raw_id(entry)
        assert raw
        assert basis in ("atom-id", "guid", "link", "content")


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_dates_parse_to_aware_datetimes(filename):
    result = parse.parse_feed(load(filename), "application/xml", "https://example.com/feed")
    for entry in result.entries:
        if entry.published is not None:
            assert entry.published.tzinfo is not None
        if entry.updated is not None:
            assert entry.updated.tzinfo is not None


def test_hn_body_is_metadata_not_article_prose():
    """hnrss items carry a <description> (Article URL / Comments URL /
    Points / # Comments), so per SPEC content precedence this correctly
    resolves as feed:summary -- it just isn't the article's own text. That's
    exactly why hn is the reference feed that would opt into
    <fulltext>true</fulltext>."""
    result = parse.parse_feed(load("hn.xml"), "application/rss+xml", "https://hnrss.org/frontpage")
    assert len(result.entries) == 20
    for entry in result.entries:
        assert entry.content_origin == "feed:summary"
        assert entry.content_html is not None
        # every hn item's description carries this hnrss-synthesized footer,
        # even Show HN self-posts that also include real prose above it.
        assert "Points:" in entry.content_html
        assert "# Comments:" in entry.content_html


def test_no_description_or_content_yields_none():
    """Synthetic minimal RSS: title + link only, no description/content at
    all -- none of the six real reference feeds exercise this branch, so it
    needs an inline fixture."""
    xml = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title><link>https://example.com</link>
<item><title>No body here</title><link>https://example.com/a</link>
<guid>https://example.com/a</guid></item>
</channel></rss>"""
    result = parse.parse_feed(xml, "application/rss+xml", "https://example.com/rss")
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.content_origin == "none"
    assert entry.content_html is None


def test_rust_blog_has_feed_content():
    result = parse.parse_feed(
        load("rust-blog.xml"), "application/atom+xml", "https://blog.rust-lang.org/feed.xml"
    )
    assert len(result.entries) == 10
    for entry in result.entries:
        assert entry.content_origin == "feed:content"
        assert entry.content_html


def test_rust_blog_entries_have_base_url():
    result = parse.parse_feed(
        load("rust-blog.xml"), "application/atom+xml", "https://blog.rust-lang.org/feed.xml"
    )
    # rust-lang sets xml:base per entry -- load-bearing for relative URLs.
    assert any(entry.base_url for entry in result.entries)


def test_github_blog_content_encoded():
    result = parse.parse_feed(
        load("github-blog.xml"), "application/rss+xml", "https://github.blog/feed/"
    )
    for entry in result.entries:
        assert entry.content_origin == "feed:content"
        assert entry.content_html


def test_xkcd_image_only_body_is_still_content():
    result = parse.parse_feed(load("xkcd.xml"), "application/rss+xml", "https://xkcd.com/rss.xml")
    assert len(result.entries) == 4
    for entry in result.entries:
        assert entry.content_origin != "none"
        assert "<img" in (entry.content_html or "")


def test_lobsters_ttl_seconds():
    result = parse.parse_feed(load("lobsters.xml"), "application/rss+xml", "https://lobste.rs/rss")
    # lobsters.xml declares <ttl>120</ttl> (minutes) -> 7200 seconds.
    assert result.meta.ttl_seconds == 7200


def test_github_blog_sy_update_frequency():
    result = parse.parse_feed(
        load("github-blog.xml"), "application/rss+xml", "https://github.blog/feed/"
    )
    # <sy:updatePeriod>hourly</sy:updatePeriod><sy:updateFrequency>1</sy:updateFrequency>
    assert result.meta.ttl_seconds == 3600


def test_feed_meta_populated():
    result = parse.parse_feed(
        load("rust-blog.xml"), "application/atom+xml", "https://blog.rust-lang.org/feed.xml"
    )
    assert result.meta.title == "Rust Blog"
    assert result.meta.link


@pytest.mark.parametrize("filename", ALL_FIXTURES)
def test_no_fabricated_body_when_absent(filename):
    """An entry with content_origin == "none" must never carry content_html."""
    result = parse.parse_feed(load(filename), "application/xml", "https://example.com/feed")
    for entry in result.entries:
        if entry.content_origin == "none":
            assert entry.content_html is None
