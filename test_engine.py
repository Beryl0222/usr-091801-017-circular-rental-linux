"""领域引擎验证：身份、权利区间、验机、结算、物流收敛、免押、统计与审计。"""

import hashlib
import random
import unittest
from datetime import timedelta

from rental import CommandRejected, Engine, asset_identity, parse_instant, format_instant

ITEMS_V1 = [
    {"code": "lens", "name": "镜头", "charge_cents": 12000},
    {"code": "body", "name": "机身", "charge_cents": 30000},
    {"code": "battery", "name": "电池", "charge_cents": 6000},
]
ITEMS_V2 = ITEMS_V1 + [{"code": "sensor", "name": "传感器", "charge_cents": 40000}]


class Clock:
    """严格递增的时间源，保证因果顺序与规范顺序一致。"""

    def __init__(self, start="2026-01-01T00:00:00Z"):
        self.t = parse_instant(start)

    def tick(self, hours=1):
        self.t += timedelta(hours=hours)
        return format_instant(self.t)


def evidence(*parts):
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def record_inspection(engine, inspection_id, asset_id, version, context, outcomes, at):
    results = [
        {"code": code, "outcome": outcome, "evidence_hash": evidence(inspection_id, code)}
        for code, outcome in outcomes.items()
    ]
    engine.execute(
        "record_inspection",
        {
            "inspection_id": inspection_id,
            "asset_id": asset_id,
            "standard_version": version,
            "context": context,
            "results": results,
            "inspector": "inspector-1",
            "at": at,
        },
    )


def boot_engine():
    """标准 + 资产 + 入仓 + 免押政策的最小环境。"""
    engine = Engine()
    clock = Clock()
    engine.execute(
        "publish_standard",
        {"category": "camera", "version": "v1", "items": ITEMS_V1, "effective_from": "2026-01-01T00:00:00Z", "at": clock.tick()},
    )
    engine.execute(
        "update_waiver_policy",
        {"subject": "renter-bob", "eligible": True, "effective_from": "2026-01-01T00:00:00Z", "at": clock.tick()},
    )
    asset_id = asset_identity("SN-001", "alice")
    engine.execute(
        "register_asset",
        {"serial_no": "SN-001", "model": "X100", "category": "camera", "owner_id": "alice", "at": clock.tick()},
    )
    engine.execute("intake_asset", {"asset_id": asset_id, "warehouse_id": "WH-A", "at": clock.tick()})
    return engine, clock, asset_id


def run_rental_cycle(engine, clock, asset_id, rental_id, renter="renter-bob", return_outcomes=None, wh="WH-A"):
    """完整租期：出库验机 → 下单 → 确认 → 出库 → 交付 → 归还验机 → 归还。"""
    record_inspection(engine, f"INSP-OUT-{rental_id}", asset_id, "v1", "outbound",
                      {"lens": "pass", "body": "pass", "battery": "pass"}, clock.tick())
    engine.execute("create_rental", {"rental_id": rental_id, "asset_id": asset_id, "renter_id": renter,
                                     "start": clock.tick(), "end": clock.tick(), "at": clock.tick()})
    engine.execute("confirm_rental", {"rental_id": rental_id, "at": clock.tick()})
    engine.execute("outbound_rental", {"rental_id": rental_id, "inspection_id": f"INSP-OUT-{rental_id}", "at": clock.tick()})
    engine.execute("deliver_rental", {"rental_id": rental_id, "at": clock.tick()})
    outcomes = return_outcomes or {"lens": "pass", "body": "pass", "battery": "pass"}
    record_inspection(engine, f"INSP-RET-{rental_id}", asset_id, "v1", "return", outcomes, clock.tick())
    engine.execute("return_rental", {"rental_id": rental_id, "inspection_id": f"INSP-RET-{rental_id}",
                                     "warehouse_id": wh, "at": clock.tick()})


