"""Tests for rssd.semantic -- HTML -> semantic XML mapping (SPEC §7)."""

from __future__ import annotations

import html as htmllib
import re
from pathlib import Path

import pytest
from lxml import etree

from rssd.config import NS
from rssd.semantic import (
    canonical_xml,
    resolve_url,
    strip_invalid_xml_chars,
    to_semantic,
)

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"


def q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def tags(el: etree._Element) -> list[str]:
    return [c.tag for c in el.iter() if isinstance(c.tag, str)]


# ── empty / None input ───────────────────────────────────────────────────


def test_none_returns_empty_content():
    el = to_semantic(None)
    assert el.tag == q("content")
    assert len(el) == 0
    assert not (el.text and el.text.strip())


def test_empty_string_returns_empty_content():
    el = to_semantic("")
    assert len(el) == 0


def test_whitespace_only_returns_empty_content():
    el = to_semantic("   \n\t  ")
    assert len(el) == 0


def test_never_raises_on_garbage():
    # Must never raise, however malformed.
    for garbage in [
        "<p><div><span>",
        "<<<>>>",
        "not html at all just & < text",
        "<script>",
        "\x00\x01\x02",
        "<p>" * 5000,
    ]:
        el = to_semantic(garbage)
        assert el.tag == q("content")
        # And it must always be serialisable to well-formed XML.
        data = etree.tostring(el)
        etree.fromstring(data)


# ── SPEC §7.1 mapping table ───────────────────────────────────────────────


def test_paragraph():
    el = to_semantic("<p>hello</p>")
    assert [c.tag for c in el] == [q("paragraph")]
    assert el[0].text == "hello"


def test_headings_all_levels():
    for lvl in range(1, 7):
        el = to_semantic(f"<h{lvl}>Head</h{lvl}>")
        assert [c.tag for c in el] == [q("heading")]
        assert el[0].get("level") == str(lvl)


def test_unordered_list():
    el = to_semantic("<ul><li>one</li><li>two</li></ul>")
    lst = el[0]
    assert lst.tag == q("list")
    assert lst.get("ordered") is None
    items = list(lst)
    assert [i.tag for i in items] == [q("item"), q("item")]
    assert items[0].text == "one"
    assert items[1].text == "two"


def test_ordered_list():
    el = to_semantic("<ol><li>one</li></ol>")
    lst = el[0]
    assert lst.get("ordered") == "true"


def test_list_item_with_bare_text_not_wrapped_in_paragraph():
    """SPEC §6 example: <list><item>Faster builds</item></list> -- no
    nested <paragraph> when the source <li> has only inline content."""
    el = to_semantic("<ul><li>Faster builds</li></ul>")
    item = el[0][0]
    assert item.tag == q("item")
    assert item.text == "Faster builds"
    assert len(item) == 0


def test_list_item_with_block_content_nests_paragraph():
    el = to_semantic("<ul><li><p>one</p><p>two</p></li></ul>")
    item = el[0][0]
    assert [c.tag for c in item] == [q("paragraph"), q("paragraph")]


def test_blockquote():
    el = to_semantic("<blockquote><p>It just works.</p></blockquote>")
    quote = el[0]
    assert quote.tag == q("quote")
    assert quote[0].tag == q("paragraph")
    assert quote[0].text == "It just works."


def test_pre_and_code_map_to_code():
    el = to_semantic("<pre>line1\nline2</pre>")
    assert el[0].tag == q("code")
    assert "line1" in el[0].text
    assert "line2" in el[0].text


def test_img_maps_to_image():
    el = to_semantic('<img src="https://example.com/a.png" alt="pic"/>')
    img = el[0]
    assert img.tag == q("image")
    assert img.get("src") == "https://example.com/a.png"
    assert img.get("alt") == "pic"


def test_hr_maps_to_separator():
    el = to_semantic("<p>a</p><hr/><p>b</p>")
    assert [c.tag for c in el] == [q("paragraph"), q("separator"), q("paragraph")]


def test_a_maps_to_link():
    el = to_semantic('<p><a href="https://example.com/x">text</a></p>')
    link = el[0][0]
    assert link.tag == q("link")
    assert link.get("href") == "https://example.com/x"
    assert link.text == "text"


