"""资产主管验收场景：同一批物流事件打乱、重复投递，两次计算必须收敛。

场景覆盖：寄租入仓 -> 有版本验机 -> 仓间转运次日达（乱序/重复扫码）->
免押出租与出库交付 -> 归还复检 -> 规则先算差异、双方确认结算 -> 二次出租
受损争议（证据保全冻结）-> 冻结期间的出租/转运/维修企图全部被拒。
"""

import random

from domain import (
    DEFAULT_MASS,
    DEFAULT_RULES,
    DEFAULT_STANDARD,
    Ledger,
    _as_ms,
)

T = 1_700_000_000_000
M = 60_000
HOUR = 3_600_000
DAY = 86_400_000

A = "cam-1001"
B = "cam-1002"
WH1 = "wh-shanghai"
WH2 = "wh-hangzhou"


def _ev_hash(tag):
    import hashlib

    return "sha256:" + hashlib.sha256(tag.encode()).hexdigest()


def build_commands():
    """返回完整命令流；含重复与乱序投递，调用方还会再洗牌。"""
    cmds = []

    def add(idem, at, **payload):
        payload["idem_key"] = idem
        payload["at"] = at
        cmds.append(payload)

    std = (DEFAULT_STANDARD["standard_id"], DEFAULT_STANDARD["version"])
    rules = (DEFAULT_RULES["rules_id"], DEFAULT_RULES["version"])

    # --- 资产 A：完整两轮出租，第二轮进入争议 -----------------------------
    add("a-register", T, type="register_asset", asset_id=A,
        category="camera", model="X-Cam Pro", owner="user-alice")
    add("a-consign", T + M, type="consign", asset_id=A, warehouse_id=WH1)
    add("a-in1", T + 2 * M, type="inspect", asset_id=A,
        inspection_id="IN-A-1", warehouse_id=WH1,
        standard_id=std[0], standard_version=std[1],
        rules_id=rules[0], rules_version=rules[1],
        items=[{"code": "shutter", "grade": 0, "evidence_hash": _ev_hash("a-in1-shutter")},
               {"code": "lens", "grade": 0, "evidence_hash": _ev_hash("a-in1-lens")},
               {"code": "body", "grade": 0, "evidence_hash": _ev_hash("a-in1-body")},
               {"code": "sensor", "grade": 0, "evidence_hash": _ev_hash("a-in1-sensor")},
               {"code": "function", "grade": 0, "evidence_hash": _ev_hash("a-in1-fn")}])

    # 仓间转运满足次日达：回调乱序（arrival 先投递）且重复
    add("a-transfer", T + DAY, type="start_transfer", asset_id=A,
        shipment_id="SH-A-0", from_warehouse=WH1, to_warehouse=WH2)
    add("a-sh0-arrival", T + DAY + 3 * HOUR, type="transfer_scan", asset_id=A,
        shipment_id="SH-A-0", node="arrival")  # 先于 pickup/in_transit 投递
    add("a-sh0-intransit", T + DAY + 2 * HOUR, type="transfer_scan", asset_id=A,
        shipment_id="SH-A-0", node="in_transit")
    add("a-sh0-pickup", T + DAY + HOUR, type="transfer_scan", asset_id=A,
        shipment_id="SH-A-0", node="pickup")
    add("a-sh0-pickup-dup", T + DAY + HOUR, type="transfer_scan", asset_id=A,
        shipment_id="SH-A-0", node="pickup")  # 重复回调
    add("a-sh0-arrival-dup", T + DAY + 3 * HOUR, type="transfer_scan", asset_id=A,
        shipment_id="SH-A-0", node="arrival")  # 重复回调

    # 第一轮出租：免押资格在确认瞬间快照
    add("a-r1-confirm", T + DAY + 5 * HOUR, type="confirm_rental", asset_id=A,
        rental_id="R-A-1", lessee="user-bob",
        rental_start=T + 2 * DAY, rental_end=T + 5 * DAY,
        deposit_free=True, deposit_policy_snapshot={"free": True, "tier": "A",
                                                    "source": "credit-650"})
    add("a-r1-out", T + 2 * DAY, type="scan_outbound", asset_id=A,
        rental_id="R-A-1", shipment_id="SH-A-1")
    add("a-r1-deliver", T + 2 * DAY + 2 * HOUR, type="confirm_delivery",
        asset_id=A, shipment_id="SH-A-1")
    add("a-r1-deliver-dup", T + 2 * DAY + 2 * HOUR, type="confirm_delivery",
        asset_id=A, shipment_id="SH-A-1")
    add("a-r1-return", T + 5 * DAY, type="scan_return", asset_id=A,
        rental_id="R-A-1", shipment_id="SH-A-1", warehouse_id=WH2)
    add("a-ret1", T + 5 * DAY + HOUR, type="inspect", asset_id=A,
        inspection_id="IN-A-RET-1", warehouse_id=WH2,
        standard_id=std[0], standard_version=std[1],
        rules_id=rules[0], rules_version=rules[1],
        items=[{"code": "shutter", "grade": 0, "evidence_hash": _ev_hash("a-ret1-shutter")},
               {"code": "lens", "grade": 1, "evidence_hash": _ev_hash("a-ret1-lens")},
               {"code": "body", "grade": 0, "evidence_hash": _ev_hash("a-ret1-body")},
               {"code": "sensor", "grade": 0, "evidence_hash": _ev_hash("a-ret1-sensor")},
               {"code": "function", "grade": 0, "evidence_hash": _ev_hash("a-ret1-fn")}])
    add("a-d1", T + 5 * DAY + 2 * HOUR, type="compute_discrepancy", asset_id=A,
        discrepancy_id="D-A-1", rental_id="R-A-1",
        outbound_inspection_id="IN-A-1", return_inspection_id="IN-A-RET-1")
    add("a-d1-lessee", T + 5 * DAY + 3 * HOUR, type="confirm_settlement",
        asset_id=A, rental_id="R-A-1", party="lessee", accepted=True)
    add("a-d1-consignor", T + 5 * DAY + 4 * HOUR, type="confirm_settlement",
        asset_id=A, rental_id="R-A-1", party="consignor", accepted=True)

    # 第二轮出租：归还后发现较重损耗，承租人否认差异 -> 争议冻结
    add("a-r2-confirm", T + 6 * DAY, type="confirm_rental", asset_id=A,
        rental_id="R-A-2", lessee="user-carol",
        rental_start=T + 8 * DAY, rental_end=T + 11 * DAY, deposit_free=False)
    add("a-r2-out", T + 8 * DAY, type="scan_outbound", asset_id=A,
        rental_id="R-A-2", shipment_id="SH-A-2")
    add("a-r2-deliver", T + 8 * DAY + 2 * HOUR, type="confirm_delivery",
        asset_id=A, shipment_id="SH-A-2")
    add("a-r2-return", T + 11 * DAY, type="scan_return", asset_id=A,
        rental_id="R-A-2", shipment_id="SH-A-2", warehouse_id=WH2)
    add("a-ret2", T + 11 * DAY + HOUR, type="inspect", asset_id=A,
        inspection_id="IN-A-RET-2", warehouse_id=WH2,
        standard_id=std[0], standard_version=std[1],
        rules_id=rules[0], rules_version=rules[1],
        items=[{"code": "shutter", "grade": 0, "evidence_hash": _ev_hash("a-ret2-shutter")},
               {"code": "lens", "grade": 2, "evidence_hash": _ev_hash("a-ret2-lens")},
               {"code": "body", "grade": 1, "evidence_hash": _ev_hash("a-ret2-body")},
               {"code": "sensor", "grade": 0, "evidence_hash": _ev_hash("a-ret2-sensor")},
               {"code": "function", "grade": 0, "evidence_hash": _ev_hash("a-ret2-fn")}])
    add("a-claim", T + 11 * DAY + 2 * HOUR, type="raise_claim", asset_id=A,
        claim_id="C-A-1", rental_id="R-A-2", reason="镜头损耗责任争议")
    add("a-d2", T + 11 * DAY + 3 * HOUR, type="compute_discrepancy", asset_id=A,
        discrepancy_id="D-A-2", rental_id="R-A-2",
        outbound_inspection_id="IN-A-RET-1", return_inspection_id="IN-A-RET-2")
    add("a-d2-lessee-reject", T + 11 * DAY + 4 * HOUR, type="confirm_settlement",
        asset_id=A, rental_id="R-A-2", party="lessee", accepted=False)

    # 冻结期间的违规企图：全部必须被确定性拒绝
    add("a-x-rental", T + 12 * DAY, type="confirm_rental", asset_id=A,
        rental_id="R-A-X", lessee="user-dan",
        rental_start=T + 13 * DAY, rental_end=T + 14 * DAY, deposit_free=True)
    add("a-x-transfer", T + 12 * DAY + HOUR, type="start_transfer", asset_id=A,
        shipment_id="SH-A-X", from_warehouse=WH2, to_warehouse=WH1)
    add("a-x-repair", T + 12 * DAY + 2 * HOUR, type="start_repair", asset_id=A,
        repair_id="RP-A-X", vendor="svc-shop-1")

    # --- 资产 B：安静在库，争议期内应始终可调度 ----------------------------
    add("b-register", T, type="register_asset", asset_id=B,
        category="camera", model="X-Cam Pro", owner="user-erin")
    add("b-consign", T + M, type="consign", asset_id=B, warehouse_id=WH2)
    add("b-in1", T + 2 * M, type="inspect", asset_id=B,
        inspection_id="IN-B-1", warehouse_id=WH2,
        standard_id=std[0], standard_version=std[1],
        rules_id=rules[0], rules_version=rules[1],
        items=[{"code": "shutter", "grade": 0, "evidence_hash": _ev_hash("b-in1-shutter")},
               {"code": "lens", "grade": 0, "evidence_hash": _ev_hash("b-in1-lens")},
               {"code": "body", "grade": 0, "evidence_hash": _ev_hash("b-in1-body")},
               {"code": "sensor", "grade": 0, "evidence_hash": _ev_hash("b-in1-sensor")},
               {"code": "function", "grade": 0, "evidence_hash": _ev_hash("b-in1-fn")}])
    # 免押政策变化后的新租期：资格变化只影响尚未确认的租期（此单按 false 快照）
    add("b-r1-confirm", T + 20 * DAY, type="confirm_rental", asset_id=B,
        rental_id="R-B-1", lessee="user-frank",
        rental_start=T + 30 * DAY, rental_end=T + 33 * DAY, deposit_free=False,
        deposit_policy_snapshot={"free": False, "reason": "policy-tightened"})

    return cmds