class AssetIdentityTest(unittest.TestCase):
    def test_identity_is_stable_and_never_reused(self):
        engine, clock, asset_id = boot_engine()
        # 同一实物重复登记：幂等，不产生新事件
        before = len(engine.store)
        events = engine.execute(
            "register_asset",
            {"serial_no": "SN-001", "model": "X100", "category": "camera", "owner_id": "alice", "at": clock.tick()},
        )
        self.assertEqual(events, [])
        self.assertEqual(len(engine.store), before)
        # 同一序列号绑定他人：拒绝
        with self.assertRaises(CommandRejected):
            engine.execute(
                "register_asset",
                {"serial_no": "SN-001", "model": "X100", "category": "camera", "owner_id": "mallory", "at": clock.tick()},
            )
        # 报废后身份仍占用，不可复用
        engine.execute("scrap_asset", {"asset_id": asset_id, "reason": "进水报废", "at": clock.tick()})
        with self.assertRaises(CommandRejected):
            engine.execute(
                "register_asset",
                {"serial_no": "SN-001", "model": "X100", "category": "camera", "owner_id": "alice", "at": clock.tick()},
            )
        # 不同序列号得到不同身份
        other = asset_identity("SN-002", "alice")
        self.assertNotEqual(asset_id, other)


class CustodyIntervalTest(unittest.TestCase):
    def test_intervals_are_mutually_exclusive_and_contiguous(self):
        engine, clock, asset_id = boot_engine()
        run_rental_cycle(engine, clock, asset_id, "R-1")
        intervals = engine.ledger.custody[asset_id]
        roles = [i["role"] for i in intervals]
        self.assertEqual(roles, ["consignor", "platform", "renter", "platform"])
        for prev, nxt in zip(intervals, intervals[1:]):
            self.assertEqual(prev["end"], nxt["start"])  # 连续无缝
        # 半开区间：边界时刻归属下一段
        intake_at = intervals[1]["start"]
        self.assertEqual(engine.query("custody_at", {"asset_id": asset_id, "at": intake_at})["custody"]["role"], "platform")
        before_intake = format_instant(parse_instant(intake_at) - timedelta(seconds=1))
        self.assertEqual(engine.query("custody_at", {"asset_id": asset_id, "at": before_intake})["custody"]["role"], "consignor")
        # 在租期间使用权在承租人
        delivered_at = intervals[2]["start"]
        mid_rent = format_instant(parse_instant(delivered_at) + timedelta(seconds=1))
        custody = engine.query("custody_at", {"asset_id": asset_id, "at": mid_rent})["custody"]
        self.assertEqual((custody["role"], custody["holder_id"]), ("renter", "renter-bob"))


class InspectionStandardTest(unittest.TestCase):
    def test_versioned_standards_and_evidence(self):
        engine, clock, asset_id = boot_engine()
        engine.execute(
            "publish_standard",
            {"category": "camera", "version": "v2", "items": ITEMS_V2, "effective_from": "2026-02-01T00:00:00Z", "at": clock.tick()},
        )
        q = lambda at: engine.query("standard_at", {"category": "camera", "at": at})["standard"]
        self.assertEqual(q("2026-01-15T00:00:00Z")["version"], "v1")
        self.assertEqual(q("2026-02-01T00:00:00Z")["version"], "v2")
        self.assertEqual(q("2026-03-01T00:00:00Z")["version"], "v2")
        # 证据哈希必须是 64 位十六进制
        with self.assertRaises(CommandRejected):
            engine.execute(
                "record_inspection",
                {
                    "inspection_id": "BAD-1",
                    "asset_id": asset_id,
                    "standard_version": "v1",
                    "context": "intake",
                    "results": [{"code": c, "outcome": "pass", "evidence_hash": "not-a-hash"} for c in ("lens", "body", "battery")],
                    "inspector": "i",
                    "at": clock.tick(),
                },
            )
        # 正常报告：结果与证据哈希完整入档
        record_inspection(engine, "INSP-OK", asset_id, "v1", "intake",
                          {"lens": "pass", "body": "fail", "battery": "pass"}, clock.tick())
        report = engine.ledger.inspections["INSP-OK"]
        self.assertEqual(report["standard_version"], "v1")
        self.assertTrue(all(len(r["evidence_hash"]) == 64 for r in report["results"]))
        # 报告缺项：拒绝
        with self.assertRaises(CommandRejected):
            record_inspection(engine, "INSP-PARTIAL", asset_id, "v1", "intake", {"lens": "pass"}, clock.tick())


