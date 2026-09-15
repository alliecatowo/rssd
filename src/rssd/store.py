"""Atomic writes, revisions, symlinks — the effectful floor everything else
stands on.

Priority 1 of SPEC.md: "Reads are sacred. A reader must never observe a
partial, corrupt, or non-well-formed file. Ever." Every write in this module
goes: write to a temp file in the SAME directory as the destination (so
os.replace is atomic even across btrfs vs tmpfs boundaries), fsync it, then
os.replace over the destination. A SIGKILL at any point can only ever leave an
orphaned ``.rssd-tmp-*`` file behind — never a partial ``*.xml``.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rssd.config import TMP_PREFIX, Config
from rssd.models import EntryRecord

# ── low-level atomic primitives ─────────────────────────────────────────────


def atomic_write(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Write ``data`` to ``path`` such that a reader never observes a partial
    file. The temp file MUST live in ``path.parent`` — a default
    ``tempfile.mkstemp()`` lands in ``/tmp``, which is tmpfs while the store is
    btrfs, and ``os.replace`` across filesystems raises ``EXDEV``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=TMP_PREFIX)
    tmp_path = Path(tmp_name)
    try:
        # mkstemp creates 0600. That is the right default for a secret and the
        # wrong one for this: the whole premise is that any program on the box
        # can read the tree, so the files have to be world-readable (modulo the
        # caller's umask, which is applied here as it would be for a normal
        # creat()).
        os.fchmod(fd, 0o644 & ~_umask())
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _umask() -> int:
    """Read the process umask without permanently changing it."""
    current = os.umask(0o022)
    os.umask(current)
    return current

def fsync_dir(path: Path) -> None:
    """fsync a directory so that renames/creates within it are durable.
    Batched once per poll per SPEC §8 — not once per file."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_symlink(link_path: Path, target_name: str, *, fsync: bool = True) -> None:
    """Create/replace a symlink at ``link_path`` pointing at ``target_name``
    (a relative name, resolved within ``link_path.parent``) such that readers
    always resolve either the old target or the new one, never a missing
    file."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{TMP_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    tmp_path = link_path.parent / tmp_name
    try:
        os.symlink(target_name, tmp_path)
        os.replace(tmp_path, link_path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    if fsync:
        fsync_dir(link_path.parent)


def sweep_temps(root: Path, older_than: float = 60.0) -> int:
    """Remove orphaned ``.rssd-tmp-*`` files older than ``older_than``
    seconds anywhere under ``root``. Returns the count removed. A SIGKILL
    mid-write can only ever leave these behind (SPEC §8.2)."""
    count = 0
    now = time.time()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.startswith(TMP_PREFIX):
                continue
            fpath = Path(dirpath) / fname
            try:
                st = fpath.lstat()
            except OSError:
                continue
            if now - st.st_mtime > older_than:
                try:
                    fpath.unlink()
                    count += 1
                except OSError:
                    pass
    return count


# ── entry filename parsing ──────────────────────────────────────────────────

#: ``{base}.r{N}.xml`` — the revision file. ``base`` is greedy but anchored
#: against the mandatory ``.rN.xml`` suffix so it can't eat into it.
_REV_FILENAME_RE = re.compile(r"^(?P<base>.+)\.r(?P<rev>\d+)\.xml$")

#: ``{stamp}-{id8}-{slug}`` — SPEC §4.2.
_BASE_NAME_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<id8>[0-9a-f]{8})-(?P<slug>.+)$"
)

_ID_ATTR_RE = re.compile(rb'<entry\b[^>]*\bid="([^"]*)"')
_FIRST_SEEN_ATTR_RE = re.compile(rb'\bfirst-seen="([^"]*)"')
_CONTENT_HASH_RE = re.compile(rb'<content\b[^>]*\bhash="([^"]*)"')

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z$"
)

#: Bound on how much of each entry file scan() reads — entries can run to many
#: KB of content, but id/first-seen/hash all live in the header, so reading
#: the whole file to rebuild the index would be needless I/O across 50k
#: files.
_SCAN_HEAD_BYTES = 4096


def _parse_iso(s: str) -> datetime:
    m = _ISO_RE.match(s)
    if not m:
        raise ValueError(f"not a recognised rssd timestamp: {s!r}")
    y, mo, d, h, mi, se, frac = m.groups()
    micro = int((frac + "000000")[:6]) if frac else 0
    return datetime(
        int(y), int(mo), int(d), int(h), int(mi), int(se), micro, tzinfo=timezone.utc
    )


# ── FeedStore ────────────────────────────────────────────────────────────────


class FeedStore:
    """The effectful store for a single feed folder: ``store/<name>/``."""

    def __init__(self, config: Config, name: str) -> None:
        self.config = config
        self.name = name

    @property
    def feed_dir(self) -> Path:
        return self.config.feed_dir(self.name)

    @property
    def entries_dir(self) -> Path:
        return self.config.entries_dir(self.name)

    def ensure_dirs(self) -> None:
        self.entries_dir.mkdir(parents=True, exist_ok=True)

    def scan(self) -> dict[str, EntryRecord]:
        """Rebuild the full picture of what's on disk, from disk alone
        (SPEC I1: ``rm -rf var/`` must be fully recoverable). Unparseable or
        unrelated files are silently skipped rather than raising."""
        records: dict[str, EntryRecord] = {}
        best_rev: dict[str, int] = {}
        if not self.entries_dir.is_dir():
            return records
        try:
            it = list(os.scandir(self.entries_dir))
        except OSError:
            return records
        for dent in it:
            name = dent.name
            if name.startswith(TMP_PREFIX):
                continue
            # Only real revision files — symlinks (the `.xml` current
            # pointer) are deliberately excluded by requiring `.rN.xml` and
            # by checking is_file(follow_symlinks=False).
            try:
                if not dent.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            m = _REV_FILENAME_RE.match(name)
            if not m:
                continue
            base_name = m.group("base")
            try:
                revision = int(m.group("rev"))
            except ValueError:
                continue
            if not _BASE_NAME_RE.match(base_name):
                continue
            if base_name in best_rev and revision <= best_rev[base_name]:
                continue
            record = self._read_record(Path(dent.path), base_name, revision)
            if record is None:
                continue
            best_rev[base_name] = revision
            records[record.id] = record
        return records

    def _read_record(
        self, path: Path, base_name: str, revision: int
    ) -> EntryRecord | None:
        try:
            with open(path, "rb") as f:
                head = f.read(_SCAN_HEAD_BYTES)
        except OSError:
            return None
        id_m = _ID_ATTR_RE.search(head)
        fs_m = _FIRST_SEEN_ATTR_RE.search(head)
        hash_m = _CONTENT_HASH_RE.search(head)
        if not (id_m and fs_m and hash_m):
            return None
        try:
            entry_id = id_m.group(1).decode("utf-8")
            first_seen_raw = fs_m.group(1).decode("utf-8")
            content_hash = hash_m.group(1).decode("utf-8")
            first_seen = _parse_iso(first_seen_raw)
        except (UnicodeDecodeError, ValueError):
            return None
        bm = _BASE_NAME_RE.match(base_name)
        assert bm is not None  # already validated by caller
        id8 = bm.group("id8")
        return EntryRecord(
            id=entry_id,
            id8=id8,
            base_name=base_name,
            revision=revision,
            content_hash=content_hash,
            first_seen=first_seen,
        )

    def write_entry(
        self, *, base_name: str, data: bytes, revision: int, fsync: bool = True
    ) -> Path:
        """Write ``<base_name>.r<revision>.xml`` atomically and repoint the
        ``<base_name>.xml`` symlink at it. Returns the revision file path."""
        self.ensure_dirs()
        rev_name = f"{base_name}.r{revision}.xml"
        rev_path = self.entries_dir / rev_name
        atomic_write(rev_path, data, fsync=fsync)
        link_path = self.entries_dir / f"{base_name}.xml"
        atomic_symlink(link_path, rev_name, fsync=fsync)
        return rev_path

    def write_feed_xml(self, data: bytes) -> bool:
        """Rewrite ``feed.xml`` only if the bytes actually changed. Returns
        whether a write happened — SPEC §4: consumers watch this file and
        must not be woken every poll for an unchanged feed."""
        self.feed_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.feed_xml(self.name)
        try:
            existing = path.read_bytes()
        except OSError:
            existing = None
        if existing == data:
            return False
        atomic_write(path, data)
        return True

    def write_status_xml(self, data: bytes) -> None:
        """``status.xml`` is volatile — always rewritten, no change-guard."""
        self.feed_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(self.config.status_xml(self.name), data)

    def entry_count(self) -> int:
        return len(self.scan())

    def fsync_entries(self) -> None:
        """Batched directory fsync — once per poll, not once per file
        (SPEC §8)."""
        if self.entries_dir.is_dir():
            fsync_dir(self.entries_dir)
