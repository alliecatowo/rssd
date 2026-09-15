"""HTML -> semantic XML tree (SPEC §7). PURE.

The mapper is the sanitiser: because the transform is an allowlist by
construction (SPEC §7.2), no separate sanitiser pass (nh3/bleach) is needed.
Only a handful of things need explicit guarding on top of the allowlist:
drop-with-subtree tags, URL scheme filtering, and illegal XML codepoints.
"""

from __future__ import annotations

import re
import urllib.parse

import lxml.html
from lxml import etree

from rssd.config import NS

# Tags that must be dropped along with their entire subtree. Unwrapping these
# instead of dropping them leaks script/style bodies as visible text -- the
# classic sanitiser footgun (SPEC §7.2).
DROP_WITH_SUBTREE = {
    "script",
    "style",
    "textarea",
    "noscript",
    "iframe",
    "object",
    "embed",
    "form",
    "template",
}

# Tags that hoist their children up into the parent, discarding the wrapper.
UNWRAP_TAGS = {"div", "span", "section", "article", "main", "figure"}

# Direct HTML tag -> rssd semantic tag mapping (SPEC §7.1), for tags that map
# 1:1 with no extra attribute handling beyond what's done generically below.
BLOCK_MAP = {
    "p": "paragraph",
    "blockquote": "quote",
    "pre": "code",
    "hr": "separator",
}
HEADING_RE = re.compile(r"^h([1-6])$")

INLINE_MAP = {
    "em": "emphasis",
    "i": "emphasis",
    "strong": "strong",
    "b": "strong",
    "br": "break",
}

ALLOWED_SCHEMES = {"http", "https", "mailto"}