class SettlementTest(unittest.TestCase):
    def test_rule_computes_then_dual_confirmation_then_settlement(self):
        engine, clock, asset_id = boot_engine()
        run_rental_cycle(engine, clock, asset_id, "R-1",
                         return_outcomes={"lens": "fail", "body": "pass", "battery": "fail"})
        rental = engine.ledger.rentals["R-1"]
        diff = rental["difference"]
        self.assertEqual(diff["rule_version"], "diff-v1")
        self.assertEqual({i["code"] for i in diff["items"]}, {"lens", "battery"})
        self.assertEqual(diff["total_cents"], 12000 + 6000)  # 规则计算，非客户端上报
        # 未双方确认不得结算
        with self.assertRaises(CommandRejected):
            engine.execute("settle_rental", {"rental_id": "R-1", "at": clock.tick()})
        engine.execute("confirm_difference", {"rental_id": "R-1", "party": "platform", "at": clock.tick()})
        with self.assertRaises(CommandRejected):
            engine.execute("settle_rental", {"rental_id": "R-1", "at": clock.tick()})
        engine.execute("confirm_difference", {"rental_id": "R-1", "party": "renter", "at": clock.tick()})
        events = engine.execute("settle_rental", {"rental_id": "R-1", "at": clock.tick()})
        self.assertEqual(events[0].payload["total_cents"], 18000)
        self.assertEqual(engine.ledger.rentals["R-1"]["status"], "settled")

    def test_no_damage_means_zero_difference(self):
        engine, clock, asset_id = boot_engine()
        run_rental_cycle(engine, clock, asset_id, "R-1")
        self.assertEqual(engine.ledger.rentals["R-1"]["difference"]["total_cents"], 0)