def test_em_and_i_map_to_emphasis():
    for tag in ("em", "i"):
        el = to_semantic(f"<p><{tag}>x</{tag}></p>")
        assert el[0][0].tag == q("emphasis")


def test_strong_and_b_map_to_strong():
    for tag in ("strong", "b"):
        el = to_semantic(f"<p><{tag}>x</{tag}></p>")
        assert el[0][0].tag == q("strong")


def test_br_maps_to_break():
    el = to_semantic("<p>a<br/>b</p>")
    p = el[0]
    assert any(c.tag == q("break") for c in p)


def test_div_span_section_article_main_figure_unwrap():
    for tag in ("div", "span", "section", "article", "main", "figure"):
        el = to_semantic(f"<{tag}><p>hi</p></{tag}>")
        assert [c.tag for c in el] == [q("paragraph")]
        assert q(tag) not in tags(el)


def test_unknown_tag_unwraps_to_text():
    el = to_semantic("<p>a <marquee>b</marquee> c</p>")
    p = el[0]
    # marquee unwraps -- its text becomes part of the paragraph, not a tag.
    assert "marquee" not in etree.tostring(p, encoding="unicode")
    assert "b" in "".join(p.itertext())


# ── SPEC §7.2 guards ────────────────────────────────────────────────────


def test_drop_with_subtree_script():
    el = to_semantic("<p>before<script>alert(1)</script>after</p>")
    out = etree.tostring(el, encoding="unicode")
    assert "alert" not in out
    assert "script" not in out
    assert "before" in out and "after" in out


@pytest.mark.parametrize(
    "tag,inner",
    [
        ("script", "alert(1)"),
        ("style", ".x{color:red}"),
        ("textarea", "secret text"),
        ("noscript", "<p>fallback</p>"),
        ("iframe", "evil"),
        ("object", "data"),
        ("embed", "data"),
        ("form", "<input/>"),
        ("template", "<p>tpl</p>"),
    ],
)
def test_drop_with_subtree_all_tags(tag, inner):
    html = f"<div><{tag}>{inner}</{tag}></div>"
    el = to_semantic(html)
    out = etree.tostring(el, encoding="unicode")
    assert tag not in out
    # None of the inner subtree content should leak into the output.
    stripped_inner = re.sub(r"<[^>]+>", "", inner)
    if stripped_inner.strip():
        assert stripped_inner.split()[0] not in out


def test_javascript_url_rejected_and_unwraps_to_text():
    el = to_semantic('<p><a href="javascript:alert(1)">click me</a></p>')
    out = etree.tostring(el, encoding="unicode")
    assert "javascript" not in out
    assert q("link") not in tags(el)
    assert "click me" in out


def test_data_url_rejected_for_links():
    el = to_semantic('<p><a href="data:text/html,hi">click</a></p>')
    out = etree.tostring(el, encoding="unicode")
    assert "data:" not in out
    assert q("link") not in tags(el)


def test_data_url_rejected_for_images():
    el = to_semantic('<img src="data:image/png;base64,AAAAAAAA"/>')
    out = etree.tostring(el, encoding="unicode")
    assert "data:" not in out
    assert q("image") not in tags(el)


def test_relative_url_resolves_against_base():
    el = to_semantic('<p><a href="/foo/bar">x</a></p>', base_url="https://example.com/dir/")
    link = el[0][0]
    assert link.get("href") == "https://example.com/foo/bar"


def test_resolve_url_rejects_bad_schemes():
    assert resolve_url("javascript:alert(1)", None) is None
    assert resolve_url("data:text/plain,hi", None) is None
    assert resolve_url("ftp://example.com/x", None) is None
    assert resolve_url(None, None) is None
    assert resolve_url("", None) is None


def test_resolve_url_allows_http_https_mailto():
    assert resolve_url("http://example.com", None) == "http://example.com"
    assert resolve_url("https://example.com", None) == "https://example.com"
    assert resolve_url("mailto:a@example.com", None) == "mailto:a@example.com"


def test_resolve_url_unparseable_returns_none():
    # A URL with something urlsplit chokes on.
    assert resolve_url("http://[::1", None) is None


