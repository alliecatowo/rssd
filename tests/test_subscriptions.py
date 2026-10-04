from __future__ import annotations

from pathlib import Path

import pytest

from rssd import subscriptions
from rssd.subscriptions import SubscriptionError, load_dir, parse_subscription


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ── parse_subscription: URL extraction order ─────────────────────────────


def test_url_from_subscription_url(tmp_path):
    path = write(
        tmp_path,
        "rust-blog.xml",
        "<subscription><url>https://blog.rust-lang.org/feed.xml</url></subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://blog.rust-lang.org/feed.xml"
    assert sub.name == "rust-blog"


def test_url_from_subscription_link(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<subscription><link>https://example.com/feed</link></subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://example.com/feed"


def test_url_from_opml_outline(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        '<opml><body><outline xmlUrl="https://example.com/opml-feed" /></body></opml>',
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://example.com/opml-feed"


def test_url_from_atom_self_link(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        '<link rel="self" href="https://example.com/atom-feed" /></feed>',
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://example.com/atom-feed"


def test_url_from_first_http_text_node(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<something><note>see https://example.com/plain-text</note></something>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://example.com/plain-text"


def test_url_precedence_prefers_subscription_url_over_others(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<subscription>"
        "<url>https://primary.example.com/feed</url>"
        "<link>https://secondary.example.com/feed</link>"
        "</subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.url == "https://primary.example.com/feed"


def test_no_url_raises(tmp_path):
    path = write(tmp_path, "feed.xml", "<subscription></subscription>")
    with pytest.raises(SubscriptionError):
        parse_subscription(path.read_bytes(), path)


def test_invalid_xml_raises(tmp_path):
    path = write(tmp_path, "feed.xml", "<subscription><url>not closed")
    with pytest.raises(SubscriptionError):
        parse_subscription(path.read_bytes(), path)


# ── name resolution ───────────────────────────────────────────────────────


def test_name_defaults_to_filename_stem(tmp_path):
    path = write(
        tmp_path, "my-cool-feed.xml", "<subscription><url>https://x.com/f</url></subscription>"
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.name == "my-cool-feed"


def test_name_element_overrides_stem(tmp_path):
    path = write(
        tmp_path,
        "original-stem.xml",
        "<subscription><url>https://x.com/f</url><name>override-name</name></subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.name == "override-name"


def test_invalid_name_element_raises(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<subscription><url>https://x.com/f</url><name>Not Valid!</name></subscription>",
    )
    with pytest.raises(SubscriptionError):
        parse_subscription(path.read_bytes(), path)


# ── interval / fulltext ────────────────────────────────────────────────────


def test_interval_parsed(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<subscription><url>https://x.com/f</url><interval>15m</interval></subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.interval == 900


def test_interval_absent_is_none(tmp_path):
    path = write(tmp_path, "feed.xml", "<subscription><url>https://x.com/f</url></subscription>")
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.interval is None


def test_fulltext_default_false(tmp_path):
    path = write(tmp_path, "feed.xml", "<subscription><url>https://x.com/f</url></subscription>")
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.fulltext is False


def test_fulltext_true(tmp_path):
    path = write(
        tmp_path,
        "feed.xml",
        "<subscription><url>https://x.com/f</url><fulltext>true</fulltext></subscription>",
    )
    sub = parse_subscription(path.read_bytes(), path)
    assert sub.fulltext is True


# ── load_dir totality ───────────────────────────────────────────────────────


def test_load_dir_loads_good_files_despite_bad_ones(tmp_path):
    write(tmp_path, "good-one.xml", "<subscription><url>https://a.com/f</url></subscription>")
    write(tmp_path, "bad.xml", "<subscription><url>not closed")
    write(tmp_path, "good-two.xml", "<subscription><url>https://b.com/f</url></subscription>")

    by_name, errors = load_dir(tmp_path)

    assert set(by_name) == {"good-one", "good-two"}
    assert len(errors) == 1
    assert errors[0][0].name == "bad.xml"


def test_load_dir_empty_directory(tmp_path):
    by_name, errors = load_dir(tmp_path)
    assert by_name == {}
    assert errors == []


def test_load_dir_missing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    by_name, errors = load_dir(missing)
    assert by_name == {}
    assert errors == []


def test_load_dir_ignores_dotfiles_and_swap_files(tmp_path):
    write(tmp_path, ".hidden.xml", "<subscription><url>https://a.com/f</url></subscription>")
    write(tmp_path, "feed.xml.swp", "<subscription><url>https://a.com/f</url></subscription>")
    write(tmp_path, "feed.xml~", "<subscription><url>https://a.com/f</url></subscription>")
    write(tmp_path, "real.xml", "<subscription><url>https://a.com/f</url></subscription>")

    by_name, errors = load_dir(tmp_path)
    assert set(by_name) == {"real"}


def test_load_dir_duplicate_names_reported_as_error(tmp_path):
    write(tmp_path, "one.xml", "<subscription><url>https://a.com/f</url><name>dupe</name></subscription>")
    write(tmp_path, "two.xml", "<subscription><url>https://b.com/f</url><name>dupe</name></subscription>")

    by_name, errors = load_dir(tmp_path)
    assert "dupe" in by_name
    assert len(errors) == 1


def test_load_dir_is_total_one_bad_file_never_blocks_others(tmp_path):
    for i in range(5):
        write(tmp_path, f"good{i}.xml", f"<subscription><url>https://a.com/{i}</url></subscription>")
    write(tmp_path, "empty.xml", "")
    write(tmp_path, "no-url.xml", "<subscription></subscription>")

    by_name, errors = load_dir(tmp_path)
    assert len(by_name) == 5
    assert len(errors) == 2


def test_subscription_xml_escapes_and_roundtrips(tmp_path):
    from rssd.cli import subscription_xml
    from rssd.subscriptions import parse_subscription

    url = "https://e.com/feed?a=1&b=2&c=<x>"
    xml = subscription_xml("my-feed", url, "weird -- comment-")
    sub = parse_subscription(xml.encode(), tmp_path / "my-feed.xml")
    assert sub.url == url
    assert sub.name == "my-feed"


def test_rssd_add_with_ampersand_url(tmp_path):
    from rssd.cli import main
    from rssd.subscriptions import load_dir

    root = tmp_path / "r"
    assert main(["init", str(root)]) == 0
    assert main(["add", "https://e.com/feed?a=1&b=2", "--name", "amp", "--root", str(root)]) == 0
    subs, errors = load_dir(root / "feeds.d")
    assert not errors
    assert subs["amp"].url == "https://e.com/feed?a=1&b=2"


def test_prune_skips_still_subscribed_feed(tmp_path, capsys):
    from rssd.cli import main
    from rssd.config import Config
    from rssd.state import load_state, save_state

    root = tmp_path / "r"
    assert main(["init", str(root)]) == 0
    assert main(["add", "https://e.com/f.xml", "--name", "keep", "--root", str(root)]) == 0
    config = Config(root=root)
    folder = config.feed_dir("keep")
    folder.mkdir(parents=True)
    (folder / "data.txt").write_text("x")
    state = load_state(config, "keep")
    state.health = "retired"
    save_state(config, state)
    assert main(["prune", "--yes", "--root", str(root)]) == 0
    assert folder.exists()