class DisputeTest(unittest.TestCase):
    def test_dispute_blocks_rerental_and_dispatch_until_resolved(self):
        engine, clock, asset_id = boot_engine()
        run_rental_cycle(engine, clock, asset_id, "R-1",
                         return_outcomes={"lens": "fail", "body": "pass", "battery": "pass"})
        dispute_at = clock.tick()
        engine.execute("open_dispute", {"rental_id": "R-1", "reason": "承租人不认可镜头定损", "at": dispute_at})
        # 争议期间：不可再租、不可调度出库
        with self.assertRaises(CommandRejected) as ctx:
            engine.execute("create_rental", {"rental_id": "R-2", "asset_id": asset_id, "renter_id": "renter-carol",
                                             "start": clock.tick(), "end": clock.tick(), "at": clock.tick()})
        self.assertIn("dispute_open", ctx.exception.reasons)
        with self.assertRaises(CommandRejected) as ctx:
            engine.execute("create_shipment", {"shipment_id": "SH-X", "asset_id": asset_id,
                                               "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
        self.assertIn("dispute_open", ctx.exception.reasons)
        eligibility = engine.query("circulation_eligibility", {"asset_id": asset_id, "at": clock.tick()})
        self.assertFalse(eligibility["eligible"])
        self.assertIn("dispute_open", eligibility["reasons"])
        # 争议期间也不能结算
        with self.assertRaises(CommandRejected):
            engine.execute("settle_rental", {"rental_id": "R-1", "at": clock.tick()})
        # 解决后恢复
        engine.execute("resolve_dispute", {"rental_id": "R-1", "resolution": "按规则减半赔付", "at": clock.tick()})
        self.assertTrue(engine.query("circulation_eligibility", {"asset_id": asset_id, "at": clock.tick()})["eligible"])
        # 争议窗口内的历史时间点仍然不可调度
        during = engine.query("circulation_eligibility", {"asset_id": asset_id, "at": dispute_at})
        self.assertFalse(during["eligible"])


class LogisticsTest(unittest.TestCase):
    def _shipped_engine(self):
        engine, clock, asset_id = boot_engine()
        engine.execute("create_shipment", {"shipment_id": "SH-1", "asset_id": asset_id,
                                           "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
        return engine, clock, asset_id

    def test_duplicate_and_out_of_order_scans_converge(self):
        engine, clock, asset_id = self._shipped_engine()
        t1, t2, t3 = clock.tick(), clock.tick(), clock.tick()
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": t1})
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": t2})
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "delivered", "warehouse_id": "WH-B", "at": t3})
        # 重复回调：幂等
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": t2})
        ship = engine.ledger.shipments["SH-1"]
        self.assertEqual((ship["status"], ship["location"]), ("delivered", "WH-B"))
        self.assertEqual(len(ship["scans"]), 3)
        # 乱序重放 + 重复投递 → 同一账本
        events = engine.store.canonical()
        for seed in range(5):
            rng = random.Random(seed)
            shuffled = events[:]
            rng.shuffle(shuffled)
            shuffled += rng.sample(events, 3)  # 重复投递
            replayed = engine.replay(shuffled)
            self.assertEqual(replayed.digest(), engine.ledger.digest())
        # 送达后保管人更新为目的仓，可继续下一程转运
        self.assertEqual(engine.ledger.assets[asset_id]["warehouse_id"], "WH-B")
        engine.execute("create_shipment", {"shipment_id": "SH-2", "asset_id": asset_id,
                                           "from_warehouse": "WH-B", "to_warehouse": "WH-C", "at": clock.tick()})
        self.assertIn("SH-2", engine.ledger.shipments)

    def test_backdated_event_triggers_refold_and_stays_convergent(self):
        engine, clock, asset_id = self._shipped_engine()
        t1, t2 = clock.tick(), clock.tick()
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": t1})
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": t2})
        # 补传一个早于运单创建时间的扫码：插入规范顺序中间，触发整体重折叠并被拒绝
        backdated = format_instant(parse_instant(t1) - timedelta(hours=2))
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": backdated})
        reasons = {v["reason"] for v in engine.ledger.violations}
        self.assertTrue(reasons & {"shipment_unknown", "scan_before_creation"})
        self.assertEqual(engine.ledger.shipments["SH-1"]["status"], "arrived")
        self.assertTrue(engine.query("verify_replay")["converged"])
        # 合法的中间时刻补传（到达前的离仓重扫）：重折叠后状态由最晚扫码决定
        mid = format_instant(parse_instant(t1) + timedelta(minutes=30))
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": mid})
        self.assertEqual(engine.ledger.shipments["SH-1"]["status"], "arrived")
        self.assertEqual(len(engine.ledger.shipments["SH-1"]["scans"]), 3)
        self.assertTrue(engine.query("verify_replay")["converged"])

    def test_scan_cannot_cross_maintenance_recall_preservation(self):
        engine, clock, asset_id = self._shipped_engine()
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": clock.tick()})
        # 维修期间扫码不得推进物流
        engine.execute("start_maintenance", {"asset_id": asset_id, "reason": "防抖组件更换", "at": clock.tick()})
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": clock.tick()})
        ship = engine.ledger.shipments["SH-1"]
        self.assertEqual(ship["status"], "in_transit")
        self.assertTrue(any(v["reason"] == "scan_while_blocked" for v in engine.ledger.violations))
        # 维修结束后扫码生效
        engine.execute("end_maintenance", {"asset_id": asset_id, "at": clock.tick()})
        engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": clock.tick()})
        self.assertEqual(engine.ledger.shipments["SH-1"]["status"], "arrived")

    def test_blocked_states_reject_dispatch(self):
        for blocker in (
            lambda e, c, a: e.execute("start_maintenance", {"asset_id": a, "reason": "r", "at": c.tick()}),
            lambda e, c, a: e.execute("issue_recall", {"asset_id": a, "reason": "r", "at": c.tick()}),
            lambda e, c, a: e.execute("start_preservation", {"asset_id": a, "reason": "r", "at": c.tick()}),
        ):
            engine, clock, asset_id = boot_engine()
            blocker(engine, clock, asset_id)
            with self.assertRaises(CommandRejected):
                engine.execute("create_shipment", {"shipment_id": "SH-B", "asset_id": asset_id,
                                                   "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
        # 在租（使用权在承租人）同样不可调度
        engine, clock, asset_id = boot_engine()
        record_inspection(engine, "INSP-OUT-R9", asset_id, "v1", "outbound",
                          {"lens": "pass", "body": "pass", "battery": "pass"}, clock.tick())
        engine.execute("create_rental", {"rental_id": "R-9", "asset_id": asset_id, "renter_id": "renter-bob",
                                         "start": clock.tick(), "end": clock.tick(), "at": clock.tick()})
        engine.execute("confirm_rental", {"rental_id": "R-9", "at": clock.tick()})
        engine.execute("outbound_rental", {"rental_id": "R-9", "inspection_id": "INSP-OUT-R9", "at": clock.tick()})
        engine.execute("deliver_rental", {"rental_id": "R-9", "at": clock.tick()})
        with self.assertRaises(CommandRejected) as ctx:
            engine.execute("create_shipment", {"shipment_id": "SH-C", "asset_id": asset_id,
                                               "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
        self.assertIn("rental_active", ctx.exception.reasons)


class WaiverSnapshotTest(unittest.TestCase):
    def test_policy_change_only_affects_unconfirmed_rentals(self):
        engine, clock, asset_id = boot_engine()
        # 政策：bob 自 1 日起免押
        engine.execute("create_rental", {"rental_id": "R-1", "asset_id": asset_id, "renter_id": "renter-bob",
                                         "start": clock.tick(), "end": clock.tick(), "at": clock.tick()})
        engine.execute("confirm_rental", {"rental_id": "R-1", "at": clock.tick()})
        self.assertTrue(engine.ledger.rentals["R-1"]["deposit_waived"])
        # 政策收紧：3 日起 bob 不再免押
        engine.execute("update_waiver_policy", {"subject": "renter-bob", "eligible": False,
                                                "effective_from": "2026-01-03T00:00:00Z", "at": clock.tick()})
        # 已确认的 R-1 不受影响
        self.assertTrue(engine.ledger.rentals["R-1"]["deposit_waived"])
        # 未确认的新租约适用新政策（时钟拨到 1 月 3 日新政生效之后）
        engine.execute("cancel_rental", {"rental_id": "R-1", "at": clock.tick()})
        clock.tick(hours=72)
        engine.execute("create_rental", {"rental_id": "R-2", "asset_id": asset_id, "renter_id": "renter-bob",
                                         "start": clock.tick(), "end": clock.tick(), "at": clock.tick()})
        engine.execute("confirm_rental", {"rental_id": "R-2", "at": clock.tick()})
        self.assertFalse(engine.ledger.rentals["R-2"]["deposit_waived"])
        # 无政策记录者默认不免押
        self.assertFalse(engine.ledger.waiver_eligible("renter-nobody", clock.tick()))


class DerivedStatsTest(unittest.TestCase):
    def test_stats_are_derived_from_events_only(self):
        engine, clock, asset_id = boot_engine()
        # 完成一单（租 2 天）
        run_rental_cycle(engine, clock, asset_id, "R-1")
        # 一次维修延寿
        engine.execute("start_maintenance", {"asset_id": asset_id, "reason": "快门组件", "at": clock.tick()})
        engine.execute("end_maintenance", {"asset_id": asset_id, "at": clock.tick()})
        ewaste = engine.query("ewaste_stats")
        self.assertEqual(ewaste["completed_rentals"], 1)
        self.assertEqual(ewaste["refurbishments"], 1)
        self.assertEqual(ewaste["avoided_waste_kg"], 1 * 0.5 + 1 * 2.0)
        self.assertEqual(ewaste["source"], "derived_from_events")
        life = engine.query("lifecycle_stats")
        entry = life["assets"][0]
        self.assertEqual(entry["rentals_completed"], 1)
        self.assertEqual(entry["maintenance_episodes"], 1)
        self.assertGreater(entry["rental_days"], 0)
        self.assertEqual(set(entry["custody_days"]), {"consignor", "platform", "renter"})
        # 不存在任何接受客户端上报统计值的命令
        with self.assertRaises(CommandRejected):
            engine.execute("report_ewaste", {"avoided_waste_kg": 999})


def build_busy_engine():
    """多资产、多状态交织的繁忙账本，用于审计模拟。"""
    engine, clock, asset1 = boot_engine()
    # 资产 1：完整租期 + 定损 + 双方确认 + 结算
    run_rental_cycle(engine, clock, asset1, "R-1",
                     return_outcomes={"lens": "fail", "body": "pass", "battery": "pass"})
    engine.execute("confirm_difference", {"rental_id": "R-1", "party": "platform", "at": clock.tick()})
    engine.execute("confirm_difference", {"rental_id": "R-1", "party": "renter", "at": clock.tick()})
    engine.execute("settle_rental", {"rental_id": "R-1", "at": clock.tick()})
    # 资产 1：跨仓转运（含一次重复回调）
    engine.execute("create_shipment", {"shipment_id": "SH-1", "asset_id": asset1,
                                       "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
    departed = clock.tick()
    engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": departed})
    engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "departed", "warehouse_id": "WH-A", "at": departed})
    engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "arrived", "warehouse_id": "WH-B", "at": clock.tick()})
    engine.execute("record_scan", {"shipment_id": "SH-1", "scan_type": "delivered", "warehouse_id": "WH-B", "at": clock.tick()})
    # 资产 2：入仓 → 维修 → 租出 → 归还 → 争议中
    asset2 = asset_identity("SN-002", "alice")
    engine.execute("register_asset", {"serial_no": "SN-002", "model": "X100", "category": "camera",
                                      "owner_id": "alice", "at": clock.tick()})
    engine.execute("intake_asset", {"asset_id": asset2, "warehouse_id": "WH-A", "at": clock.tick()})
    engine.execute("start_maintenance", {"asset_id": asset2, "reason": "镀膜修复", "at": clock.tick()})
    engine.execute("end_maintenance", {"asset_id": asset2, "at": clock.tick()})
    run_rental_cycle(engine, clock, asset2, "R-2",
                     return_outcomes={"lens": "pass", "body": "fail", "battery": "pass"})
    engine.execute("open_dispute", {"rental_id": "R-2", "reason": "机身划痕责任争议", "at": clock.tick()})
    # 资产 3：证据保全中
    asset3 = asset_identity("SN-003", "alice")
    engine.execute("register_asset", {"serial_no": "SN-003", "model": "X200", "category": "camera",
                                      "owner_id": "alice", "at": clock.tick()})
    engine.execute("intake_asset", {"asset_id": asset3, "warehouse_id": "WH-C", "at": clock.tick()})
    engine.execute("start_preservation", {"asset_id": asset3, "reason": "司法取证", "at": clock.tick()})
    return engine, clock, (asset1, asset2, asset3)


class SupervisorAuditTest(unittest.TestCase):
    """资产主管视角：随机时点查询 + 打乱/重复物流事件 → 两次计算必须收敛。"""

    def test_random_point_in_time_queries_are_consistent(self):
        engine, clock, (asset1, asset2, asset3) = build_busy_engine()
        rng = random.Random(42)
        start = parse_instant("2026-01-01T00:00:00Z")
        end = parse_instant(clock.tick())
        for _ in range(50):
            at = format_instant(start + timedelta(seconds=rng.randrange(0, int((end - start).total_seconds()))))
            for asset_id in (asset1, asset2, asset3):
                via_query = engine.query("custody_at", {"asset_id": asset_id, "at": at})["custody"]
                via_prefix = engine.ledger_at(at).custody_at(asset_id, at)
                self.assertEqual(via_query, via_prefix)
                # 任意时刻每条资产的保管区间至多一条（互斥）
                intervals = engine.ledger_at(at).custody.get(asset_id, [])
                open_at = [i for i in intervals
                           if parse_instant(i["start"]) <= parse_instant(at)
                           and (i["end"] is None or parse_instant(at) < parse_instant(i["end"]))]
                self.assertLessEqual(len(open_at), 1)
            # 验机标准与流转资格在两种计算路径下一致
            self.assertEqual(
                engine.query("standard_at", {"category": "camera", "at": at})["standard"],
                engine.ledger_at(at).standard_at("camera", at),
            )
            eligibility = engine.query("circulation_eligibility", {"asset_id": asset2, "at": at})
            self.assertEqual(eligibility["reasons"], engine.ledger_at(at).blocked_reasons(asset2, at))

    def test_shuffled_and_duplicated_events_converge_to_same_ledger(self):
        engine, _, _ = build_busy_engine()
        events = engine.store.canonical()
        scans = [e for e in events if e.type == "shipment_scan"]
        self.assertTrue(scans)
        for seed in range(10):
            rng = random.Random(seed)
            shuffled = events[:]
            rng.shuffle(shuffled)
            shuffled += scans * 2  # 物流回调重复投递
            replayed = engine.replay(shuffled)
            self.assertEqual(replayed.digest(), engine.ledger.digest())
            self.assertEqual(replayed.violations, engine.ledger.violations)
        self.assertTrue(engine.query("verify_replay")["converged"])

    def test_disputed_asset_is_never_dispatchable(self):
        engine, clock, (_, asset2, asset3) = build_busy_engine()
        rental = engine.ledger.rentals["R-2"]
        opened = parse_instant(rental["dispute"]["opened_at"])
        # 争议窗口内的每个采样时刻：资格查询拒绝 + 调度命令拒绝
        for hours in (0, 1, 6, 24):
            at = format_instant(opened + timedelta(hours=hours))
            self.assertIn("dispute_open", engine.query("circulation_eligibility", {"asset_id": asset2, "at": at})["reasons"])
        with self.assertRaises(CommandRejected):
            engine.execute("create_shipment", {"shipment_id": "SH-D", "asset_id": asset2,
                                               "from_warehouse": "WH-A", "to_warehouse": "WH-B", "at": clock.tick()})
        # 证据保全中的资产 3 同样不可调度
        with self.assertRaises(CommandRejected):
            engine.execute("create_shipment", {"shipment_id": "SH-E", "asset_id": asset3,
                                               "from_warehouse": "WH-C", "to_warehouse": "WH-B", "at": clock.tick()})


if __name__ == "__main__":
    unittest.main()
