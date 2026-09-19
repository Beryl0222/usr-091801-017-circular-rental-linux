"""HTTP 接口：/health 健康探针、/commands/<name> 写入、/queries/<name> 读取。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .engine import CommandRejected, Engine, UnknownQuery


def make_handler(engine: Engine, health):
    class ApiHandler(BaseHTTPRequestHandler):
        server_version = "CircularRental/1.0"

        def _send(self, code: int, obj: dict):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self._send(200, health())
            if parsed.path.startswith("/queries/"):
                name = parsed.path[len("/queries/"):]
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                try:
                    return self._send(200, engine.query(name, params))
                except UnknownQuery:
                    return self._send(404, {"error": "unknown_query"})
                except CommandRejected as exc:
                    return self._send(400, {"error": "bad_query", "reasons": exc.reasons})
                except ValueError as exc:
                    return self._send(400, {"error": "bad_query", "reasons": [str(exc)]})
            return self._send(404, {"error": "not_found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/commands/"):
                name = parsed.path[len("/commands/"):]
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (json.JSONDecodeError, ValueError):
                    return self._send(400, {"error": "invalid_json"})
                try:
                    events = engine.execute(name, body)
                except CommandRejected as exc:
                    return self._send(409, {"error": "command_rejected", "reasons": exc.reasons})
                return self._send(200, {"events": [e.as_dict() for e in events]})
            return self._send(404, {"error": "not_found"})

        def log_message(self, *_args):
            return

    return ApiHandler