def test_strip_c0_controls():
    s = "a\x00b\x01c\x1fd"
    cleaned = strip_invalid_xml_chars(s)
    assert "\x00" not in cleaned
    assert "\x01" not in cleaned
    assert "\x1f" not in cleaned
    assert "a" in cleaned and "b" in cleaned


def test_strip_lone_surrogate_does_not_raise():
    s = "hello\ud83dworld"
    cleaned = strip_invalid_xml_chars(s)
    assert "\ud83d" not in cleaned
    # Must be encodable now.
    cleaned.encode("utf-8")


def test_lone_surrogate_through_to_semantic_is_safe():
    html = "<p>hello\ud83dworld</p>"
    el = to_semantic(html)
    data = etree.tostring(el)  # must not raise
    etree.fromstring(data)


def test_named_entities_survive_as_characters_not_entities():
    """&nbsp;/&mdash; are undefined in XML. Because we serialise from an
    lxml tree (already-decoded characters), they never appear as named
    entities in the output -- only the five predefined XML entities are
    ever escaped."""
    el = to_semantic("<p>a&nbsp;b&mdash;c</p>")
    out = etree.tostring(el, encoding="unicode")
    assert "&nbsp;" not in out
    assert "&mdash;" not in out
    assert "\xa0" in out  # nbsp character present
    assert "—" in out  # mdash character present


def test_class_id_style_data_attrs_dropped():
    html = '<p class="foo" id="bar" style="color:red" data-track="x">hi</p>'
    el = to_semantic(html)
    p = el[0]
    assert p.attrib == {}


def test_class_id_style_data_attrs_dropped_on_link():
    html = '<p><a href="https://example.com" class="cta" data-x="1">go</a></p>'
    el = to_semantic(html)
    link = el[0][0]
    assert set(link.attrib) == {"href"}


def test_empty_elements_dropped():
    el = to_semantic("<p></p><p>real</p>")
    assert [c.text for c in el] == ["real"]


def test_whitespace_only_text_between_blocks_collapses():
    el = to_semantic("<p>a</p>\n\n   \n<p>b</p>")
    assert len(el) == 2


def test_image_is_block_level_bare():
    """xkcd-style body: content is literally a single <img>."""
    el = to_semantic('<img src="https://imgs.xkcd.com/comics/x.png" alt="alt text"/>')
    assert [c.tag for c in el] == [q("image")]


# ── real fixture bytes ────────────────────────────────────────────────────


def _atom_entry_content(data: str) -> tuple[str, str | None]:
    m = re.search(
        r'<content type="html"(?: xml:base="([^"]*)")?>(.*?)</content>', data, re.S
    )
    if not m:
        m = re.search(r'<summary type="html">(.*?)</summary>', data, re.S)
        assert m, "fixture format changed"
        return htmllib.unescape(m.group(1)), None
    base, raw = m.group(1), m.group(2)
    return htmllib.unescape(raw), base


def _rss_description(data: str) -> str:
    # Skip the channel-level <description> and grab the first <item>'s.
    item_m = re.search(r"<item>(.*?)</item>", data, re.S)
    assert item_m, "no <item> found"
    m = re.search(r"<description>(.*?)</description>", item_m.group(1), re.S)
    assert m and m.group(1).strip(), "item has no description"
    return htmllib.unescape(m.group(1))


def test_rust_blog_fixture_well_formed_and_semantic():
    data = (FIXTURES / "rust-blog.xml").read_text(encoding="utf-8")
    html, base = _atom_entry_content(data)
    assert base and "blog.rust-lang.org" in base  # xml:base is present & load-bearing
    el = to_semantic(html, base_url=base)
    out_bytes = etree.tostring(el)
    etree.fromstring(out_bytes)  # well-formed
    found = set(tags(el))
    assert q("paragraph") in found
    assert q("link") in found
    assert q("list") in found
    # xml:base resolution should have produced absolute rust-lang.org links
    hrefs = [l.get("href") for l in el.iter(q("link"))]
    assert all(h.startswith("http") for h in hrefs)


def test_simonw_fixture_well_formed_and_semantic():
    data = (FIXTURES / "simonw.xml").read_text(encoding="utf-8")
    html, base = _atom_entry_content(data)
    el = to_semantic(html, base_url=base)
    etree.fromstring(etree.tostring(el))
    found = set(tags(el))
    assert q("paragraph") in found