# XML 1.0 legal codepoint ranges (SPEC §7.2). Everything else -- C0 controls
# (other than tab/lf/cr), lone surrogates, and a few noncharacters -- must be
# stripped before serialising or a strict parser will reject the output.
_XML_ILLEGAL_RE = re.compile(
    "[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def strip_invalid_xml_chars(s: str) -> str:
    """Remove C0 controls and lone surrogates that would make serialised XML
    invalid. Never raises."""
    if s is None:
        return s
    return _XML_ILLEGAL_RE.sub("", s)


def resolve_url(url: str | None, base: str | None) -> str | None:
    """Resolve ``url`` against ``base`` and return it only if the resulting
    scheme is http/https/mailto. Returns None for anything else, including
    unparseable input -- callers must then unwrap the element to plain text.
    """
    if url is None:
        return None
    url = url.strip()
    if not url:
        return None
    try:
        if base:
            resolved = urllib.parse.urljoin(base, url)
        else:
            resolved = url
        parsed = urllib.parse.urlsplit(resolved)
    except (ValueError, UnicodeError):
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return None
    return resolved


def _new_el(tag: str, **attrs: str) -> etree._Element:
    el = etree.Element(f"{{{NS}}}{tag}")
    for k, v in attrs.items():
        if v is not None:
            el.set(k, v)
    return el


# ASCII whitespace only -- deliberately NOT `\s`, which in a Unicode pattern
# also matches \xa0 (nbsp) and other Unicode space separators. Collapsing
# those would destroy the very distinction &nbsp; exists to preserve.
_ASCII_WS_RE = re.compile(r"[ \t\n\r\f\v]+")


def _collapse_ws(text: str | None) -> str:
    if not text:
        return ""
    return _ASCII_WS_RE.sub(" ", text)


def _is_blank(text: str | None) -> bool:
    return not text or not text.strip()


def _append_text(parent: etree._Element, text: str | None) -> None:
    """Append text to the current tail-most position of parent's content.

    Whitespace runs (including source newlines/indentation) are collapsed to
    a single space, matching HTML rendering semantics and SPEC §7.2's
    "whitespace-only text ... collapses" rule.
    """
    if not text:
        return
    text = _collapse_ws(strip_invalid_xml_chars(text))
    if not text:
        return
    if len(parent):
        last = parent[-1]
        last.tail = (last.tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _walk_inline(node, parent: etree._Element, base_url: str | None) -> None:
    """Walk an lxml.html node, mapping to inline semantic elements, appending
    results into ``parent`` (which is already a block-level semantic element
    such as <paragraph> or <heading>)."""
    _append_text(parent, node.text)

    for child in node:
        tag = child.tag if isinstance(child.tag, str) else None
        if tag is None:
            # comment/PI node -- skip subtree but keep tail text
            _append_text(parent, child.tail)
            continue
        tag = tag.lower()

        if tag in DROP_WITH_SUBTREE:
            # dropped entirely, subtree and all; tail text still flows
            _append_text(parent, child.tail)
            continue

        if tag == "a":
            href = resolve_url(child.get("href"), base_url)
            if href is not None:
                link_el = _new_el("link", href=href)
                _walk_inline(child, link_el, base_url)
                if link_el.text or len(link_el):
                    parent.append(link_el)
                else:
                    # empty link -- drop
                    pass
            else:
                # unwrap to text: hoist children as plain text/inline
                _walk_inline(child, parent, base_url)
            _append_text(parent, child.tail)
            continue

        if tag == "img":
            src = resolve_url(child.get("src"), base_url)
            if src is not None:
                alt = child.get("alt")
                img_el = _new_el("image", src=src, alt=alt)
                parent.append(img_el)
            # else: dropped (unwrap to text == nothing, img has no text)
            _append_text(parent, child.tail)
            continue

        if tag in INLINE_MAP:
            sem_tag = INLINE_MAP[tag]
            if sem_tag == "break":
                parent.append(_new_el("break"))
            else:
                inline_el = _new_el(sem_tag)
                _walk_inline(child, inline_el, base_url)
                if inline_el.text or len(inline_el):
                    parent.append(inline_el)
                else:
                    pass
            _append_text(parent, child.tail)
            continue

        if tag == "code":
            code_el = _new_el("code")
            _walk_inline(child, code_el, base_url)
            if code_el.text or len(code_el):
                parent.append(code_el)
            _append_text(parent, child.tail)
            continue

        # Anything else at inline level (unwrap tags, unknown tags, nested
        # blocks bleeding into inline context) -- unwrap to text/children.
        _walk_inline(child, parent, base_url)
        _append_text(parent, child.tail)


def _has_block_descendant(node) -> bool:
    """True if any descendant (through wrapper tags) is a block-level
    element. Used to decide whether an <li> should hold its content as
    plain inline text (the common case: ``<li>Faster builds</li>``) or be
    walked as a nested block container (``<li><p>...</p><ul>...</ul></li>``).
    """
    for child in node.iterdescendants():
        tag = child.tag
        if not isinstance(tag, str):
            continue
        tag = tag.lower()
        if tag in BLOCK_MAP or HEADING_RE.match(tag) or tag in ("ul", "ol", "img"):
            return True
    return False


def _walk_block(node, out: etree._Element, base_url: str | None) -> None:
    """Walk an lxml.html node at block level, appending block-level semantic
    elements (paragraph, heading, list, quote, code, image, separator) into
    ``out``. Stray inline/text content at block level is coalesced into an
    implicit <paragraph>."""
    pending_inline_text = []

    def flush_pending(pending_el: etree._Element | None) -> etree._Element | None:
        if pending_el is not None:
            if pending_el.text or len(pending_el):
                out.append(pending_el)
        return None

    pending_para = None

    def get_para() -> etree._Element:
        nonlocal pending_para
        if pending_para is None:
            pending_para = _new_el("paragraph")
        return pending_para

    def close_para():
        nonlocal pending_para
        if pending_para is not None and (pending_para.text or len(pending_para)):
            out.append(pending_para)
        pending_para = None

    # handle leading text at this level
    if node.text and not _is_blank(node.text):
        _append_text(get_para(), _collapse_ws(node.text))

    for child in node:
        tag = child.tag if isinstance(child.tag, str) else None
        if tag is None:
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue
        tag = tag.lower()

        if tag in DROP_WITH_SUBTREE:
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue

        heading_m = HEADING_RE.match(tag)

        if tag in BLOCK_MAP or heading_m:
            close_para()
            if heading_m:
                level = heading_m.group(1)
                el = _new_el("heading", level=level)
                _walk_inline(child, el, base_url)
            elif tag == "pre":
                el = _new_el("code")
                el.text = strip_invalid_xml_chars(child.text_content())
            elif tag == "blockquote":
                el = _new_el("quote")
                _walk_block(child, el, base_url)
            elif tag == "hr":
                el = _new_el("separator")
            else:  # p
                el = _new_el(BLOCK_MAP[tag])
                _walk_inline(child, el, base_url)
            if tag == "hr" or el.text or len(el) or el.tag.endswith("separator"):
                if tag == "hr":
                    out.append(el)
                elif el.text or len(el):
                    out.append(el)
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue

        if tag in ("ul", "ol"):
            close_para()
            list_el = _new_el("list")
            if tag == "ol":
                list_el.set("ordered", "true")
            for li in child:
                li_tag = li.tag if isinstance(li.tag, str) else None
                if li_tag is None or li_tag.lower() != "li":
                    continue
                item_el = _new_el("item")
                if _has_block_descendant(li):
                    _walk_block(li, item_el, base_url)
                else:
                    _walk_inline(li, item_el, base_url)
                if item_el.text or len(item_el):
                    list_el.append(item_el)
            if len(list_el):
                out.append(list_el)
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue

        if tag == "code":
            # bare <code> at block level -> code block
            close_para()
            el = _new_el("code")
            el.text = strip_invalid_xml_chars(child.text_content())
            if el.text:
                out.append(el)
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue

        if tag == "img":
            close_para()
            src = resolve_url(child.get("src"), base_url)
            if src is not None:
                alt = child.get("alt")
                out.append(_new_el("image", src=src, alt=alt))
            if child.tail and not _is_blank(child.tail):
                _append_text(get_para(), _collapse_ws(child.tail))
            continue

        if tag in ("a", "em", "i", "strong", "b", "br") or tag not in UNWRAP_TAGS:
            # inline-level or unknown tag encountered at block level:
            # fold into the current paragraph via inline walking.
            para = get_para()
            _walk_inline(child, para, base_url)
            continue

        # unwrap tags (div/span/section/article/main/figure): recurse as
        # block-level, hoisting children directly into `out`.
        close_para()
        _walk_block(child, out, base_url)
        if child.tail and not _is_blank(child.tail):
            _append_text(get_para(), _collapse_ws(child.tail))

    close_para()


def to_semantic(html: str | None, base_url: str | None = None) -> etree._Element:
    """Map raw HTML to the rssd semantic vocabulary (SPEC §7). Returns a
    ``<content>`` element. Never raises -- malformed input degrades to an
    empty or partial tree rather than propagating an exception."""
    content = _new_el("content")
    if html is None or not html.strip():
        return content

    try:
        html = strip_invalid_xml_chars(html)
        # lxml.html is the forgiving parser -- it will accept fragments,
        # unclosed tags, bare text, etc.
        fragment = lxml.html.fragment_fromstring(
            html, create_parent="div"
        )
    except Exception:
        try:
            fragment = lxml.html.fromstring(f"<div>{html}</div>")
        except Exception:
            # Total parse failure: degrade to a single paragraph of the raw
            # text with tags stripped as a last resort.
            text = re.sub(r"<[^>]+>", " ", html)
            text = _collapse_ws(text).strip()
            if text:
                p = _new_el("paragraph")
                p.text = strip_invalid_xml_chars(text)
                content.append(p)
            return content

    try:
        _walk_block(fragment, content, base_url)
    except Exception:
        # Never let a mapping bug propagate -- degrade to plain text.
        content.clear()
        try:
            text = _collapse_ws(fragment.text_content()).strip()
        except Exception:
            text = ""
        if text:
            p = _new_el("paragraph")
            p.text = strip_invalid_xml_chars(text)
            content.append(p)

    return content


# ── canonical serialisation ──────────────────────────────────────────────

# Deliberately ASCII-only, same reasoning as _ASCII_WS_RE above: collapsing
# \xa0 would erase the &nbsp; vs plain-space distinction content hashing
# should not care about, but the *character* must survive.
_CANON_WS_RE = _ASCII_WS_RE


def _canon_text(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = _CANON_WS_RE.sub(" ", text)
    return collapsed


def _canon_element(el: etree._Element, *, strip_leading: bool, strip_trailing: bool) -> etree._Element:
    """Build a deep copy with sorted attributes and collapsed whitespace,
    suitable for deterministic serialisation.

    ``strip_leading``/``strip_trailing`` are true only at the true start/end
    of the document's text flow (this element's own leading text, or the
    tail of its last descendant) so that meaningful single spaces between
    inline siblings (e.g. ``text <link>x</link> more text``) are preserved
    while cosmetic leading/trailing whitespace is not.
    """
    new = etree.Element(el.tag)
    for k in sorted(el.attrib):
        new.set(k, el.attrib[k])

    text = _canon_text(el.text)
    if text:
        if strip_leading:
            text = text.lstrip(" ")
        if strip_trailing and len(el) == 0:
            text = text.rstrip(" ")
    new.text = text if text else None

    # Leading-strip responsibility transfers to the first child only when
    # this element had no text of its own before it (otherwise that text
    # already absorbed the leading strip above).
    leading_passthrough = strip_leading and not (el.text and el.text.strip(" "))

    children = list(el)
    for i, child in enumerate(children):
        is_first = i == 0
        is_last = i == len(children) - 1
        new_child = _canon_element(
            child,
            strip_leading=leading_passthrough and is_first,
            strip_trailing=strip_trailing and is_last,
        )
        new.append(new_child)
        tail = _canon_text(child.tail)
        if tail and strip_trailing and is_last:
            tail = tail.rstrip(" ")
        new_child.tail = tail if tail else None
    return new


def canonical_xml(el: etree._Element) -> str:
    """Deterministic serialisation used for content hashing (SPEC §9.2).

    No pretty-printing, attributes sorted, whitespace runs collapsed,
    leading/trailing text stripped. Two semantically identical trees
    (differing only in attribute order, added class attrs already stripped
    by the mapper, cosmetic whitespace, or a wrapper element) must produce
    identical output -- this is the primary defence against revision churn.

    We serialise from the lxml tree (not by string-munging raw HTML) so that
    named HTML entities such as &nbsp;/&mdash; -- already decoded to real
    characters by the parser -- come out as those characters, not as
    undefined-in-XML named entities. Only the five predefined XML entities
    are ever (re-)escaped by lxml's serialiser.
    """
    canon = _canon_element(el, strip_leading=True, strip_trailing=True)
    data = etree.tostring(canon, encoding="unicode", method="xml")
    return data
