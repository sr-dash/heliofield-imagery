#!/usr/bin/env python3
"""Serve this folder on http://localhost:8812 with the cross-origin header
GitHub Pages also sends, so a local HelioField can read the images."""
import http.server
import sys
from functools import partial
from pathlib import Path


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8812
http.server.ThreadingHTTPServer(("", port), partial(Handler, directory=str(Path(__file__).parent))).serve_forever()
