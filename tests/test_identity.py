from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rssd import identity
from rssd.models import ParsedEntry


def make_entry(**kwargs) -> ParsedEntry:
    defaults = dict(raw_id="", basis="link", title="Some Title")
    defaults.update(kwargs)
    return ParsedEntry(**defaults)


# ── canonical_link ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://example.com/a?utm_source=x&utm_campaign=y",
            "https://example.com/a",
        ),
        ("https://example.com/a?fbclid=123", "https://example.com/a"),
        ("https://example.com/a?gclid=123", "https://example.com/a"),
        ("https://example.com/a?ref=hn", "https://example.com/a"),
        ("https://example.com/a?mc_cid=1&mc_eid=2", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        (
            "https://example.com/a?keep=me&utm_source=x",
            "https://example.com/a?keep=me",
        ),
        ("https://example.com/a", "https://example.com/a"),
    ],
)
def test_canonical_link(url, expected):
    assert identity.canonical_link(url) == expected


# ── derive_raw_id precedence ─────────────────────────────────────────────


def test_derive_raw_id_atom_id_wins():
    entry = make_entry(raw_id="tag:example.com,2026:1", basis="atom-id", link="https://x.com/a")
    raw, basis = identity.derive_raw_id(entry)
    assert basis == "atom-id"
    assert raw == "tag:example.com,2026:1"


def test_derive_raw_id_guid_wins_over_link():
    entry = make_entry(raw_id="guid-123", basis="guid", link="https://x.com/a")
    raw, basis = identity.derive_raw_id(entry)
    assert basis == "guid"
    assert raw == "guid-123"


def test_derive_raw_id_falls_back_to_link():
    entry = make_entry(raw_id="", basis="link", link="https://x.com/a?utm_source=z")
    raw, basis = identity.derive_raw_id(entry)
    assert basis == "link"
    assert raw == "https://x.com/a"


def test_derive_raw_id_falls_back_to_content_when_no_guid_or_link():
    entry = make_entry(
        raw_id="",
        basis="link",
        link=None,
        title="A Title",
        published=datetime(2026, 9, 7, tzinfo=timezone.utc),
        content_html="<p>hello world</p>",
    )
    raw, basis = identity.derive_raw_id(entry)
    assert basis == "content"
    assert len(raw) == 64  # hex sha256 digest
    # deterministic
    raw2, basis2 = identity.derive_raw_id(entry)
    assert (raw2, basis2) == (raw, basis)


def test_content_basis_raw_matches_derive_raw_id_fallback():
    import hashlib

    entry = make_entry(raw_id="", basis="link", link=None, title="No Guid No Link")
    raw_via_derive, basis = identity.derive_raw_id(entry)
    assert basis == "content"

    basis_raw = identity.content_basis_raw(entry)
    raw_via_helper = hashlib.sha256(basis_raw.encode("utf-8")).hexdigest()
    assert raw_via_helper == raw_via_derive

    id_via_derive = identity.entry_id("feedname", basis, raw_via_derive)
    id_via_helper = identity.entry_id("feedname", "content", raw_via_helper)
    assert id_via_derive == id_via_helper


# ── entry_id / id8 ────────────────────────────────────────────────────────


def test_entry_id_format():
    result = identity.entry_id("rust-blog", "atom-id", "https://example.com/1")
    assert result.startswith("sha256:")
    assert len(result) == len("sha256:") + 64


def test_entry_id_namespaced_by_feed():
    a = identity.entry_id("feed-a", "guid", "same-raw")
    b = identity.entry_id("feed-b", "guid", "same-raw")
    assert a != b


def test_entry_id_deterministic():
    a = identity.entry_id("feed", "guid", "raw")
    b = identity.entry_id("feed", "guid", "raw")
    assert a == b


def test_id8_is_first_8_hex_chars():
    full = identity.entry_id("feed", "guid", "raw")
    short = identity.id8(full)
    assert short == full[len("sha256:") : len("sha256:") + 8]
    assert len(short) == 8


# ── content_hash ────────────────────────────────────────────────────────


