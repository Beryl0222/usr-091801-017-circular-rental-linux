"""领域规则测试：身份、权利互斥、版本验机、争议冻结、收敛与派生统计。"""

import hashlib
import random
import unittest

from domain import (
    DEFAULT_RULES,
    DEFAULT_STANDARD,
    Catalog,
    Ledger,
    Projection,
    Rejected,
    canon,
    digest_of,
)
from scenario import build_commands as scenario_commands
from scenario import _feed

T = 1_700_000_000_000
H = 3_600_000
D = 86_400_000
WH = "wh-1"


def eh(tag):
    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def base_items(prefix, grades=None):
    grades = grades or {}
    return [
        {"code": code, "grade": grades.get(code, 0), "evidence_hash": eh(f"{prefix}-{code}")}
        for code in ("shutter", "lens", "body", "sensor", "function")
    ]


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()

    def feed(self, *cmds):
        return self.ledger.ingest_commands(list(cmds))

    def cmd(self, ctype, at, **kw):
        kw["type"] = ctype
        kw.setdefault("asset_id", "a1")
        kw["at"] = at
        kw.setdefault("idem_key", f"{ctype}-{at}-{kw.get('rental_id', '')}{kw.get('shipment_id', '')}")
        return kw

    def register_flow(self, ledger=None, deposit_free=True):
        """登记 -> 入仓 -> 验机 -> 确认出租 -> 出库 -> 交付。"""
        ledger = ledger or self.ledger
        std = (DEFAULT_STANDARD["standard_id"], DEFAULT_STANDARD["version"])
        rules = (DEFAULT_RULES["rules_id"], DEFAULT_RULES["version"])
        ledger.ingest_commands([
            {"type": "register_asset", "asset_id": "a1", "at": T,
             "category": "camera", "model": "M1", "owner": "alice", "idem_key": "reg"},
            {"type": "consign", "asset_id": "a1", "at": T + H, "warehouse_id": WH,
             "idem_key": "consign"},
            {"type": "inspect", "asset_id": "a1", "at": T + 2 * H,
             "inspection_id": "i1", "warehouse_id": WH,
             "standard_id": std[0], "standard_version": std[1],
             "rules_id": rules[0], "rules_version": rules[1],
             "items": base_items("i1"), "idem_key": "in1"},
            {"type": "confirm_rental", "asset_id": "a1", "at": T + 3 * H,
             "rental_id": "r1", "lessee": "bob",
             "rental_start": T + D, "rental_end": T + 4 * D,
             "deposit_free": deposit_free,
             "deposit_policy_snapshot": {"free": deposit_free, "tier": "A"},
             "idem_key": "r1c"},
            {"type": "scan_outbound", "asset_id": "a1", "at": T + D,
             "rental_id": "r1", "shipment_id": "s1", "idem_key": "out"},
            {"type": "confirm_delivery", "asset_id": "a1", "at": T + D + H,
             "shipment_id": "s1", "idem_key": "del"},
        ])

    # -- 身份与物权 --------------------------------------------------------

    def test_asset_identity_is_non_reusable(self):
        self.feed(self.cmd("register_asset", T, category="camera", model="M", owner="o",
                           idem_key="x1"))
        again = self.cmd("register_asset", T + H, category="camera", model="M", owner="o2",
                         idem_key="x2")
        results = self.feed(again)
        self.assertEqual(results[0]["status"], "accepted")  # 命令受理
        rejected = [r for r in self.ledger.rejected if r["reason"].startswith("资产身份不可复用")]
        self.assertEqual(len(rejected), 1)

    def test_ownership_custody_usage_intervals_are_exclusive(self):
        self.register_flow()
        # 使用期间：承租人有使用权，平台无保管，物权仍在寄租人
        mid = self.ledger.snapshot_at("a1", T + 2 * D)
        self.assertIsNone(mid["custody"])
        self.assertEqual(mid["usage"]["lessee"], "bob")
        self.assertEqual(mid["owner_at"], "alice")
        # 寄租人从未拥有使用权/保管权
        state = self.ledger.asset("a1")
        owners = [seg["holder"] for seg in state["ownership"]]
        self.assertNotIn("bob", owners)
        # 同一时点权利不重叠：保管与使用互斥
        for at in range(T, T + 5 * D, H):
            snap = self.ledger.snapshot_at("a1", at)
            self.assertFalse(snap["custody"] and snap["usage"], f"{at} 保管与使用重叠")

    # -- 版本化验机 --------------------------------------------------------

    def test_inspection_rejects_unknown_item_and_grade_and_missing_evidence(self):
        self.feed(self.cmd("register_asset", T, category="camera", model="M", owner="o"),
                  self.cmd("consign", T + H, warehouse_id=WH))
        bad_items = [
            base_items("bad") + [{"code": "ghost", "grade": 0, "evidence_hash": eh("g")}],
            [{**base_items("g2")[0], "grade": 9}],
            [{**base_items("g3")[0], "evidence_hash": ""}],
        ]
        for n, items in enumerate(bad_items):
            res = self.ledger.ingest_commands([{
                "type": "inspect", "asset_id": "a1", "at": T + (2 + n) * H,
                "inspection_id": f"bad-{n}", "warehouse_id": WH,
                "standard_id": DEFAULT_STANDARD["standard_id"], "standard_version": 1,
                "rules_id": DEFAULT_RULES["rules_id"], "rules_version": 1,
                "items": items, "idem_key": f"bad-in-{n}"}])
            self.assertEqual(res[0]["status"], "accepted")
        reasons = [r["reason"] for r in self.ledger.rejected]
        self.assertTrue(any("不在标准中" in x for x in reasons))
        self.assertTrue(any("等级越界" for x in reasons))
        self.assertTrue(any("证据哈希" in x for x in reasons))
        self.assertEqual(len(self.ledger.asset("a1")["inspections"]), 0)

    def test_registered_standard_version_is_immutable(self):
        cat = Catalog()
        tweaked = {**DEFAULT_STANDARD, "name": "被篡改的名字"}
        with self.assertRaises(ValueError):
            cat.register_standard(tweaked)

    def test_inspection_pins_standard_version_and_evidence_bundle_hash(self):
        self.register_flow()
        ins = self.ledger.asset("a1")["inspections"][0]
        expected = digest_of({
            "standard_id": DEFAULT_STANDARD["standard_id"],
            "standard_version": 1,
            "items": ins["results"],
        })
        self.assertEqual(ins["evidence_bundle_hash"], expected)

    # -- 免押快照 ----------------------------------------------------------

    def test_deposit_policy_change_only_affects_unconfirmed_rentals(self):
        self.register_flow(deposit_free=True)
        # r1 已确认且快照 free=True；政策收紧后，已确认租期快照不变
        r1 = next(r for r in self.ledger.asset("a1")["rentals"] if r["rental_id"] == "r1")
        self.assertTrue(r1["deposit_policy_snapshot"]["free"])
        # 新开租期按收紧后的资格快照 false
        self.ledger.ingest_commands([
            {"type": "scan_return", "asset_id": "a1", "at": T + 4 * D,
             "rental_id": "r1", "shipment_id": "s1", "warehouse_id": WH,
             "idem_key": "ret1"},
            {"type": "confirm_rental", "asset_id": "a1", "at": T + 5 * D,
             "rental_id": "r2", "lessee": "carol",
             "rental_start": T + 6 * D, "rental_end": T + 7 * D,
             "deposit_free": False, "idem_key": "r2c"},
        ])
        r2 = next(r for r in self.ledger.asset("a1")["rentals"] if r["rental_id"] == "r2")
        r1_now = next(r for r in self.ledger.asset("a1")["rentals"] if r["rental_id"] == "r1")
        self.assertFalse(r2["deposit_free"])
        self.assertTrue(r1_now["deposit_policy_snapshot"]["free"])

    # -- 争议与阻断 --------------------------------------------------------

    def test_claim_freezes_asset_until_resolved(self):
        self.register_flow()
        self.ledger.ingest_commands([
            {"type": "scan_return", "asset_id": "a1", "at": T + 4 * D,
             "rental_id": "r1", "shipment_id": "s1", "warehouse_id": WH,
             "idem_key": "ret"},
            {"type": "raise_claim", "asset_id": "a1", "at": T + 4 * D + H,
             "claim_id": "c1", "idem_key": "claim"},
        ])
        # 冻结期间：出租、转运、出库、维修全部拒绝
        attempts = [
            {"type": "confirm_rental", "asset_id": "a1", "at": T + 5 * D,
             "rental_id": "rx", "lessee": "x", "rental_start": T + 6 * D,
             "rental_end": T + 7 * D, "idem_key": "rx"},
            {"type": "start_transfer", "asset_id": "a1", "at": T + 5 * D + H,
             "shipment_id": "sx", "from_warehouse": WH, "to_warehouse": "wh-2",
             "idem_key": "sx"},
            {"type": "start_repair", "asset_id": "a1", "at": T + 5 * D + 2 * H,
             "repair_id": "fx", "idem_key": "fx"},
        ]
        self.ledger.ingest_commands(attempts)
        reasons = {r["type"]: r["reason"] for r in self.ledger.rejected}
        self.assertIn("rental_confirmed", reasons)
        self.assertIn("transfer_started", reasons)
        self.assertIn("repair_started", reasons)
        for at in (T + 5 * D, T + 9 * D):
            ids = {x["asset_id"] for x in self.ledger.dispatch_eligible(at)}
            self.assertNotIn("a1", ids)
        # 争议解除后恢复可调度
        self.ledger.ingest_commands([
            {"type": "resolve_claim", "asset_id": "a1", "at": T + 10 * D,
             "claim_id": "c1", "resolution": "resolved", "idem_key": "resolve"}])
        ids = {x["asset_id"] for x in self.ledger.dispatch_eligible(T + 10 * D + H)}
        self.assertIn("a1", ids)

    def test_repair_and_recall_block_dispatch_and_transfer(self):
        self.register_flow()
        self.ledger.ingest_commands([
            {"type": "scan_return", "asset_id": "a1", "at": T + 4 * D,
             "rental_id": "r1", "shipment_id": "s1", "warehouse_id": WH,
             "idem_key": "ret"},
            {"type": "start_repair", "asset_id": "a1", "at": T + 5 * D,
             "repair_id": "f1", "idem_key": "rep"},
            {"type": "start_transfer", "asset_id": "a1", "at": T + 5 * D + H,
             "shipment_id": "s2", "from_warehouse": WH, "to_warehouse": "wh-2",
             "idem_key": "tr-during-repair"},
        ])
        self.assertTrue(any("阻断状态下不得发起转运" in r["reason"]
                            for r in self.ledger.rejected))
        self.assertFalse(any(x["asset_id"] == "a1"
                             for x in self.ledger.dispatch_eligible(T + 5 * D + 2 * H)))
        # 维修完成后才可召回清除/再调度
        self.ledger.ingest_commands([
            {"type": "finish_repair", "asset_id": "a1", "at": T + 6 * D,
             "warehouse_id": WH, "idem_key": "rep-done"}])
        self.assertTrue(any(x["asset_id"] == "a1"
                            for x in self.ledger.dispatch_eligible(T + 6 * D + H)))

    def test_overlapping_confirmed_rentals_are_rejected(self):
        self.register_flow()
        # r1 占用 [T+D, T+4D)，与其重叠的新租期在归还前确认应被拒
        res = self.ledger.ingest_commands([
            {"type": "confirm_rental", "asset_id": "a1", "at": T + 3 * D,
             "rental_id": "r-overlap", "lessee": "x",
             "rental_start": T + 2 * D, "rental_end": T + 3 * D,
             "idem_key": "overlap"}])
        self.assertEqual(res[0]["status"], "accepted")
        self.assertTrue(any("互斥" in r["reason"] for r in self.ledger.rejected))

    # -- 结算：规则先算，双方确认 ------------------------------------------

    def test_discrepancy_is_rule_computed_then_confirmed_by_both(self):
        self.register_flow()
        self.ledger.ingest_commands([
            {"type": "scan_return", "asset_id": "a1", "at": T + 4 * D,
             "rental_id": "r1", "shipment_id": "s1", "warehouse_id": WH,
             "idem_key": "ret"},
            {"type": "inspect", "asset_id": "a1", "at": T + 4 * D + H,
             "inspection_id": "i2", "warehouse_id": WH,
             "standard_id": DEFAULT_STANDARD["standard_id"], "standard_version": 1,
             "rules_id": DEFAULT_RULES["rules_id"], "rules_version": 1,
             "items": base_items("i2", {"lens": 2, "body": 1}), "idem_key": "in2"},
            {"type": "compute_discrepancy", "asset_id": "a1", "at": T + 4 * D + 2 * H,
             "discrepancy_id": "d1", "rental_id": "r1",
             "outbound_inspection_id": "i1", "return_inspection_id": "i2",
             "idem_key": "d1"},
        ])
        def rental():
            return next(r for r in self.ledger.asset("a1")["rentals"]
                        if r["rental_id"] == "r1")

        # lens 0->2: 500-0=500; body 0->1: 200；合计 700，且仍为提案
        self.assertEqual(rental()["settlement"]["total_charge"], 700)
        self.assertEqual(rental()["settlement"]["status"], "proposed")
        self.ledger.ingest_commands([
            {"type": "confirm_settlement", "asset_id": "a1", "at": T + 4 * D + 3 * H,
             "rental_id": "r1", "party": "lessee", "accepted": True, "idem_key": "cf-l"},
        ])
        self.assertEqual(rental()["settlement"]["status"], "proposed")  # 仅一方仍不生效
        self.ledger.ingest_commands([
            {"type": "confirm_settlement", "asset_id": "a1", "at": T + 4 * D + 4 * H,
             "rental_id": "r1", "party": "consignor", "accepted": True, "idem_key": "cf-c"},
        ])
        self.assertEqual(rental()["settlement"]["status"], "final")
        self.assertEqual(rental()["status"], "settled")

    def test_rejection_by_one_party_opens_dispute_and_blocks_confirm(self):
        ledger = Ledger()
        # 直接构造：归还->差异->承租人拒绝
        self.register_flow(ledger)
        ledger.ingest_commands([
            {"type": "scan_return", "asset_id": "a1", "at": T + 4 * D,
             "rental_id": "r1", "shipment_id": "s1", "warehouse_id": WH,
             "idem_key": "ret2"},
            {"type": "inspect", "asset_id": "a1", "at": T + 4 * D + H,
             "inspection_id": "i2b", "warehouse_id": WH,
             "standard_id": DEFAULT_STANDARD["standard_id"], "standard_version": 1,
             "rules_id": DEFAULT_RULES["rules_id"], "rules_version": 1,
             "items": base_items("i2b", {"lens": 1}), "idem_key": "in2b"},
            {"type": "compute_discrepancy", "asset_id": "a1", "at": T + 4 * D + 2 * H,
             "discrepancy_id": "d2", "rental_id": "r1",
             "outbound_inspection_id": "i1", "return_inspection_id": "i2b",
             "idem_key": "d2"},
            {"type": "confirm_settlement", "asset_id": "a1", "at": T + 4 * D + 3 * H,
             "rental_id": "r1", "party": "lessee", "accepted": False, "idem_key": "rej"},
        ])
        r1 = next(r for r in ledger.asset("a1")["rentals"] if r["rental_id"] == "r1")
        self.assertEqual(r1["status"], "disputed")
        # 另一方不能把已拒绝提案翻成 final
        ledger.ingest_commands([
            {"type": "confirm_settlement", "asset_id": "a1", "at": T + 4 * D + 4 * H,
             "rental_id": "r1", "party": "consignor", "accepted": True,
             "idem_key": "cf-after-reject"}])
        self.assertNotEqual(r1["settlement"]["status"], "final")

    # -- 幂等与乱序收敛 ----------------------------------------------------

    def test_duplicate_commands_and_callbacks_collapse(self):
        l1 = Ledger()
        self.register_flow(l1)
        d1 = l1.digest()
        # 同幂等键再投一遍：全部 duplicate，digest 不变
        results = l1.ingest_commands(_build_flow_commands())
        self.assertTrue(all(r["status"] == "duplicate" for r in results))
        self.assertEqual(l1.digest(), d1)
        # 未带同一幂等键的重复物流回调：事件落账但应用幂等，不产生重复区间
        dup_scans = [
            {"type": "confirm_delivery", "asset_id": "a1", "at": T + D + H,
             "shipment_id": "s1", "idem_key": "del-dup-1"},
            {"type": "confirm_delivery", "asset_id": "a1", "at": T + D + H,
             "shipment_id": "s1", "idem_key": "del-dup-2"},
        ]
        l1.ingest_commands(dup_scans)
        self.assertEqual(len(l1.asset("a1")["usage"]), 1)
        # 与“从头就带着这些重复回调”的账本状态完全一致
        l2 = Ledger()
        l2.ingest_commands(_build_flow_commands() + dup_scans)
        self.assertEqual(canon(_state_view(l1)), canon(_state_view(l2)))

    def test_shuffled_chunked_and_repeated_delivery_converge(self):
        commands = scenario_commands()
        ledgers = []
        for seed, chunked in ((None, False), (1, False), (2, False), (9, True), (13, True)):
            lg = Ledger()
            _feed(lg, commands, shuffle_seed=seed, chunked=chunked)
            ledgers.append(lg)
        digests = {lg.digest() for lg in ledgers}
        self.assertEqual(len(digests), 1)
        # 时点视图两两一致
        rng = random.Random(7)
        for _ in range(30):
            at = rng.randrange(T - H, T + 35 * D)
            views = {canon(lg.snapshot_at("cam-1001", at)) for lg in ledgers}
            self.assertEqual(len(views), 1, f"时点 {at} 视图发散")
        self.assertEqual({canon(lg.stats()) for lg in ledgers},
                         {canon(ledgers[0].stats())})

    def test_out_of_order_transfer_scans_normalize_to_one_result(self):
        # 注册入仓后发起转运，arrival 先于 pickup 投递且重复
        cmds = [
            {"type": "register_asset", "asset_id": "a1", "at": T,
             "category": "camera", "model": "M", "owner": "o", "idem_key": "r"},
            {"type": "consign", "asset_id": "a1", "at": T + H,
             "warehouse_id": "wh-a", "idem_key": "c"},
            {"type": "start_transfer", "asset_id": "a1", "at": T + 2 * H,
             "shipment_id": "sh", "from_warehouse": "wh-a", "to_warehouse": "wh-b",
             "idem_key": "t"},
        ]
        scans = [
            ("arrival", T + 5 * H, "arr1"),
            ("arrival", T + 5 * H, "arr2"),
            ("in_transit", T + 4 * H, "it"),
            ("pickup", T + 3 * H, "pk"),
            ("pickup", T + 3 * H, "pk2"),
        ]
        for order in (scans, list(reversed(scans))):
            lg = Ledger()
            lg.ingest_commands(cmds + [
                {"type": "transfer_scan", "asset_id": "a1", "at": at,
                 "shipment_id": "sh", "node": node, "idem_key": key}
                for node, at, key in order])
            state = lg.asset("a1")
            self.assertEqual(state["location"],
                             {"type": "warehouse", "place": "wh-b"})
            self.assertIsNone(state["in_transit"])
            self.assertEqual(state["shipments"]["sh"]["events"],
                             ["pickup", "in_transit", "arrival"])

    # -- 统计只从流转推导 --------------------------------------------------

    def test_stats_are_derived_and_client_cannot_report_totals(self):
        commands = scenario_commands()
        lg = Ledger()
        _feed(lg, commands, shuffle_seed=3)
        stats = lg.stats()
        self.assertTrue(stats["derived_from_events"])
        self.assertEqual(stats["assets"], 2)
        self.assertEqual(stats["rentals_total"], 3)
        # 不存在任何"上报汇总"命令：未知命令直接 invalid
        bad = lg.ingest_commands([
            {"type": "report_ewaste_total", "asset_id": "cam-1001",
             "at": T + 100 * D, "ewaste_avoided_kg": 999, "idem_key": "cheat"}])
        self.assertEqual(bad[0]["status"], "invalid")
        after = lg.stats()
        self.assertEqual(after, stats)

    # -- 规范化 ------------------------------------------------------------

    def test_canon_is_order_independent(self):
        self.assertEqual(canon({"a": 1, "b": 2}), canon({"b": 2, "a": 1}))
        self.assertEqual(digest_of({"x": [1, 2]}), digest_of({"x": [1, 2]}))


