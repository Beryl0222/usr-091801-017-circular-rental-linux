"""循环租用资产履约的运行入口：健康探针与资产账本 API。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from domain import _as_ms
from scenario import run_selfcheck

SERVICE_ID = "circular-rental"
SERVICE_NAME = "循环租用资产履约"

# 进程级单例账本；命令按幂等键与事件ID去重，重复/乱序投递收敛
_LEDGER = None


def get_ledger():
    global _LEDGER
    if _LEDGER is None:
        from domain import Ledger

        _LEDGER = Ledger()
    return _LEDGER


def health_payload():
    """返回服务身份和状态（保持基线契约不变）。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康端点与账本读写端点。"""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        path = parts.path
        if path == "/health":
            self._send_json(200, health_payload())
            return
        if path == "/standards":
            self._send_json(200, get_ledger().catalog.listing())
            return
        if path == "/digest":
            self._send_json(200, {"digest": get_ledger().digest()})
            return
        if path == "/stats":
            self._send_json(200, get_ledger().stats())
            return
        if path == "/dispatch":
            at = _query_at(query)
            self._send_json(200, {"eligible": get_ledger().dispatch_eligible(at)})
            return
        if path.startswith("/assets/"):
            asset_id = path[len("/assets/"):]
            if not asset_id:
                self.send_error(404)
                return
            at = _query_at(query)
            ledger = get_ledger()
            if at is not None:
                snap = ledger.snapshot_at(asset_id, at)
                if snap is None or not snap.get("registered"):
                    self._send_json(404, {"error": "资产不存在或该时点尚未登记"})
                    return
                self._send_json(200, snap)
                return
            state = ledger.asset(asset_id)
            if state is None:
                self._send_json(404, {"error": "资产不存在"})
                return
            self._send_json(200, _public_state(state))
            return
        self.send_error(404)

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path != "/commands":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"[]")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "请求体必须是 JSON 命令或命令数组"})
            return
        commands = payload if isinstance(payload, list) else [payload]
        if not commands or not all(isinstance(c, dict) for c in commands):
            self._send_json(400, {"error": "命令不能为空"})
            return
        try:
            results = get_ledger().ingest_commands(commands)
        except Exception as exc:  # 防御：单批解析异常不应击垮服务
            self._send_json(400, {"error": str(exc)})
            return
        status = 207 if any(r["status"] not in ("accepted", "duplicate") for r in results) else 200
        self._send_json(status, {"results": results, "digest": get_ledger().digest()})

    def log_message(self, *_args):
        return


def _query_at(query):
    raw = query.get("at", [None])[0]
    if raw is None:
        return None
    try:
        return _as_ms(raw) if not raw.isdigit() else int(raw)
    except ValueError:
        return None


def _public_state(state):
    return {
        "asset_id": state["asset_id"],
        "category": state["category"],
        "model": state["model"],
        "owner": state["owner"],
        "retired": state["retired"],
        "location": state["location"],
        "ownership": state["ownership"],
        "custody": state["custody"],
        "usage": state["usage"],
        "flows": state["flows"],
        "claims": list(state["claims"].values()),
        "rentals": state["rentals"],
        "inspections": state["inspections"],
        "version": state["version"],
    }


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["name"] == SERVICE_NAME
        run_selfcheck(verbose=True)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