def test_content_hash_format_and_determinism():
    h1 = identity.content_hash("Title", "Author", "<content/>")
    h2 = identity.content_hash("Title", "Author", "<content/>")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_content_hash_changes_with_inputs():
    base = identity.content_hash("Title", "Author", "<content/>")
    assert base != identity.content_hash("Title2", "Author", "<content/>")
    assert base != identity.content_hash("Title", "Author2", "<content/>")
    assert base != identity.content_hash("Title", "Author", "<content>x</content>")


def test_content_hash_handles_none_author():
    # Must not raise, and must differ from an empty-string author collision
    # in a degenerate way -- just check it's stable and well-formed.
    h = identity.content_hash("Title", None, "<content/>")
    assert h.startswith("sha256:")


# ── slugify ─────────────────────────────────────────────────────────────


def test_slugify_basic():
    assert identity.slugify("Rust debugging survey 2026 results") == (
        "rust-debugging-survey-2026-results"
    )


def test_slugify_empty_is_untitled():
    assert identity.slugify("") == "untitled"
    assert identity.slugify("   ") == "untitled"
    assert identity.slugify("!!!") == "untitled"


def test_slugify_truncates_on_word_boundary():
    title = "one two three four five six seven eight nine ten eleven twelve"
    slug = identity.slugify(title, max_len=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")
    # every char group in slug should be a whole word from title
    words = title.split()
    for part in slug.split("-"):
        assert part in words


def test_slugify_cjk_and_emoji_is_filename_safe():
    title = "日本語のタイトル \U0001f600 with emoji"
    slug = identity.slugify(title)
    assert slug  # never empty for non-empty title (falls back to untitled)
    assert all(c.isalnum() or c == "-" for c in slug)
    assert "/" not in slug and "\x00" not in slug


def test_slugify_very_long_title():
    title = "word " * 200  # 1000 chars
    slug = identity.slugify(title, max_len=48)
    assert len(slug) <= 48
    assert slug  # non-empty


def test_slugify_400_char_title_is_filename_safe():
    title = "x" * 400
    slug = identity.slugify(title, max_len=48)
    assert len(slug) <= 48
    assert re_all_safe(slug)


def re_all_safe(s: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z0-9-]*", s))


# ── entry_base_name / parse_entry_filename round-trip ────────────────────


def test_entry_base_name():
    assert identity.entry_base_name("20260907T000000Z", "9f2c1a3b", "my-slug") == (
        "20260907T000000Z-9f2c1a3b-my-slug"
    )


@pytest.mark.parametrize(
    "stamp,id8_,slug,revision",
    [
        ("20260907T000000Z", "9f2c1a3b", "rust-debugging-survey", 0),
        ("20260907T000000Z", "9f2c1a3b", "rust-debugging-survey", 1),
        ("20260907T000000Z", "9f2c1a3b", "rust-debugging-survey", 42),
        ("20260907T000000Z", "deadbeef", "untitled", 0),
        ("20260907T000000Z", "deadbeef", "a", 3),
    ],
)
def test_parse_entry_filename_roundtrip(stamp, id8_, slug, revision):
    base = identity.entry_base_name(stamp, id8_, slug)
    filename = f"{base}.xml" if revision == 0 else f"{base}.r{revision}.xml"
    result = identity.parse_entry_filename(filename)
    assert result == (stamp, id8_, slug, revision)


def test_parse_entry_filename_garbage_returns_none():
    assert identity.parse_entry_filename("not-a-valid-filename.xml") is None
    assert identity.parse_entry_filename("20260907T000000Z-badid8-slug.xml") is None
    assert identity.parse_entry_filename("random.txt") is None
    assert identity.parse_entry_filename("") is None
    assert identity.parse_entry_filename("20260907T000000Z-9f2c1a3b-.xml") is None


def test_parse_entry_filename_never_raises_on_weird_input():
    weird_inputs = [
        "../../etc/passwd",
        "20260907T000000Z-9f2c1a3b-slug.rX.xml",
        "20260907T000000Z-9f2c1a3b-slug.r-1.xml",
        ".rssd-tmp-abc123",
        "20260907T000000Z-9F2C1A3B-slug.xml",  # uppercase hex should not match
    ]
    for filename in weird_inputs:
        # must not raise
        identity.parse_entry_filename(filename)