# 争议发生时刻起，A 永不可调度
CLAIM_AT = T + 11 * DAY + 2 * HOUR


def _feed(ledger, commands, shuffle_seed=None, chunked=False):
    rng = random.Random(shuffle_seed)
    stream = list(commands)
    # 再人为制造重复投递
    stream += [dict(c) for c in commands if c["idem_key"] in (
        "a-sh0-intransit", "a-r1-out", "a-d1-lessee", "b-consign")]
    if shuffle_seed is not None:
        rng.shuffle(stream)
    if chunked:
        # 乱序小块增量投递，模拟物流回调各自到达
        order = list(stream)
        rng.shuffle(order)
        for i in range(0, len(order), 3):
            ledger.ingest_commands(order[i:i + 3])
    else:
        ledger.ingest_commands(stream)


def run_selfcheck(verbose=True):
    commands = build_commands()

    ledger_ordered = Ledger()
    _feed(ledger_ordered, commands, shuffle_seed=None, chunked=False)

    ledger_shuffled = Ledger()
    _feed(ledger_shuffled, commands, shuffle_seed=20260919, chunked=False)

    ledger_chunked = Ledger()
    _feed(ledger_chunked, commands, shuffle_seed=77, chunked=True)

    d1, d2, d3 = ledger_ordered.digest(), ledger_shuffled.digest(), ledger_chunked.digest()
    assert d1 == d2 == d3, f"账本未收敛: {d1} != {d2} != {d3}"
    assert ledger_ordered.pending_count() == 0, "存在未决挂起事件"
    assert ledger_shuffled.pending_count() == 0
    assert ledger_chunked.pending_count() == 0

    # 拒绝集合一致：三条违规企图 + 任何确定性拒绝
    rejected_ids = sorted(r["event_id"] for r in ledger_ordered.rejected)
    assert rejected_ids == sorted(r["event_id"] for r in ledger_shuffled.rejected)
    rejected_reasons = {r["event_id"]: r["reason"] for r in ledger_ordered.rejected}

    # 随机时点查询：两个账本的责任/标准/资格视图必须逐点一致
    rng = random.Random(42)
    sample_times = [T - HOUR] + sorted(
        rng.randrange(T - HOUR, T + 40 * DAY) for _ in range(60)
    )
    a_never_scheduled_after_claim = True
    for at in sample_times:
        s1 = ledger_ordered.snapshot_at(A, at)
        s2 = ledger_shuffled.snapshot_at(A, at)
        s3 = ledger_chunked.snapshot_at(A, at)
        assert s1 == s2 == s3, f"时点 {at} 的 A 视图不一致"
        sb1 = ledger_ordered.snapshot_at(B, at)
        sb2 = ledger_shuffled.snapshot_at(B, at)
        assert sb1 == sb2, f"时点 {at} 的 B 视图不一致"
        if at >= CLAIM_AT and s1["dispatch_eligible"]:
            a_never_scheduled_after_claim = False
        if at >= CLAIM_AT:
            ids = {row["asset_id"] for row in ledger_ordered.dispatch_eligible(at)}
            assert A not in ids, f"争议设备在 {at} 出现在调度清单"
            assert s1["blocking_flows"] == ["evidentiary_hold"]

    assert a_never_scheduled_after_claim, "争议期间设备曾被判定可调度"

    # 关键时点断言
    # 转运到达后、出租开始前：A 在杭州仓保管、可调度
    pre_rental = ledger_ordered.snapshot_at(A, T + DAY + 4 * HOUR)
    assert pre_rental["custody"]["warehouse_id"] == WH2
    assert pre_rental["dispatch_eligible"] is True
    # 承租人使用期间：平台无保管区间、使用权属承租人
    in_use = ledger_ordered.snapshot_at(A, T + 3 * DAY)
    assert in_use["custody"] is None and in_use["usage"]["lessee"] == "user-bob"
    assert in_use["dispatch_eligible"] is False
    # 验机标准时点视图
    assert ledger_ordered.snapshot_at(A, T)["standard_in_force"] is None
    assert ledger_ordered.snapshot_at(A, T + 3 * M)["standard_in_force"][
        "standard_version"] == 1

    # 结算金额由规则推导：镜头 0->1 扣款 200
    state_a = ledger_ordered.asset(A)
    r1 = next(r for r in state_a["rentals"] if r["rental_id"] == "R-A-1")
    assert r1["settlement"]["status"] == "final"
    assert r1["settlement"]["total_charge"] == 200, r1["settlement"]
    # 免押快照不受后来政策变化影响
    assert r1["deposit_policy_snapshot"]["free"] is True
    r_b = next(r for r in ledger_ordered.asset(B)["rentals"] if r["rental_id"] == "R-B-1")
    assert r_b["deposit_free"] is False

    # 统计只从流转推导，且各账本一致
    stats1, stats2 = ledger_ordered.stats(), ledger_shuffled.stats()
    assert stats1 == stats2
    assert stats1["assets"] == 2
    assert stats1["rentals_total"] == 3
    assert stats1["rentals_settled"] == 1
    assert stats1["derived_from_events"] is True
    assert stats1["ewaste_avoided_kg"] > 0

    if verbose:
        print("收敛与争议阻断验收通过")
        print("ledger digest:", d1)
        print("拒绝事件数:", len(rejected_ids))
        for eid, reason in sorted(rejected_reasons.items()):
            print(f"  - {eid[:20]}… {reason}")
        print("统计:", stats1)
    return {
        "digest": d1,
        "rejected": rejected_reasons,
        "stats": stats1,
        "sample_times": len(sample_times),
    }


if __name__ == "__main__":
    run_selfcheck()
