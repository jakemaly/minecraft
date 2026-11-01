"""Small HTTP surface for the local Minecraft control app."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_MAX_BODY = 1_000_000
_RUN_ID = re.compile(r"run-[A-Za-z0-9-]{8,80}\Z")
_CONTROLS = {
    "auth_start", "auth_callback", "pin_default_model", "disconnect_chatgpt",
    "start_run", "pause_run", "resume_run", "stop_run", "export_run",
}
_UI_ASSETS = {
    "dark-theme-enabled.svg",
}


def create_server(daemon, host: str, port: int) -> ThreadingHTTPServer:
    index = Path(__file__).resolve().parents[2] / "web" / "index.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            return not origin or urlparse(origin).netloc == self.headers.get("Host")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > _MAX_BODY:
                raise ValueError("request body exceeds 1 MB")
            value = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(value, dict):
                raise ValueError("request body must be a JSON object")
            return value

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if route.path in {"/", "/index.html"}:
                try:
                    self._send(200, index.read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    self._json(404, {"error": "app UI file is missing"})
                return
            if route.path.startswith("/assets/"):
                name = route.path.removeprefix("/assets/")
                if name not in _UI_ASSETS:
                    self._json(404, {"error": "app asset not found"})
                    return
                try:
                    self._send(200, (index.parent / "assets" / name).read_bytes(), "image/svg+xml; charset=utf-8")
                except OSError:
                    self._json(404, {"error": "app asset is missing"})
                return
            if route.path == "/api/status":
                try:
                    self._json(200, daemon.status())
                except Exception as exc:
                    self._json(503, {"error": str(exc)[:500]})
                return
            if route.path == "/api/live":
                self._json(200, daemon.live_status())
                return
            if route.path == "/api/events":
                query = parse_qs(route.query)
                try:
                    before = int(query["before"][0]) if query.get("before") else None
                    limit = int(query.get("limit", ["100"])[0])
                    result = daemon.events_page(query.get("run_id", [""])[0], before=before, limit=limit)
                    self._json(200, result)
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)[:300]})
                return
            if route.path == "/api/workspace":
                run_id = parse_qs(route.query).get("run_id", [""])[0]
                if not _RUN_ID.fullmatch(run_id):
                    self._json(400, {"error": "invalid run ID"})
                    return
                try:
                    self._json(200, daemon.workspace_tree(run_id))
                except ValueError as exc:
                    self._json(404, {"error": str(exc)[:300]})
                except OSError:
                    self._json(404, {"error": "workspace could not be read"})
                return
            if route.path == "/api/frame":
                query = parse_qs(route.query)
                run_id = query.get("run_id", [""])[0]
                relative = query.get("path", [""])[0]
                if not _RUN_ID.fullmatch(run_id):
                    self._json(400, {"error": "invalid run ID"})
                    return
                run_root = (daemon.runtime / "runs" / run_id).resolve()
                if run_root.parent != (daemon.runtime / "runs").resolve() or run_root.name != run_id:
                    self._json(404, {"error": "run directory is not available"})
                    return
                path = (run_root / relative).resolve()
                if path.parent != (run_root / "media" / "frames").resolve() or path.suffix.lower() != ".png" or not path.is_file():
                    self._json(404, {"error": "viewer frame is not available"})
                    return
                self._send(200, path.read_bytes(), "image/png")
                return
            if route.path == "/api/artifact":
                run_id = parse_qs(route.query).get("run_id", [""])[0]
                if not _RUN_ID.fullmatch(run_id):
                    self._json(400, {"error": "invalid run ID"})
                    return
                try:
                    result = daemon.export_run()
                except Exception as exc:
                    self._json(409, {"error": str(exc)[:500]})
                    return
                path = Path(result["path"]).resolve()
                if path.parent != (daemon.runtime / "exports").resolve() or not path.is_file():
                    self._json(404, {"error": "run export is not available"})
                    return
                if path.name != f"{run_id}.zip":
                    self._json(404, {"error": "run export does not match this run"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="{run_id}.zip"')
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with path.open("rb") as archive:
                    while chunk := archive.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/control":
                self._json(404, {"error": "not found"})
                return
            if not self._same_origin():
                self._json(403, {"error": "cross-origin control request refused"})
                return
            try:
                body = self._body()
                method = body.get("method")
                params = body.get("params", {})
                if method not in _CONTROLS or not isinstance(params, dict):
                    raise ValueError("unsupported control request")
                self._json(200, daemon.dispatch(method, params))
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)[:300]})
            except Exception as exc:
                self._json(409, {"error": str(exc)[:500]})

    return ThreadingHTTPServer((host, port), Handler)