def _state_view(ledger):
    """剥离易变外层，只比较资产事实视图。"""
    return {aid: ledger.asset(aid) for aid in ("a1",) if ledger.asset(aid)}


def _build_flow_commands():
    """register_flow 使用的命令集合（用于重复投递断言）。"""
    std = (DEFAULT_STANDARD["standard_id"], DEFAULT_STANDARD["version"])
    rules = (DEFAULT_RULES["rules_id"], DEFAULT_RULES["version"])
    return [
        {"type": "register_asset", "asset_id": "a1", "at": T,
         "category": "camera", "model": "M1", "owner": "alice", "idem_key": "reg"},
        {"type": "consign", "asset_id": "a1", "at": T + H, "warehouse_id": WH,
         "idem_key": "consign"},
        {"type": "inspect", "asset_id": "a1", "at": T + 2 * H,
         "inspection_id": "i1", "warehouse_id": WH,
         "standard_id": std[0], "standard_version": std[1],
         "rules_id": rules[0], "rules_version": rules[1],
         "items": base_items("i1"), "idem_key": "in1"},
        {"type": "confirm_rental", "asset_id": "a1", "at": T + 3 * H,
         "rental_id": "r1", "lessee": "bob",
         "rental_start": T + D, "rental_end": T + 4 * D,
         "deposit_free": True, "deposit_policy_snapshot": {"free": True, "tier": "A"},
         "idem_key": "r1c"},
        {"type": "scan_outbound", "asset_id": "a1", "at": T + D,
         "rental_id": "r1", "shipment_id": "s1", "idem_key": "out"},
        {"type": "confirm_delivery", "asset_id": "a1", "at": T + D + H,
         "shipment_id": "s1", "idem_key": "del"},
    ]


if __name__ == "__main__":
    unittest.main()
