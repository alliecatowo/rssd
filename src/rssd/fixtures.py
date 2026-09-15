"""A local HTTP server over the committed feed fixtures.

Demo insurance. Venue wifi dies, a publisher starts returning 500s, or you want
to show the daemon working on a plane -- point every subscription here with
`--fixture-mode` and the whole system runs offline, with correct ETag and
Last-Modified behaviour so the conditional-GET path still gets exercised.

It also underpins `rssd demo-mutate`: mutating a served fixture is the only
reproducible way to demonstrate "every revision is kept" without waiting for a
real publisher to fix a typo.
"""

from __future__ import annotations

import hashlib
import http.server
import socketserver
import threading
from email.utils import formatdate
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "feeds"

#: Subscription name -> fixture filename. Mirrors the reference feeds in SPEC §14.
FIXTURE_MAP = {
    "lobsters": "lobsters.xml",
    "rust-blog": "rust-blog.xml",
    "simonw": "simonw.xml",
    "hn": "hn.xml",
    "xkcd": "xkcd.xml",
    "godev": "godev.xml",
    "mutable": "mutable.xml",
}


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    directory: Path = FIXTURE_DIR

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass  # the daemon's event log is the interesting output, not this

    def _resolve(self) -> Path | None:
        name = self.path.lstrip("/").split("?")[0]
        if not name or "/" in name or ".." in name:
            return None
        path = self.directory / name
        return path if path.is_file() else None

    def do_GET(self) -> None:  # noqa: N802
        path = self._resolve()
        if path is None:
            self.send_error(404)
            return
        body = path.read_bytes()
        stat = path.stat()
        etag = '"%s"' % hashlib.sha256(body).hexdigest()[:16]
        last_modified = formatdate(stat.st_mtime, usegmt=True)

        # Honour conditional requests, so the 304 path is demoable offline.
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        if self.headers.get("If-Modified-Since") == last_modified:
            self.send_response(304)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Cache-Control", "max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 8765, directory: Path | None = None) -> _Server:
    if directory is not None:
        FixtureHandler.directory = directory
    return _Server(("127.0.0.1", port), FixtureHandler)


def serve_in_thread(port: int = 8765, directory: Path | None = None) -> tuple[_Server, threading.Thread]:
    server = serve(port, directory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
