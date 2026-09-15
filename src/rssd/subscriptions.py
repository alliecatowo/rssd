"""Parse feeds.d/*.xml. PURE.

See SPEC §5 (subscription file format, liberal URL extraction) and §4.1
(feed folder naming).
"""

from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

from rssd.config import valid_name
from rssd.models import Subscription
from rssd.timeutil import parse_duration


class SubscriptionError(Exception):
    """A subscription file failed to parse into a usable Subscription."""


_URL_XPATHS = (
    "/subscription/url",
    "/subscription/link",
    "//outline/@xmlUrl",
    "//link[@rel='self']/@href",
)

_URL_IN_TEXT_RE = re.compile(r"https?://\S+")


def _text_of(node) -> str | None:
    if isinstance(node, str):
        text = node
    else:
        text = node.text
    if text is None:
        return None
    text = text.strip()
    return text or None


def _first_http_text(root) -> str | None:
    """Fallback: the first http(s):// URL found in any text node (or, failing
    that, any attribute value) anywhere in the document, document order."""
    for node in root.iter():
        if isinstance(node.text, str):
            match = _URL_IN_TEXT_RE.search(node.text)
            if match:
                return match.group(0).rstrip(").,;\"'")
    for node in root.iter():
        for attr_value in node.attrib.values():
            match = _URL_IN_TEXT_RE.search(attr_value)
            if match:
                return match.group(0).rstrip(").,;\"'")
    return None


def _extract_url(root) -> str | None:
    for xpath in _URL_XPATHS:
        try:
            result = root.xpath(xpath)
        except etree.XPathEvalError:
            continue
        for node in result:
            text = _text_of(node)
            if text:
                return text
    return _first_http_text(root)


def _extract_name(root, source_path: Path) -> str:
    default = source_path.stem
    name_nodes = root.xpath("/subscription/name")
    if name_nodes:
        text = _text_of(name_nodes[0])
        if text:
            if not valid_name(text):
                raise SubscriptionError(
                    f"invalid <name> {text!r}: must match ^[a-z0-9][a-z0-9._-]{{0,63}}$"
                )
            return text
    if not valid_name(default):
        raise SubscriptionError(
            f"invalid filename stem {default!r} as a feed name: "
            "must match ^[a-z0-9][a-z0-9._-]{{0,63}}$ or supply <name>"
        )
    return default


def _extract_interval(root) -> int | None:
    nodes = root.xpath("/subscription/interval")
    if not nodes:
        return None
    text = _text_of(nodes[0])
    if not text:
        return None
    seconds = parse_duration(text)
    if seconds is None:
        raise SubscriptionError(f"invalid <interval> {text!r}")
    return seconds


_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _extract_fulltext(root) -> bool:
    nodes = root.xpath("/subscription/fulltext")
    if not nodes:
        return False
    text = _text_of(nodes[0])
    if not text:
        return False
    lowered = text.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise SubscriptionError(f"invalid <fulltext> {text!r}")


def parse_subscription(data: bytes, source_path: Path) -> Subscription:
    """Parse one subscription file. Raises SubscriptionError on anything
    that leaves the subscription unusable -- unparsable XML, no extractable
    URL, or an invalid name."""
    try:
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise SubscriptionError(f"XML syntax error: {exc}") from exc

    url = _extract_url(root)
    if not url:
        raise SubscriptionError("no URL found (checked url/link/outline/self-link/text)")

    name = _extract_name(root, source_path)
    interval = _extract_interval(root)
    fulltext = _extract_fulltext(root)

    return Subscription(
        name=name,
        url=url,
        source_path=source_path,
        interval=interval,
        fulltext=fulltext,
    )


def _is_ignorable(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    if name.endswith("~") or name.endswith(".swp"):
        return True
    if path.suffix.lower() != ".xml":
        return True
    return False


def load_dir(feeds_d: Path) -> tuple[dict[str, Subscription], list[tuple[Path, str]]]:
    """Load every subscription file in feeds_d. Total: one bad file never
    prevents the good ones from loading. Returns (by_name, errors)."""
    by_name: dict[str, Subscription] = {}
    errors: list[tuple[Path, str]] = []

    if not feeds_d.is_dir():
        return by_name, errors

    for path in sorted(feeds_d.iterdir()):
        if not path.is_file() or _is_ignorable(path):
            continue
        try:
            data = path.read_bytes()
            subscription = parse_subscription(data, path)
        except SubscriptionError as exc:
            errors.append((path, str(exc)))
            continue
        except OSError as exc:
            errors.append((path, f"read error: {exc}"))
            continue

        if subscription.name in by_name:
            errors.append(
                (path, f"duplicate feed name {subscription.name!r}, keeping first")
            )
            continue
        by_name[subscription.name] = subscription

    return by_name, errors
