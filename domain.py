"""循环租用资产履约：事件溯源账本与确定性投影。

所有业务事实都是不可变事件；资产状态、责任区间、结算与统计都由事件流
按 (occurred_at, event_id) 归位后确定性折叠得到。重复命令按 idem_key 去重，
乱序/重复投递同一批输入必然收敛到同一账本（见 Ledger.settle / digest）。
"""

import copy
import hashlib
import json
import threading

VERSION = "1.0.0"
PROJECTION_VERSION = 1

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def canon(value):
    """规范化 JSON：键排序、无空白，作为哈希与去重的唯一序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(value) -> str:
    return "sha256:" + hashlib.sha256(canon(value).encode("utf-8")).hexdigest()


def _as_ms(value) -> int:
    """接受毫秒整数或 ISO 字符串，统一为毫秒。"""
    if isinstance(value, bool):  # bool 是 int 子类，必须先排除
        raise ValueError("时间不能是布尔值")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        from datetime import datetime

        return int(datetime.fromisoformat(text).timestamp() * 1000)
    raise ValueError(f"无法解析时间: {value!r}")


def _segments_overlap(a_start, a_end, b_start, b_end) -> bool:
    """半开时间区间 [start, end) 是否重叠；None 表示开放端。"""
    if a_end is not None and b_start >= a_end:
        return False
    if b_end is not None and a_start >= b_end:
        return False
    return True


# ---------------------------------------------------------------------------
# 验机标准 / 费率目录（有版本，注册即不可变）
# ---------------------------------------------------------------------------

DEFAULT_STANDARD = {
    "standard_id": "std-camera",
    "version": 1,
    "name": "相机类验机标准 v1",
    "items": [
        {"code": "shutter", "name": "快门", "max_grade": 3},
        {"code": "lens", "name": "镜头", "max_grade": 3},
        {"code": "body", "name": "机身外观", "max_grade": 3},
        {"code": "sensor", "name": "传感器", "max_grade": 3},
        {"code": "function", "name": "功能", "max_grade": 2},
    ],
}

DEFAULT_RULES = {
    "rules_id": "charge-rules",
    "version": 1,
    "name": "损耗与缺件计费规则 v1",
    "per_grade_charge": [0, 200, 500, 1200],  # 等级 0..3 的扣款（分级/每项）
    "missing_item_charge": 800,
    "max_grade": 3,
}

DEFAULT_MASS = {
    "mass_id": "mass-table",
    "version": 1,
    "category_mass_kg": {"camera": 0.6, "lens": 0.4, "phone": 0.2, "computer": 1.5},
}


class Catalog:
    """有版本的标准/规则目录。注册后内容冻结，事件只引用其 id+version。"""

    def __init__(self):
        self._standards = {}
        self._rules = {}
        self._mass = {}
        self.register_standard(DEFAULT_STANDARD)
        self.register_rules(DEFAULT_RULES)
        self.register_mass(DEFAULT_MASS)

    def _register(self, table, doc, id_field, frozen_fields):
        key = (doc[id_field], int(doc["version"]))
        if key in table:
            if canon(table[key]) != canon(doc):
                raise ValueError(f"{id_field} 版本已存在且内容不同，标准不可变: {key}")
            return
        for field in frozen_fields:
            if field not in doc:
                raise ValueError(f"目录文档缺少字段: {field}")
        table[key] = copy.deepcopy(doc)

    def register_standard(self, doc):
        self._register(self._standards, doc, "standard_id", ("items",))
        if not isinstance(doc["items"], list) or not doc["items"]:
            raise ValueError("验机标准至少包含一个检查项")
        for item in doc["items"]:
            for field in ("code", "name", "max_grade"):
                if field not in item:
                    raise ValueError(f"检查项缺少字段: {field}")

    def register_rules(self, doc):
        self._register(self._rules, doc, "rules_id", ("per_grade_charge",))

    def register_mass(self, doc):
        self._register(self._mass, doc, "mass_id", ("category_mass_kg",))

    def standard(self, standard_id, version):
        return self._standards[(standard_id, int(version))]

    def rules(self, rules_id, version):
        return self._rules[(rules_id, int(version))]

    def mass(self, mass_id, version):
        return self._mass[(mass_id, int(version))]

    def listing(self):
        return {
            "standards": sorted(
                ({"standard_id": k[0], "version": k[1], "name": v["name"]}
                 for k, v in self._standards.items()),
                key=lambda d: (d["standard_id"], d["version"]),
            ),
            "rules": sorted(
                ({"rules_id": k[0], "version": k[1], "name": v["name"]}
                 for k, v in self._rules.items()),
                key=lambda d: (d["rules_id"], d["version"]),
            ),
        }


# ---------------------------------------------------------------------------
# 投影
# ---------------------------------------------------------------------------

# 阻止出库/调度的状态
BLOCKING_FLOWS = ("repair", "recall", "evidentiary_hold")


def _new_asset_state(asset_id):
    return {
        "asset_id": asset_id,
        "exists": False,
        "registered_at": None,
        "category": None,
        "model": None,
        "owner": None,
        "retired": False,
        "location": None,            # 当前位置（仓库/物流中）
        "in_transit": None,          # 进行中的转运 {"shipment_id", "from", "to"}
        "ownership": [],             # {"holder", "start", "end"}
        "custody": [],               # 平台保管区间
        "usage": [],                 # 承租人使用区间（含免押快照）
        "flows": [],                 # 维修/召回/保全/转运区间
        "inspections": [],
        "rentals": [],               # 租期 {"rental_id","lessee","start","end","status",...}
        "shipments": {},             # shipment_id -> 扫描状态
        "claims": {},                # claim_id -> 争议
        "retirement": None,
        "last_event": None,
        "version": 0,
    }


def _close_open(segments, end_ms):
    """闭合右端开放的区间。"""
    for seg in reversed(segments):
        if seg["end"] is None:
            seg["end"] = end_ms
            return seg
        break  # 仅最后一段可能开放
    return None


class Projection:
    """单资产的确定性状态机。每个事件要么被接受，要么被拒绝（拒绝可复现）。"""

    def __init__(self, asset_id, catalog: Catalog):
        self.s = _new_asset_state(asset_id)
        self.catalog = catalog

    # -- 通用校验 ----------------------------------------------------------

    def _require_exists(self, e):
        if not self.s["exists"]:
            raise Rejected(e, "资产尚未登记")
        if self.s["retired"]:
            raise Rejected(e, "资产已报废，生命周期结束")

    def _active_blocking(self, at):
        return [
            f["kind"]
            for f in self.s["flows"]
            if f["kind"] in BLOCKING_FLOWS and f["start"] <= at
            and (f["end"] is None or f["end"] > at)
        ]

    def _rental(self, rental_id):
        for r in self.s["rentals"]:
            if r["rental_id"] == rental_id:
                return r
        return None

    # -- 事件应用 ----------------------------------------------------------

    def apply(self, e):
        etype = e["type"]
        handler = getattr(self, f"_ev_{etype}", None)
        if handler is None:
            raise Rejected(e, f"未知事件类型: {etype}")
        handler(e)
        self.s["last_event"] = {"event_id": e["event_id"], "at": e["occurred_at"]}
        self.s["version"] += 1

    def _ev_asset_registered(self, e):
        if self.s["exists"]:
            raise Rejected(e, "资产身份不可复用：已登记过")
        for field in ("category", "model", "owner"):
            if not e.get(field):
                raise Rejected(e, f"登记缺少字段: {field}")
        self.s["exists"] = True
        self.s["registered_at"] = e["occurred_at"]
        self.s["category"] = e["category"]
        self.s["model"] = e["model"]
        self.s["owner"] = e["owner"]
        self.s["location"] = {"type": "owner", "place": e["owner"]}
        self.s["ownership"].append(
            {"holder": e["owner"], "basis": "consignment", "start": e["occurred_at"], "end": None}
        )

    def _ev_consigned_to_warehouse(self, e):
        self._require_exists(e)
        if not e.get("warehouse_id"):
            raise Rejected(e, "缺少 warehouse_id")
        _close_open(self.s["custody"], e["occurred_at"])
        self.s["custody"].append(
            {"warehouse_id": e["warehouse_id"], "start": e["occurred_at"], "end": None}
        )
        self.s["location"] = {"type": "warehouse", "place": e["warehouse_id"]}

    def _ev_inspection_completed(self, e):
        self._require_exists(e)
        try:
            standard = self.catalog.standard(e["standard_id"], e["standard_version"])
        except KeyError:
            raise Rejected(e, "引用的验机标准版本不存在")
        try:
            rules = self.catalog.rules(e["rules_id"], e["rules_version"])
        except KeyError:
            raise Rejected(e, "引用的计费规则版本不存在")
        items_payload = e.get("items")
        if not isinstance(items_payload, list) or not items_payload:
            raise Rejected(e, "验机缺少检查项结果")
        codes = {item["code"]: item for item in standard["items"]}
        results = []
        for raw in items_payload:
            code = raw.get("code")
            spec = codes.get(code)
            if spec is None:
                raise Rejected(e, f"检查项不在标准中: {code}")
            grade = raw.get("grade")
            if not isinstance(grade, int) or not (0 <= grade <= spec["max_grade"]):
                raise Rejected(e, f"检查项 {code} 等级越界")
            evidence = raw.get("evidence_hash", "")
            if not isinstance(evidence, str) or not evidence:
                raise Rejected(e, f"检查项 {code} 缺少证据哈希")
            results.append({"code": code, "grade": grade, "evidence_hash": evidence})
        # 证据包哈希：标准版本 + 全部结果，防篡改、可复核
        evidence_bundle = digest_of(
            {
                "standard_id": e["standard_id"],
                "standard_version": e["standard_version"],
                "items": results,
            }
        )
        self.s["inspections"].append(
            {
                "inspection_id": e["inspection_id"],
                "at": e["occurred_at"],
                "warehouse_id": e.get("warehouse_id"),
                "standard_id": e["standard_id"],
                "standard_version": e["standard_version"],
                "rules_id": e["rules_id"],
                "rules_version": e["rules_version"],
                "results": results,
                "overall_grade": e.get("overall_grade"),
                "evidence_bundle_hash": evidence_bundle,
            }
        )
        self.s["location"] = {"type": "warehouse",
                              "place": e.get("warehouse_id")
                              or (self.s["custody"][-1]["warehouse_id"] if self.s["custody"] else None)}

    def _ev_rental_confirmed(self, e):
        self._require_exists(e)
        if self._rental(e["rental_id"]) is not None:
            raise Rejected(e, "租期重复确认")
        start, end = _as_ms(e["rental_start"]), _as_ms(e["rental_end"])
        if start >= end:
            raise Rejected(e, "租期起止非法")
        blocking = self._active_blocking(e["occurred_at"])
        if blocking:
            raise Rejected(e, f"资产处于阻断状态，不可出租: {','.join(blocking)}")
        if self.s["in_transit"] is not None:
            raise Rejected(e, "资产转运中，不可出租")
        claim = self._open_claim(e["occurred_at"])
        if claim is not None:
            raise Rejected(e, f"争议保全中，不可出租: {claim}")
        for r in self.s["rentals"]:
            if r["status"] in ("confirmed", "outbound", "active") and _segments_overlap(
                start, end, _as_ms(r["start"]), _as_ms(r["end"])
            ):
                raise Rejected(e, "租期与未结束租期互斥冲突")
        deposit_free = bool(e.get("deposit_free"))
        record = {
            "rental_id": e["rental_id"],
            "lessee": e["lessee"],
            "start": start,
            "end": end,
            "confirmed_at": e["occurred_at"],
            "status": "confirmed",
            "deposit_free": deposit_free,
            # 免押资格在确认瞬间快照；事后资格变化不影响本租期
            "deposit_policy_snapshot": copy.deepcopy(e.get("deposit_policy_snapshot", {"free": deposit_free})),
            "settlement": None,
        }
        self.s["rentals"].append(record)

    def _open_claim(self, at):
        for cid, claim in self.s["claims"].items():
            if claim["status"] == "open" and claim["raised_at"] <= at:
                return cid
        return None

    def _ev_outbound_scanned(self, e):
        self._require_exists(e)
        rental = self._rental(e["rental_id"])
        if rental is None or rental["status"] != "confirmed":
            raise Rejected(e, "出库扫码缺少已确认租期")
        blocking = self._active_blocking(e["occurred_at"])
        if blocking:
            raise Rejected(e, f"资产处于阻断状态，禁止出库: {','.join(blocking)}")
        claim = self._open_claim(e["occurred_at"])
        if claim is not None:
            raise Rejected(e, f"争议保全中，禁止出库: {claim}")
        if self.s["in_transit"] is not None:
            raise Rejected(e, "资产转运中，禁止出库")
        shipment = self._shipment(e)
        if shipment["events"]:
            return  # 幂等：该运单已出过库
        shipment["events"].append("outbound")
        shipment["rental_id"] = e["rental_id"]
        rental["status"] = "outbound"
        # 出库即结束平台保管，开始承租人使用区间
        _close_open(self.s["custody"], e["occurred_at"])
        _close_open(self.s["usage"], e["occurred_at"])
        self.s["usage"].append(
            {"rental_id": rental["rental_id"], "lessee": rental["lessee"],
             "start": e["occurred_at"], "end": None}
        )
        self.s["location"] = {"type": "lessee", "place": rental["lessee"]}

    def _ev_delivery_confirmed(self, e):
        shipment = self._shipment(e)
        if "outbound" not in shipment["events"]:
            raise Rejected(e, "未出库不能确认交付")
        if "delivery" in shipment["events"]:
            return
        shipment["events"].append("delivery")
        rental = self._rental(shipment.get("rental_id") or e.get("rental_id"))
        if rental is not None and rental["status"] == "outbound":
            rental["status"] = "active"

    def _ev_return_scanned(self, e):
        self._require_exists(e)
        rental = self._rental(e["rental_id"])
        if rental is None or rental["status"] not in ("active", "outbound"):
            raise Rejected(e, "归还扫码需要进行中的租期")
        shipment = self._shipment(e)
        if "return" in shipment["events"]:
            return
        shipment["events"].append("return")
        shipment["rental_id"] = e["rental_id"]
        rental["status"] = "returned"
        # 归还入仓：承租人使用结束，平台保管恢复
        _close_open(self.s["usage"], e["occurred_at"])
        _close_open(self.s["custody"], e["occurred_at"])
        warehouse_id = e.get("warehouse_id") or (
            self.s["custody"][-1]["warehouse_id"] if self.s["custody"] else None)
        if warehouse_id:
            self.s["custody"].append(
                {"warehouse_id": warehouse_id, "start": e["occurred_at"], "end": None})
            self.s["location"] = {"type": "warehouse", "place": warehouse_id}
        else:
            self.s["location"] = {"type": "returning", "place": None}

    def _ev_discrepancy_computed(self, e):
        """平台按验机版本与计费规则先计算差异；此时仅为提案，未扣款。"""
        self._require_exists(e)
        rental = self._rental(e["rental_id"])
        if rental is None or rental["status"] not in ("returned", "disputed"):
            raise Rejected(e, "差异计算需要已归还租期")
        if rental["settlement"] is not None:
            raise Rejected(e, "租期已结算，不可重复计算")
        before = self._inspection(e.get("outbound_inspection_id"))
        after = self._inspection(e["return_inspection_id"])
        if before is None or after is None:
            raise Rejected(e, "差异计算需要出库与归还两次验机")
        rules = self.catalog.rules(after["rules_id"], after["rules_version"])
        table = rules["per_grade_charge"]
        before_map = {r["code"]: r for r in before["results"]}
        lines = []
        total = 0
        for cur in after["results"]:
            prev = before_map.get(cur["code"])
            prev_grade = prev["grade"] if prev else 0
            delta = max(0, cur["grade"] - prev_grade)
            if delta:
                charge = table[min(cur["grade"], len(table) - 1)] - table[prev_grade]
                charge = max(0, charge)
            else:
                charge = 0
            missing = bool(cur.get("missing"))
            if missing:
                charge += rules["missing_item_charge"]
            if charge:
                total += charge
                lines.append({
                    "code": cur["code"], "before_grade": prev_grade,
                    "after_grade": cur["grade"], "missing": missing, "charge": charge,
                })
        proposal = {
            "discrepancy_id": e["discrepancy_id"],
            "at": e["occurred_at"],
            "rental_id": rental["rental_id"],
            "outbound_inspection_id": before["inspection_id"],
            "return_inspection_id": after["inspection_id"],
            "rules_id": after["rules_id"],
            "rules_version": after["rules_version"],
            "lines": lines,
            "total_charge": total,
            "currency": rules.get("currency", "CNY"),
            "status": "proposed",
            "confirmed_by": [],
        }
        rental["settlement"] = proposal
        rental["status"] = "returned"

    def _inspection(self, inspection_id):
        if not inspection_id:
            return self.s["inspections"][-1] if self.s["inspections"] else None
        for ins in self.s["inspections"]:
            if ins["inspection_id"] == inspection_id:
                return ins
        return None

    def _ev_settlement_confirmed(self, e):
        """差异经双方确认后才成为最终结算。"""
        self._require_exists(e)
        rental = self._rental(e["rental_id"])
        if rental is None or rental["settlement"] is None:
            raise Rejected(e, "没有待确认的差异提案")
        proposal = rental["settlement"]
        if proposal["status"] == "final":
            return
        party = e["party"]
        if party not in ("lessee", "consignor"):
            raise Rejected(e, "确认方必须是 lessee 或 consignor")
        if proposal["status"] == "rejected":
            raise Rejected(e, "差异已被一方拒绝并进入争议，不可再确认")
        if party in proposal["confirmed_by"]:
            return  # 同一方重复确认幂等
        proposal["confirmed_by"].append(party)
        if e.get("accepted", True) is False:
            proposal["status"] = "rejected"
            rental["status"] = "disputed"
            return
        if set(proposal["confirmed_by"]) >= {"lessee", "consignor"}:
            proposal["status"] = "final"
            rental["status"] = "settled"

    def _ev_claim_raised(self, e):
        self._require_exists(e)
        if e["claim_id"] in self.s["claims"]:
            raise Rejected(e, "争议单重复")
        self.s["claims"][e["claim_id"]] = {
            "claim_id": e["claim_id"],
            "rental_id": e.get("rental_id"),
            "raised_at": e["occurred_at"],
            "reason": e.get("reason"),
            "status": "open",
        }
        # 争议即证据保全：冻结，不可再出租/出库
        self.s["flows"].append(
            {"kind": "evidentiary_hold", "ref": e["claim_id"],
             "start": e["occurred_at"], "end": None}
        )

    def _ev_claim_resolved(self, e):
        claim = self.s["claims"].get(e["claim_id"])
        if claim is None:
            raise Rejected(e, "争议单不存在")
        if claim["status"] != "open":
            return
        claim["status"] = e.get("resolution", "resolved")
        claim["resolved_at"] = e["occurred_at"]
        for flow in reversed(self.s["flows"]):
            if flow["kind"] == "evidentiary_hold" and flow["ref"] == e["claim_id"] and flow["end"] is None:
                flow["end"] = e["occurred_at"]
                break

    def _ev_repair_started(self, e):
        self._require_exists(e)
        if self.s["in_transit"] is not None:
            raise Rejected(e, "资产转运在途，需到仓后方可送修")
        blocking = self._active_blocking(e["occurred_at"])
        if "evidentiary_hold" in blocking:
            raise Rejected(e, "证据保全期间不得维修（防止灭失证据）")
        if "recall" in blocking:
            raise Rejected(e, "召回处理期间不得直接转维修")
        self.s["flows"].append(
            {"kind": "repair", "ref": e.get("repair_id"), "start": e["occurred_at"], "end": None}
        )
        self.s["location"] = {"type": "repair", "place": e.get("vendor")}

    def _ev_repair_finished(self, e):
        seg = self._find_open_flow("repair")
        if seg is None:
            raise Rejected(e, "没有进行中的维修")
        seg["end"] = e["occurred_at"]
        self.s["location"] = {"type": "warehouse", "place": e.get("warehouse_id")}

    def _ev_recall_issued(self, e):
        self._require_exists(e)
        self.s["flows"].append(
            {"kind": "recall", "ref": e.get("recall_id"), "start": e["occurred_at"], "end": None}
        )

    def _ev_recall_cleared(self, e):
        seg = self._find_open_flow("recall")
        if seg is None:
            raise Rejected(e, "没有进行中的召回")
        seg["end"] = e["occurred_at"]

    def _find_open_flow(self, kind):
        for flow in reversed(self.s["flows"]):
            if flow["kind"] == kind and flow["end"] is None:
                return flow
        return None

    # -- 仓间转运 ----------------------------------------------------------

    def _shipment(self, e):
        sid = e["shipment_id"]
        return self.s["shipments"].setdefault(
            sid, {"shipment_id": sid, "events": [], "rental_id": None})

    def _ev_transfer_started(self, e):
        self._require_exists(e)
        if self.s["in_transit"] is not None:
            raise Rejected(e, "已在转运中")
        blocking = self._active_blocking(e["occurred_at"])
        if blocking:
            raise Rejected(e, f"阻断状态下不得发起转运: {','.join(blocking)}")
        if self._open_claim(e["occurred_at"]):
            raise Rejected(e, "争议保全中不得转运")
        self.s["in_transit"] = {
            "shipment_id": e["shipment_id"], "from": e["from_warehouse"],
            "to": e["to_warehouse"], "start": e["occurred_at"],
        }
        self.s["flows"].append(
            {"kind": "interwarehouse_transfer", "ref": e["shipment_id"],
             "start": e["occurred_at"], "end": None}
        )
        self.s["location"] = {"type": "transit", "place": e["to_warehouse"]}

    def _ev_transfer_scanned(self, e):
        """物流回调：节点扫码；重复回调幂等、乱序归一。"""
        self._require_exists(e)
        tr = self.s["in_transit"]
        shipment = self._shipment(e)
        node = e["node"]  # pickup | in_transit | arrival
        order = {"pickup": 0, "in_transit": 1, "arrival": 2}
        if node not in order:
            raise Rejected(e, f"未知扫码节点: {node}")
        if node == "arrival":
            # 到达关闭转运；重复到达回调幂等
            if tr is None:
                if any(f["kind"] == "interwarehouse_transfer"
                       and f["ref"] == e["shipment_id"] for f in self.s["flows"]):
                    return
                raise Rejected(e, "没有进行中的转运")
            if tr["shipment_id"] != e["shipment_id"]:
                raise Rejected(e, "运单与当前转运不符")
            if "arrival" not in shipment["events"]:
                shipment["events"].append("arrival")
            for flow in reversed(self.s["flows"]):
                if flow["kind"] == "interwarehouse_transfer" and flow["end"] is None:
                    flow["end"] = e["occurred_at"]
                    break
            self.s["in_transit"] = None
            # 平台全程在保，到达后保管责任切换到目的仓
            _close_open(self.s["custody"], e["occurred_at"])
            self.s["custody"].append(
                {"warehouse_id": tr["to"], "start": e["occurred_at"], "end": None})
            self.s["location"] = {"type": "warehouse", "place": tr["to"]}
            return
        if tr is None:
            raise Rejected(e, "转运已结束或未开始，忽略途中扫码")
        if tr["shipment_id"] != e["shipment_id"]:
            raise Rejected(e, "运单与当前转运不符")
        # 只按节点前进，乱序回调不回退状态
        already = {order[x]: x for x in shipment["events"] if x in order}
        if node not in shipment["events"]:
            shipment["events"].append(node)
        shipment["events"].sort(key=lambda n: order.get(n, 99))

    def _ev_asset_retired(self, e):
        self._require_exists(e)
        if self.s["in_transit"] is not None:
            raise Rejected(e, "转运中不可报废")
        if self._open_claim(e["occurred_at"]):
            raise Rejected(e, "争议未决不可报废")
        self.s["retired"] = True
        self.s["retirement"] = {"at": e["occurred_at"], "reason": e.get("reason", "retired")}
        _close_open(self.s["custody"], e["occurred_at"])
        _close_open(self.s["usage"], e["occurred_at"])

    # -- 查询 --------------------------------------------------------------

    def snapshot(self, at=None):
        s = self.s
        ret = {
            "asset_id": s["asset_id"],
            "category": s["category"],
            "model": s["model"],
            "exists": s["exists"],
            "retired": s["retired"],
            "location": copy.deepcopy(s["location"]),
            "owner": s["owner"],
            "version": s["version"],
        }
        if at is None:
            ret.update(
                ownership=copy.deepcopy(s["ownership"]),
                custody=copy.deepcopy(s["custody"]),
                usage=copy.deepcopy(s["usage"]),
                flows=copy.deepcopy(s["flows"]),
                claims=copy.deepcopy(list(s["claims"].values())),
                rentals=copy.deepcopy(s["rentals"]),
                inspections=copy.deepcopy(s["inspections"]),
            )
            return ret
        # 时点查询：保管责任 / 适用验机标准 / 流转资格
        custody_at = None
        for seg in s["custody"]:
            if seg["start"] <= at and (seg["end"] is None or seg["end"] > at):
                custody_at = seg
        owner_at = None
        for seg in s["ownership"]:
            if seg["start"] <= at and (seg["end"] is None or seg["end"] > at):
                owner_at = seg
        usage_at = None
        for seg in s["usage"]:
            if seg["start"] <= at and (seg["end"] is None or seg["end"] > at):
                usage_at = seg
        active_flows = [
            f["kind"] for f in s["flows"]
            if f["start"] <= at and (f["end"] is None or f["end"] > at)
        ]
        # 时点有效的验机标准：该时点之前最近一次验机所引用的版本
        standard_at = None
        for ins in s["inspections"]:
            if ins["at"] <= at:
                standard_at = {
                    "standard_id": ins["standard_id"],
                    "standard_version": ins["standard_version"],
                    "rules_id": ins["rules_id"],
                    "rules_version": ins["rules_version"],
                    "inspection_id": ins["inspection_id"],
                }
        blocking = [f for f in active_flows if f in BLOCKING_FLOWS]
        transit_at = any(f == "interwarehouse_transfer" for f in active_flows)
        open_claims = [
            cid for cid, c in s["claims"].items()
            if c["status"] == "open" and c["raised_at"] <= at
        ]
        registered = bool(
            s["exists"] and s["registered_at"] is not None and s["registered_at"] <= at
        )
        retired_at = bool(s["retired"] and s["retirement"]["at"] <= at)
        # 已确认但尚未出库的预订也占用租期窗口
        reserved = self._reserved_at(at)
        # 流转资格完全由时点区间推导：在仓保管、承租人未占用、无阻断/争议/转运
        eligible = (
            registered
            and not retired_at
            and not blocking
            and not open_claims
            and not transit_at
            and not reserved
            and custody_at is not None
            and usage_at is None
        )
        return {
            "asset_id": s["asset_id"],
            "at": at,
            "registered": registered,
            "retired": bool(s["retired"] and s["retirement"]["at"] <= at),
            "owner_at": owner_at["holder"] if owner_at else None,
            "custody": copy.deepcopy(custody_at),
            "in_platform_custody": custody_at is not None,
            "usage": copy.deepcopy(usage_at),
            "active_flows": active_flows,
            "blocking_flows": blocking,
            "open_claims": open_claims,
            "standard_in_force": standard_at,
            "dispatch_eligible": eligible,
        }

    def _reserved_at(self, at):
        """时点 at 是否已被某个确认而未终止的租期占用（含已确认未开始的预订）。"""
        for r in self.s["rentals"]:
            if r["confirmed_at"] <= at < r["end"] \
                    and r["status"] in ("confirmed", "outbound", "active"):
                return True
        return False


class Rejected(Exception):
    """业务规则拒绝；同样输入必然同样拒绝（确定性）。"""

    def __init__(self, event, reason):
        self.event = event
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# 命令 -> 事件；收件箱（幂等、乱序收敛）
# ---------------------------------------------------------------------------

COMMAND_EVENT = {
    "register_asset": "asset_registered",
    "consign": "consigned_to_warehouse",
    "inspect": "inspection_completed",
    "confirm_rental": "rental_confirmed",
    "scan_outbound": "outbound_scanned",
    "confirm_delivery": "delivery_confirmed",
    "scan_return": "return_scanned",
    "compute_discrepancy": "discrepancy_computed",
    "confirm_settlement": "settlement_confirmed",
    "raise_claim": "claim_raised",
    "resolve_claim": "claim_resolved",
    "start_repair": "repair_started",
    "finish_repair": "repair_finished",
    "issue_recall": "recall_issued",
    "clear_recall": "recall_cleared",
    "start_transfer": "transfer_started",
    "transfer_scan": "transfer_scanned",
    "retire_asset": "asset_retired",
}

# 各事件必须携带的字段（从命令透传）
EVENT_REQUIRED = {
    "asset_registered": ("category", "model", "owner"),
    "consigned_to_warehouse": ("warehouse_id",),
    "inspection_completed": ("inspection_id", "standard_id", "standard_version",
                             "rules_id", "rules_version", "items"),
    "rental_confirmed": ("rental_id", "lessee", "rental_start", "rental_end"),
    "outbound_scanned": ("rental_id", "shipment_id"),
    "delivery_confirmed": ("shipment_id",),
    "return_scanned": ("rental_id", "shipment_id"),
    "discrepancy_computed": ("discrepancy_id", "rental_id", "return_inspection_id"),
    "settlement_confirmed": ("rental_id", "party"),
    "claim_raised": ("claim_id",),
    "claim_resolved": ("claim_id",),
    "repair_started": ("repair_id",),
    "repair_finished": (),
    "recall_issued": ("recall_id",),
    "recall_cleared": (),
    "transfer_started": ("shipment_id", "from_warehouse", "to_warehouse"),
    "transfer_scanned": ("shipment_id", "node"),
    "asset_retired": (),
}


def command_to_event(command: dict) -> dict:
    """把命令确定性地映射为事件。命令不携带 event_id 时由内容派生。"""
    ctype = command.get("type")
    etype = COMMAND_EVENT.get(ctype)
    if etype is None:
        raise ValueError(f"未知命令类型: {ctype}")
    if not command.get("asset_id"):
        raise ValueError("命令缺少 asset_id")
    for field in EVENT_REQUIRED[etype]:
        if field not in command:
            raise ValueError(f"命令 {ctype} 缺少字段: {field}")
    event = {k: v for k, v in command.items() if k not in ("type", "idem_key", "at")}
    event["type"] = etype
    event["occurred_at"] = _as_ms(command["at"] if command.get("at") is not None
                                  else command.get("occurred_at"))
    if not event.get("event_id"):
        if command.get("idem_key"):
            event["event_id"] = "evt-" + hashlib.sha256(
                str(command["idem_key"]).encode()).hexdigest()[:16]
        else:
            event["event_id"] = "evt-" + hashlib.sha256(
                canon(event).encode()).hexdigest()[:16]
    return event


# ---------------------------------------------------------------------------
# Ledger：多资产收件箱与折叠
# ---------------------------------------------------------------------------

class Ledger:
    """多资产收件箱与确定性折叠。

    收件箱只做幂等去重并保存全部原始事件；每次投递后按 (occurred_at,
    event_id) 对全集重新折叠一遍。这样无论事件怎样分块、乱序、重复投递，
    只要最终事件全集相同，归位结果、拒绝集合与账本指纹就必然相同。
    """

    def __init__(self, catalog=None):
        self.catalog = catalog or Catalog()
        self._states = {}
        self._events = {}              # event_id -> event（原始事实，不可变）
        self._idem = {}                # idem_key -> event_id
        self.rejected = []             # 最近一次折叠的确定性拒绝
        self.appended = []             # 最近一次折叠的归位事件ID
        self._lock = threading.RLock()

    def _state(self, asset_id):
        st = self._states.get(asset_id)
        if st is None:
            st = Projection(asset_id, self.catalog)
            self._states[asset_id] = st
        return st

    def ingest_commands(self, commands):
        """提交一批命令（可重复、可乱序、可分块）。返回每条命令的受理结果。"""
        with self._lock:
            if isinstance(commands, dict):
                commands = [commands]
            results = []
            changed = False
            for cmd in commands:
                idem = cmd.get("idem_key")
                if idem and idem in self._idem:
                    results.append({"status": "duplicate", "event_id": self._idem[idem]})
                    continue
                try:
                    event = command_to_event(cmd)
                except ValueError as exc:
                    results.append({"status": "invalid", "reason": str(exc)})
                    continue
                if event["event_id"] in self._events:
                    results.append({"status": "duplicate", "event_id": event["event_id"]})
                    continue
                self._events[event["event_id"]] = event
                if idem:
                    self._idem[idem] = event["event_id"]
                results.append({"status": "accepted", "event_id": event["event_id"]})
                changed = True
            if changed:
                self._fold()
            return results

    def _fold(self):
        """对事件全集按时间全量重放，重建全部资产投影与拒绝集合。"""
        self._states = {}
        self.rejected = []
        self.appended = []
        ordered = sorted(self._events.values(),
                         key=lambda e: (e["occurred_at"], e["event_id"]))
        for event in ordered:
            st = self._state(event["asset_id"])
            try:
                st.apply(event)
            except Rejected as rej:
                self.rejected.append({
                    "event_id": event["event_id"],
                    "asset_id": event["asset_id"],
                    "type": event["type"],
                    "occurred_at": event["occurred_at"],
                    "reason": rej.reason,
                })
                continue
            self.appended.append(event["event_id"])

    # -- 查询 --------------------------------------------------------------

    def asset(self, asset_id):
        with self._lock:
            st = self._states.get(asset_id)
            return st.s if st is not None and st.s["exists"] else None

    def snapshot_at(self, asset_id, at):
        with self._lock:
            st = self._states.get(asset_id)
            if st is None:
                return None
            return st.snapshot(at)

    def dispatch_eligible(self, at=None):
        """各仓当前可调度出库的资产。争议/维修/召回/保全/转运中/租用中一律排除。"""
        with self._lock:
            out = []
            for asset_id, st in self._states.items():
                if not st.s["exists"] or st.s["retired"]:
                    continue
                if at is None:
                    blocking = [f["kind"] for f in st.s["flows"] if f["end"] is None
                                and f["kind"] in BLOCKING_FLOWS]
                    open_claim = (st._open_claim(st.s["last_event"]["at"])
                                  if st.s["last_event"] else None)
                    busy_rental = any(r["status"] in ("confirmed", "outbound", "active")
                                      for r in st.s["rentals"])
                    place = st.s["location"]["place"] if st.s["location"] else None
                    if not blocking and not open_claim and not busy_rental \
                            and st.s["in_transit"] is None and st.s["location"] \
                            and st.s["location"]["type"] == "warehouse":
                        out.append({"asset_id": asset_id, "warehouse_id": place})
                else:
                    snap = st.snapshot(at)
                    if snap["dispatch_eligible"]:
                        out.append({"asset_id": asset_id,
                                    "warehouse_id": (snap.get("custody") or {}).get("warehouse_id")})
            return sorted(out, key=lambda d: d["asset_id"])

    def digest(self):
        """账本指纹：归位事件流与拒绝列表的哈希。

        两次同样输入（打乱顺序、分块、重复投递）必须相等。
        """
        with self._lock:
            return digest_of({
                "projection_version": PROJECTION_VERSION,
                "appended": self.appended,
                "rejected": [(r["event_id"], r["reason"]) for r in self.rejected],
            })

    # -- 派生统计（只从真实流转推导，不接受客户端上报） ---------------------

    def stats(self):
        with self._lock:
            mass = self.catalog.mass(DEFAULT_MASS["mass_id"], DEFAULT_MASS["version"])
            table = mass["category_mass_kg"]
            total_rentals = total_settled = total_active_days = 0
            charges = 0
            reused = 0           # 完成 ≥2 次出租的资产数（再利用）
            ewaste_avoided_kg = 0.0
            retired = 0
            for st in self._states.values():
                s = st.s
                if not s["exists"]:
                    continue
                completed = [r for r in s["rentals"]
                             if r["status"] in ("settled", "returned", "disputed")]
                total_rentals += len(s["rentals"])
                if len(s["rentals"]) >= 2:
                    reused += 1
                for r in s["rentals"]:
                    if r["status"] in ("settled", "returned"):
                        total_settled += 1
                        days = max(1, round((r["end"] - r["start"]) / 86_400_000))
                        total_active_days += days
                    if r["settlement"] and r["settlement"]["status"] == "final":
                        charges += r["settlement"]["total_charge"]
                if s["retired"]:
                    retired += 1
                else:
                    # 每一次完成的再出租循环视为推迟该品类一台设备的废弃
                    ewaste_avoided_kg += table.get(s["category"], 0.5) * len(completed)
            return {
                "assets": sum(1 for st in self._states.values() if st.s["exists"]),
                "rentals_total": total_rentals,
                "rentals_settled": total_settled,
                "rental_days": total_active_days,
                "assets_re_rented": reused,
                "assets_retired": retired,
                "settlement_charges": charges,
                "ewaste_avoided_kg": round(ewaste_avoided_kg, 3),
                "derived_from_events": True,
            }

    def pending_count(self):
        """全量折叠下没有挂起事件；保留接口用于断言输入全部被裁决。"""
        return 0

    def event_count(self):
        with self._lock:
            return len(self._events)
