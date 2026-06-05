#!/usr/bin/env python
"""Serve outputs/ over HTTP **with Range support** so the PMTiles map actually loads.

The MapLibre viewer fetches tile byte-ranges from ``locations.pmtiles``; a plain
``python -m http.server`` answers ``Range`` requests with a full ``200`` body (no byte
serving), so pmtiles.js fails on every tile and the map shows no points. This minimal
threaded server returns proper ``206 Partial Content`` responses.

    ../../.venv/bin/python scripts/serve_map.py        # -> http://localhost:8000/coverage_map.html
    ../../.venv/bin/python scripts/serve_map.py 8800   # custom port

(Equivalent quick alternative if you have Node: ``npx serve outputs``.)
"""
from __future__ import annotations

import io
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUTPUTS = Path(__file__).resolve().parents[1] / "outputs"


class _Limited(io.RawIOBase):
    """Read at most ``n`` bytes from an open file (for a single Range slice)."""

    def __init__(self, f, n):
        self.f, self.n = f, n

    def read(self, size=-1):
        if self.n <= 0:
            return b""
        if size < 0 or size > self.n:
            size = self.n
        data = self.f.read(size)
        self.n -= len(data)
        return data

    def readable(self):
        return True

    def close(self):
        # SimpleHTTPRequestHandler closes the object we return from send_head(); make sure the
        # underlying file is closed too, or many tile requests leak FDs until reads start failing.
        try:
            self.f.close()
        finally:
            super().close()


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            return super().send_head()
        size = os.path.getsize(path)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        length = end - start + 1
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        return _Limited(f, length)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    handler = partial(RangeHandler, directory=str(OUTPUTS))
    with ThreadingHTTPServer(("", port), handler) as httpd:
        httpd.daemon_threads = True
        print(f"serving {OUTPUTS} with Range support")
        print(f"  open http://localhost:{port}/coverage_map.html")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
