"""Stable entry IDs, content hashing, and filename slugs. PURE.

See SPEC §9.1 (identity), §9.2 (content hash), §4.2 (filename/slug rules).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from rssd.models import IdBasis, ParsedEntry

#: Tracking query params stripped from canonicalised links (SPEC §9.1).
_STRIP_PARAMS_PREFIXES = ("utm_",)
_STRIP_PARAMS_EXACT = {"fbclid", "gclid", "ref", "mc_cid", "mc_eid"}


def canonical_link(url: str) -> str:
    """Strip tracking query params and the fragment from a URL."""
    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_STRIP_PARAMS_PREFIXES)
        and k.lower() not in _STRIP_PARAMS_EXACT
    ]
    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))


def content_basis_raw(entry: ParsedEntry) -> str:
    """The content-derived identity input: ``title + NUL + published + NUL +
    first 512 chars of text`` -- exactly what gets hashed for the SPEC §9.1
    #4 fallback branch of ``derive_raw_id``.

    Public because the daemon needs to recompute this independently once
    rotating-guid detection pins a feed to content-based identity (SPEC
    §9.4), even for entries that *do* have a guid -- see ``derive_raw_id``.
    """
    text = entry.content_html or ""
    published = entry.published.isoformat() if entry.published else ""
    return f"{entry.title}\0{published}\0{text[:512]}"


def derive_raw_id(entry: ParsedEntry) -> tuple[str, IdBasis]:
    """SPEC §9.1 precedence: atom-id -> guid -> canonicalised link -> content hash.

    ``ParsedEntry`` doesn't distinguish "raw_id came from an atom <id>" vs
    "came from an rss <guid>" at the type level -- parse.py is expected to
    have already set ``entry.raw_id``/``entry.basis`` to whichever of those two
    it found (feedparser exposes both through the same ``id`` field). This
    function trusts that upstream decision when present, and only falls
    through to link/content when the parser found neither.
    """
    if entry.raw_id and entry.basis in ("atom-id", "guid"):
        return entry.raw_id, entry.basis
    if entry.link:
        return canonical_link(entry.link), "link"
    # Fallback: content basis, per SPEC §9.1 #4.
    raw = hashlib.sha256(content_basis_raw(entry).encode("utf-8")).hexdigest()
    return raw, "content"


def entry_id(feed_name: str, basis: IdBasis, raw: str) -> str:
    """"sha256:" + sha256(feed_name + NUL + basis + NUL + raw)."""
    digest = hashlib.sha256(f"{feed_name}\0{basis}\0{raw}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def id8(entry_id_value: str) -> str:
    """First 8 hex chars of the entry ID (after the "sha256:" prefix)."""
    _, _, hexpart = entry_id_value.partition(":")
    hexpart = hexpart or entry_id_value
    return hexpart[:8]


def content_hash(title: str, author: str | None, canonical_content: str) -> str:
    """"sha256:" + sha256(title + NUL + author + NUL + canonical_content_xml).

    SPEC §9.2: computed over the rendered semantic XML, never raw HTML. This
    function takes the already-canonicalised content string -- it does not
    know or care how that string was produced.
    """
    author_part = author or ""
    digest = hashlib.sha256(
        f"{title}\0{author_part}\0{canonical_content}".encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


_WORD_RE = re.compile(r"[a-z0-9]+")


def slugify(title: str, max_len: int = 48) -> str:
    """NFKD-normalise, lowercase, collapse non-alphanumerics to "-", truncate
    at max_len on a word boundary. Empty -> "untitled"."""
    if not title:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", title)
    # Drop combining marks (accents) left behind by NFKD decomposition.
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    ascii_ish = stripped.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_ish.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not collapsed:
        return "untitled"
    if len(collapsed) <= max_len:
        return collapsed
    truncated = collapsed[:max_len]
    # Truncate on a word boundary: back off to the last "-" if mid-word.
    if "-" in truncated:
        last_dash = truncated.rfind("-")
        # Only back off if it doesn't throw away almost everything.
        if last_dash > 0:
            truncated = truncated[:last_dash]
    truncated = truncated.strip("-")
    return truncated or "untitled"


def entry_base_name(stamp: str, id8_: str, slug: str) -> str:
    """"{stamp}-{id8}-{slug}", the filename minus the ``.rN.xml`` suffix."""
    return f"{stamp}-{id8_}-{slug}"


_FILENAME_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<id8>[0-9a-f]{8})-(?P<slug>.+?)"
    r"(?:\.r(?P<rev>\d+))?\.xml$"
)


def parse_entry_filename(filename: str) -> tuple[str, str, str, int] | None:
    """Inverse of entry_base_name() + the ".rN.xml"/".xml" suffix.

    Returns (stamp, id8, slug, revision), where revision 0 means the bare
    symlink name (no ".rN"). Returns None if the filename doesn't match the
    expected shape. Total -- never raises.
    """
    match = _FILENAME_RE.match(filename)
    if not match:
        return None
    stamp = match.group("stamp")
    id8_ = match.group("id8")
    slug = match.group("slug")
    rev = match.group("rev")
    revision = int(rev) if rev is not None else 0
    return stamp, id8_, slug, revision