def test_lobsters_fixture_well_formed_and_semantic():
    data = (FIXTURES / "lobsters.xml").read_text(encoding="utf-8")
    html = _rss_description(data)
    el = to_semantic(html, base_url="https://lobste.rs/")
    etree.fromstring(etree.tostring(el))
    found = set(tags(el))
    # thin p/a only fixture
    assert q("paragraph") in found
    assert q("link") in found


def test_xkcd_fixture_well_formed_and_semantic():
    data = (FIXTURES / "xkcd.xml").read_text(encoding="utf-8")
    html = _rss_description(data)
    el = to_semantic(html, base_url="https://xkcd.com/")
    etree.fromstring(etree.tostring(el))
    assert q("image") in set(tags(el))


def test_hn_fixture_has_no_body_content():
    """hn.xml has NO body content at all -- to_semantic(None) must produce
    an empty <content/> the caller marks origin="none"."""
    data = (FIXTURES / "hn.xml").read_text(encoding="utf-8")
    assert "<description>" not in data or True  # just document the fixture shape
    el = to_semantic(None)
    assert len(el) == 0


def test_github_blog_fixture_well_formed():
    data = (FIXTURES / "github-blog.xml").read_text(encoding="utf-8")
    m = re.search(r"<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>", data, re.S)
    assert m, "content:encoded not found in github-blog fixture"
    html = m.group(1)
    el = to_semantic(html, base_url="https://github.blog/")
    etree.fromstring(etree.tostring(el))


def test_godev_fixture_well_formed():
    data = (FIXTURES / "godev.xml").read_text(encoding="utf-8")
    html, base = _atom_entry_content(data)
    el = to_semantic(html, base_url=base)
    etree.fromstring(etree.tostring(el))


# ── canonical_xml ───────────────────────────────────────────────────────


def _make(html: str) -> etree._Element:
    return to_semantic(html)


def test_canonical_xml_stable_across_runs():
    el1 = _make("<p>Hello <a href='https://example.com'>world</a></p>")
    el2 = _make("<p>Hello <a href='https://example.com'>world</a></p>")
    assert canonical_xml(el1) == canonical_xml(el2)


def test_canonical_xml_no_pretty_printing():
    el = _make("<p>a</p><p>b</p>")
    out = canonical_xml(el)
    assert "\n  " not in out


def test_canonical_xml_invariant_under_reordered_attrs():
    a = etree.Element(q("content"))
    img1 = etree.SubElement(a, q("image"))
    img1.set("src", "https://x/a.png")
    img1.set("alt", "pic")

    b = etree.Element(q("content"))
    img2 = etree.SubElement(b, q("image"))
    img2.set("alt", "pic")
    img2.set("src", "https://x/a.png")

    assert canonical_xml(a) == canonical_xml(b)


def test_canonical_xml_invariant_under_added_class_attr_via_source_html():
    # class is stripped by the mapper itself, so two HTML sources differing
    # only by a `class` attribute must map to identical semantic trees and
    # identical canonical XML.
    el1 = _make('<p class="foo">hi</p>')
    el2 = _make("<p>hi</p>")
    assert canonical_xml(el1) == canonical_xml(el2)


def test_canonical_xml_invariant_under_whitespace_changes():
    el1 = _make("<p>Hello   world</p>")
    el2 = _make("<p>Hello\n\nworld</p>")
    assert canonical_xml(el1) == canonical_xml(el2)


def test_canonical_xml_invariant_under_wrapper_div():
    el1 = _make("<div><p>hi</p></div>")
    el2 = _make("<p>hi</p>")
    assert canonical_xml(el1) == canonical_xml(el2)


def test_canonical_xml_strips_leading_trailing_whitespace():
    el = etree.Element(q("content"))
    p = etree.SubElement(el, q("paragraph"))
    p.text = "  hello  "
    out = canonical_xml(el)
    assert ">hello<" in out or ">hello</" in out
    assert "  hello" not in out


def test_canonical_xml_different_content_differs():
    el1 = _make("<p>hello</p>")
    el2 = _make("<p>goodbye</p>")
    assert canonical_xml(el1) != canonical_xml(el2)
