"""Loopback service that owns the Clef reference model in its separate env."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models.interfaces import Candidate
from .models.local_clef import ClefReleaseAdapter


def serve(snapshot: str, host: str, port: int, device: str) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Clef service must bind to loopback")
    adapter = ClefReleaseAdapter(snapshot, device=device)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            self._json(200, {"status": "ready", "model": "Cloudflare/clef-flash", "head": "joint_schema_model.py"})

        def do_POST(self):
            if self.path != "/choose":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_000_000:
                self.send_error(413)
                return
            try:
                payload = json.loads(self.rfile.read(length))
                candidates = [Candidate(**item) for item in payload["candidates"]]
                scores = adapter.choose(payload["state"], payload["objective"], candidates)
                self._json(200, {"scores": scores, "metrics": getattr(adapter, "last_metrics", {"status": "unavailable"})})
            except Exception as exc:
                self._json(400, {"error": type(exc).__name__, "message": str(exc)[:300]})

        def _json(self, status, value):
            body = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print("CLEF_READY", flush=True)
    server.serve_forever(poll_interval=0.5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()
    serve(args.snapshot, args.host, args.port, args.device)


if __name__ == "__main__":
    main()
