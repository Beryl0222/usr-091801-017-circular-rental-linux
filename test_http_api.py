"""HTTP API 契约：命令投递、幂等、时点查询、调度清单与未知路由。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from domain import Ledger

        service._LEDGER = Ledger()  # 每个测试类一个干净账本
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, payload):
        data = json.dumps(payload).encode()
        req = Request(self.base + path, data=data, method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, None

    def _get(self, path):
        try:
            with urlopen(self.base + path, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, None

    def test_full_flow_over_http_with_idempotent_repost(self):
        status, body = self._post("/commands", [
            {"type": "register_asset", "asset_id": "cam-x", "at": 1_000,
             "category": "camera", "model": "M", "owner": "alice",
             "idem_key": "h-reg"},
            {"type": "consign", "asset_id": "cam-x", "at": 2_000,
             "warehouse_id": "wh-1", "idem_key": "h-consign"},
            {"type": "inspect", "asset_id": "cam-x", "at": 3_000,
             "inspection_id": "h-i1", "warehouse_id": "wh-1",
             "standard_id": "std-camera", "standard_version": 1,
             "rules_id": "charge-rules", "rules_version": 1,
             "items": [{"code": c, "grade": 0, "evidence_hash": f"sha256:{c}"}
                       for c in ("shutter", "lens", "body", "sensor", "function")],
             "idem_key": "h-insp"},
        ])
        self.assertEqual(status, 200)
        self.assertTrue(all(r["status"] == "accepted" for r in body["results"]))
        first_digest = body["digest"]

        # 重复投递：全部 duplicate，digest 不变
        status, body = self._post("/commands", [
            {"type": "register_asset", "asset_id": "cam-x", "at": 1_000,
             "category": "camera", "model": "M", "owner": "alice",
             "idem_key": "h-reg"},
        ])
        self.assertEqual(body["results"][0]["status"], "duplicate")
        status, digest_body = self._get("/digest")
        self.assertEqual(digest_body["digest"], first_digest)

        # 资产查询
        status, asset = self._get("/assets/cam-x")
        self.assertEqual(status, 200)
        self.assertEqual(asset["owner"], "alice")
        self.assertEqual(len(asset["inspections"]), 1)

        # 时点查询：登记前 404；入仓后在保
        self.assertEqual(self._get("/assets/cam-x?at=500")[0], 404)
        status, snap = self._get("/assets/cam-x?at=2500")
        self.assertEqual(status, 200)
        self.assertTrue(snap["in_platform_custody"])
        self.assertEqual(snap["custody"]["warehouse_id"], "wh-1")

        # 争议冻结后不可调度
        self._post("/commands", [
            {"type": "raise_claim", "asset_id": "cam-x", "at": 4_000,
             "claim_id": "h-c1", "idem_key": "h-claim"},
        ])
        status, dispatch = self._get("/dispatch?at=500000")
        self.assertNotIn("cam-x", [row["asset_id"] for row in dispatch["eligible"]])
        status, snap = self._get("/assets/cam-x?at=500000")
        self.assertFalse(snap["dispatch_eligible"])
        self.assertEqual(snap["blocking_flows"], ["evidentiary_hold"])

    def test_standards_and_stats_endpoints(self):
        status, standards = self._get("/standards")
        self.assertEqual(status, 200)
        ids = {(d["standard_id"], d["version"]) for d in standards["standards"]}
        self.assertIn(("std-camera", 1), ids)
        status, stats = self._get("/stats")
        self.assertEqual(status, 200)
        self.assertTrue(stats["derived_from_events"])
        self.assertGreaterEqual(stats["assets"], 1)

    def test_invalid_command_reported_without_crashing(self):
        status, body = self._post("/commands", [
            {"type": "report_ewaste_total", "asset_id": "cam-x",
             "ewaste_avoided_kg": 999, "idem_key": "cheat"},
        ])
        self.assertEqual(status, 207)
        self.assertEqual(body["results"][0]["status"], "invalid")

    def test_unknown_routes_and_malformed_body(self):
        self.assertEqual(self._get("/unknown")[0], 404)
        self.assertEqual(self._post("/nope", {})[0], 404)
        status, _ = self._post("/commands", "not-json")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
