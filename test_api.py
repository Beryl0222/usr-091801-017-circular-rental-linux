"""HTTP 接口验证：命令写入、查询读取、错误语义。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rental import Engine, asset_identity
from rental.api import make_handler
from service import health_payload


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        handler = make_handler(self.engine, health_payload)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, command, body):
        request = Request(
            f"{self.base}/commands/{command}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            return json.load(response)

    def get(self, path):
        with urlopen(f"{self.base}{path}", timeout=2) as response:
            return json.load(response)

    def test_command_and_query_roundtrip(self):
        result = self.post(
            "register_asset",
            {"serial_no": "SN-API", "model": "X100", "category": "camera",
             "owner_id": "alice", "at": "2026-01-01T00:00:00Z"},
        )
        self.assertEqual(len(result["events"]), 1)
        asset_id = asset_identity("SN-API", "alice")
        asset = self.get(f"/queries/asset?asset_id={asset_id}")["asset"]
        self.assertEqual(asset["serial_no"], "SN-API")
        custody = self.get(f"/queries/custody_at?asset_id={asset_id}&at=2026-01-02T00:00:00Z")["custody"]
        self.assertEqual(custody["role"], "consignor")
        self.assertTrue(self.get("/queries/verify_replay")["converged"])

    def test_rejected_command_is_409_with_reasons(self):
        with self.assertRaises(HTTPError) as ctx:
            self.post("intake_asset", {"asset_id": "AST-none", "warehouse_id": "WH-A",
                                       "at": "2026-01-01T00:00:00Z"})
        self.assertEqual(ctx.exception.code, 409)
        ctx.exception.close()

    def test_unknown_command_and_query_are_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self.post("report_ewaste", {"avoided_waste_kg": 1})
        self.assertEqual(ctx.exception.code, 409)  # 统计不接受客户端上报
        ctx.exception.close()
        with self.assertRaises(HTTPError) as ctx:
            self.get("/queries/no_such_query")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_missing_field_is_409(self):
        with self.assertRaises(HTTPError) as ctx:
            self.post("register_asset", {"serial_no": "SN-X"})
        self.assertEqual(ctx.exception.code, 409)
        ctx.exception.close()

    def test_unknown_route_is_404(self):
        with self.assertRaises(HTTPError) as ctx:
            self.get("/unknown")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()
