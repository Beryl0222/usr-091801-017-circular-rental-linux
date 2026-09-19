"""账本投影：把事件折叠成可查询的领域状态。

所有派生数据（保管区间、差异结算单、阻断原因）都由规则在折叠时计算，
不信任任何客户端上报的结论。折叠是全函数：非法事件记入 violations，
状态保持不变，保证任意输入都能确定性地收敛。
"""

from __future__ import annotations

from .core import content_hash, parse_instant

ROLE_CONSIGNOR = "consignor"  # 寄租人：交付权利
ROLE_PLATFORM = "platform"    # 平台：保管责任
ROLE_RENTER = "renter"        # 承租人：使用权

INSPECTION_CONTEXTS = ("intake", "outbound", "return", "maintenance")
SCAN_TYPES = ("departed", "arrived", "delivered")
DIFF_RULE_VERSION = "diff-v1"

# 租约占用资产、阻断新租约的状态
RENTAL_ACTIVE_STATES = ("created", "confirmed", "outbound", "delivered")

_SCAN_STATUS = {"departed": "in_transit", "arrived": "arrived", "delivered": "delivered"}


def asset_identity(serial_no: str, owner_id: str) -> str:
    """实物身份：由序列号与归属人内容寻址，永不复用（含报废后）。"""
    return "AST-" + content_hash({"owner_id": owner_id, "serial_no": serial_no})[:20]


def compute_difference(ledger: "Ledger", rental: dict) -> dict | None:
    """diff-v1：归还验机中由非 fail 变为 fail 的质量项，按归还时标准计费。

    差异完全由规则从出库/归还两份验机报告推导，不接受客户端金额。
    """
    outbound = ledger.inspections.get(rental["outbound_inspection_id"])
    returned = ledger.inspections.get(rental["return_inspection_id"])
    asset = ledger.assets.get(rental["asset_id"])
    if outbound is None or returned is None or asset is None:
        return None
    standard = ledger.find_standard(asset["category"], returned["standard_version"])
    if standard is None:
        return None
    charges = {item["code"]: item["charge_cents"] for item in standard["items"]}
    before = {r["code"]: r["outcome"] for r in outbound["results"]}
    after = {r["code"]: r["outcome"] for r in returned["results"]}
    items = []
    for code in sorted(charges):
        old, new = before.get(code, "na"), after.get(code, "na")
        if old != "fail" and new == "fail":
            items.append({"code": code, "from": old, "to": new, "charge_cents": charges[code]})
    return {
        "rule_version": DIFF_RULE_VERSION,
        "outbound_inspection_id": rental["outbound_inspection_id"],
        "return_inspection_id": rental["return_inspection_id"],
        "items": items,
        "total_cents": sum(i["charge_cents"] for i in items),
    }


class Ledger:
    """事件折叠出的领域状态。"""

    def __init__(self):
        self.assets: dict[str, dict] = {}
        self.serial_to_asset: dict[str, str] = {}
        self.custody: dict[str, list[dict]] = {}        # asset_id -> 互斥区间 [start, end)
        self.standards: dict[str, list[dict]] = {}      # category -> 按生效时间排序的版本
        self.inspections: dict[str, dict] = {}
        self.rentals: dict[str, dict] = {}
        self.shipments: dict[str, dict] = {}
        self.maintenance: dict[str, list[dict]] = {}
        self.recalls: dict[str, list[dict]] = {}
        self.preservations: dict[str, list[dict]] = {}
        self.waiver_policies: dict[str, list[dict]] = {}
        self.violations: list[dict] = []

    # ---------------------------------------------------------------- 折叠

    def apply(self, event):
        handler = getattr(self, "_on_" + event.type, None)
        if handler is None:
            self._violate(event, "unknown_event_type")
            return
        handler(event)

    def _violate(self, event, reason: str, **detail):
        entry = {"event_id": event.id, "event_type": event.type, "reason": reason}
        entry.update(detail)
        self.violations.append(entry)

    # ---------------------------------------------------------------- 查询

    def custody_at(self, asset_id: str, at: str) -> dict | None:
        """某时刻的保管责任归属；区间半开 [start, end)，任意时刻至多一条。"""
        at_dt = parse_instant(at)
        for interval in self.custody.get(asset_id, []):
            start = parse_instant(interval["start"])
            end = parse_instant(interval["end"]) if interval["end"] else None
            if start <= at_dt and (end is None or at_dt < end):
                return dict(interval)
        return None

    def find_standard(self, category: str, version: str) -> dict | None:
        for std in self.standards.get(category, []):
            if std["version"] == version:
                return std
        return None

    def standard_at(self, category: str, at: str) -> dict | None:
        """某时刻有效的验机标准版本。"""
        at_dt = parse_instant(at)
        best = None
        for std in self.standards.get(category, []):
            eff = parse_instant(std["effective_from"])
            if eff <= at_dt and (
                best is None
                or (eff, std["version"]) > (parse_instant(best["effective_from"]), best["version"])
            ):
                best = std
        return dict(best) if best else None

    def waiver_eligible(self, subject: str, at: str) -> bool:
        """免押资格按生效时间取最近一条；确认租约时快照，之后的变化不影响已确认租期。"""
        at_dt = parse_instant(at)
        best = None
        for policy in self.waiver_policies.get(subject, []):
            eff = parse_instant(policy["effective_from"])
            if eff <= at_dt and (best is None or eff > parse_instant(best["effective_from"])):
                best = policy
        return bool(best and best["eligible"])

    @staticmethod
    def _series_active(series, at_dt) -> bool:
        for item in series:
            start = parse_instant(item["start"])
            end = parse_instant(item["end"]) if item["end"] else None
            if start <= at_dt and (end is None or at_dt < end):
                return True
        return False

    @staticmethod
    def _rental_active_at(rental, at_dt) -> bool:
        confirmed = rental["confirmed_at"]
        if not confirmed or parse_instant(confirmed) > at_dt:
            return False
        for closing in (rental["returned_at"], rental["cancelled_at"]):
            if closing and parse_instant(closing) <= at_dt:
                return False
        return True

    @staticmethod
    def _dispute_open_at(rental, at_dt) -> bool:
        dispute = rental["dispute"]
        if not dispute or parse_instant(dispute["opened_at"]) > at_dt:
            return False
        resolved = dispute["resolved_at"]
        return not resolved or parse_instant(resolved) > at_dt

    def transit_blockers(self, asset_id: str, at: str) -> list[str]:
        """转运不得穿越的状态：维修、召回、证据保全（以及报废）。"""
        at_dt = parse_instant(at)
        asset = self.assets.get(asset_id)
        if asset is None:
            return ["asset_unknown"]
        reasons = []
        if asset["scrapped"] and parse_instant(asset["scrapped"]["at"]) <= at_dt:
            reasons.append("scrapped")
        for name, series_map in (
            ("maintenance", self.maintenance),
            ("recall", self.recalls),
            ("preservation", self.preservations),
        ):
            if self._series_active(series_map.get(asset_id, []), at_dt):
                reasons.append(name)
        return reasons

    def blocked_reasons(self, asset_id: str, at: str) -> list[str]:
        """流转资格的完整阻断原因；为空表示可调度出库。"""
        reasons = self.transit_blockers(asset_id, at)
        if reasons == ["asset_unknown"]:
            return reasons
        at_dt = parse_instant(at)
        for rental in self.rentals.values():
            if rental["asset_id"] != asset_id:
                continue
            if self._rental_active_at(rental, at_dt):
                reasons.append("rental_active")
            if self._dispute_open_at(rental, at_dt):
                reasons.append("dispute_open")
        custody = self.custody_at(asset_id, at)
        if custody is None or custody["role"] != ROLE_PLATFORM:
            reasons.append("custody_not_platform")
        return sorted(set(reasons))

    # ---------------------------------------------------------------- 摘要

    def snapshot(self) -> dict:
        return {
            "assets": self.assets,
            "custody": self.custody,
            "standards": self.standards,
            "inspections": self.inspections,
            "rentals": self.rentals,
            "shipments": self.shipments,
            "maintenance": self.maintenance,
            "recalls": self.recalls,
            "preservations": self.preservations,
            "waiver_policies": self.waiver_policies,
            "violations": self.violations,
        }

    def digest(self) -> str:
        """账本摘要：两次计算收敛的判据。"""
        return content_hash(self.snapshot())

    # ---------------------------------------------------------------- 资产

    def _on_asset_registered(self, event):
        p = event.payload
        asset_id = p["asset_id"]
        if asset_identity(p["serial_no"], p["owner_id"]) != asset_id:
            return self._violate(event, "identity_mismatch")
        if asset_id in self.assets:
            return self._violate(event, "asset_exists")
        if p["serial_no"] in self.serial_to_asset:
            return self._violate(event, "serial_bound")
        self.assets[asset_id] = {
            "asset_id": asset_id,
            "serial_no": p["serial_no"],
            "model": p["model"],
            "category": p["category"],
            "owner_id": p["owner_id"],
            "registered_at": event.occurred_at,
            "warehouse_id": None,
            "scrapped": None,
        }
        self.serial_to_asset[p["serial_no"]] = asset_id
        # 登记后资产在寄租人手中（持有交付权利），等待入仓
        self.custody.setdefault(asset_id, []).append(
            {"role": ROLE_CONSIGNOR, "holder_id": p["owner_id"], "start": event.occurred_at, "end": None}
        )

    def _on_asset_intake(self, event):
        p = event.payload
        if p["asset_id"] not in self.assets:
            return self._violate(event, "asset_unknown")
        if self._custody_transition(event, p["asset_id"], ROLE_CONSIGNOR, ROLE_PLATFORM, p["warehouse_id"]):
            self.assets[p["asset_id"]]["warehouse_id"] = p["warehouse_id"]

    def _on_asset_scrapped(self, event):
        p = event.payload
        asset = self.assets.get(p["asset_id"])
        if asset is None:
            return self._violate(event, "asset_unknown")
        if asset["scrapped"]:
            return self._violate(event, "asset_already_scrapped")
        for rental in self.rentals.values():
            if rental["asset_id"] == p["asset_id"] and rental["status"] in RENTAL_ACTIVE_STATES:
                return self._violate(event, "rental_active")
        asset["scrapped"] = {"at": event.occurred_at, "reason": p["reason"]}
        intervals = self.custody.get(p["asset_id"], [])
        if intervals and intervals[-1]["end"] is None:
            intervals[-1]["end"] = event.occurred_at

    # ---------------------------------------------------------------- 保管

    def _custody_transition(self, event, asset_id, expect_role, new_role, holder_id) -> bool:
        """关闭当前区间并开启新区间，保证同一资产的权利区间互斥且连续。"""
        intervals = self.custody.setdefault(asset_id, [])
        if not intervals or intervals[-1]["end"] is not None:
            self._violate(event, "custody_not_open")
            return False
        top = intervals[-1]
        if top["role"] != expect_role:
            self._violate(event, "custody_role_mismatch", expected=expect_role, actual=top["role"])
            return False
        if parse_instant(event.occurred_at) < parse_instant(top["start"]):
            self._violate(event, "custody_time_regression")
            return False
        top["end"] = event.occurred_at
        intervals.append(
            {"role": new_role, "holder_id": holder_id, "start": event.occurred_at, "end": None}
        )
        return True

    def _set_platform_holder(self, asset_id, warehouse_id):
        intervals = self.custody.get(asset_id, [])
        if intervals and intervals[-1]["end"] is None and intervals[-1]["role"] == ROLE_PLATFORM:
            intervals[-1]["holder_id"] = warehouse_id
        asset = self.assets.get(asset_id)
        if asset is not None:
            asset["warehouse_id"] = warehouse_id

    # ---------------------------------------------------------------- 验机

    def _on_standard_published(self, event):
        p = event.payload
        if self.find_standard(p["category"], p["version"]):
            return self._violate(event, "standard_exists")
        self.standards.setdefault(p["category"], []).append(
            {
                "category": p["category"],
                "version": p["version"],
                "items": p["items"],
                "effective_from": p["effective_from"],
                "published_at": event.occurred_at,
            }
        )
        self.standards[p["category"]].sort(key=lambda s: (s["effective_from"], s["version"]))

    def _on_inspection_completed(self, event):
        p = event.payload
        asset = self.assets.get(p["asset_id"])
        if asset is None:
            return self._violate(event, "asset_unknown")
        if p["inspection_id"] in self.inspections:
            return self._violate(event, "inspection_exists")
        if self.find_standard(asset["category"], p["standard_version"]) is None:
            return self._violate(event, "standard_unknown")
        self.inspections[p["inspection_id"]] = {
            "inspection_id": p["inspection_id"],
            "asset_id": p["asset_id"],
            "standard_version": p["standard_version"],
            "context": p["context"],
            "results": p["results"],
            "inspector": p["inspector"],
            "recorded_at": event.occurred_at,
        }

    # ---------------------------------------------------------------- 租约

    def _rental(self, event, rental_id):
        rental = self.rentals.get(rental_id)
        if rental is None:
            self._violate(event, "rental_unknown")
        return rental

    def _on_rental_created(self, event):
        p = event.payload
        if p["rental_id"] in self.rentals:
            return self._violate(event, "rental_exists")
        asset = self.assets.get(p["asset_id"])
        if asset is None:
            return self._violate(event, "asset_unknown")
        if asset["scrapped"]:
            return self._violate(event, "asset_scrapped")
        for rental in self.rentals.values():
            if rental["asset_id"] != p["asset_id"]:
                continue
            if rental["status"] in RENTAL_ACTIVE_STATES:
                return self._violate(event, "rental_active")
            if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
                return self._violate(event, "dispute_open")
        self.rentals[p["rental_id"]] = {
            "rental_id": p["rental_id"],
            "asset_id": p["asset_id"],
            "renter_id": p["renter_id"],
            "start": p["start"],
            "end": p["end"],
            "status": "created",
            "deposit_waived": None,
            "confirmed_at": None,
            "outbound_at": None,
            "delivered_at": None,
            "returned_at": None,
            "cancelled_at": None,
            "settled_at": None,
            "outbound_inspection_id": None,
            "return_inspection_id": None,
            "difference": None,
            "confirmations": [],
            "dispute": None,
        }

    def _on_rental_confirmed(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        if rental["status"] != "created":
            return self._violate(event, "rental_not_created")
        # 免押资格在确认时刻快照；之后的政策变化不影响本租期
        rental["deposit_waived"] = self.waiver_eligible(rental["renter_id"], event.occurred_at)
        rental["confirmed_at"] = event.occurred_at
        rental["status"] = "confirmed"

    def _on_rental_cancelled(self, event):
        rental = self._rental(event, event.payload["rental_id"])
        if rental is None:
            return
        if rental["status"] not in ("created", "confirmed"):
            return self._violate(event, "rental_not_cancellable")
        rental["status"] = "cancelled"
        rental["cancelled_at"] = event.occurred_at

    def _check_inspection(self, event, rental, inspection_id, context):
        insp = self.inspections.get(inspection_id)
        if insp is None:
            return self._violate(event, "inspection_unknown")
        if insp["asset_id"] != rental["asset_id"]:
            return self._violate(event, "inspection_asset_mismatch")
        if insp["context"] != context:
            return self._violate(event, "inspection_context_mismatch", expected=context)
        return True

    def _on_rental_outbound(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        if rental["status"] != "confirmed":
            return self._violate(event, "rental_not_confirmed")
        if not self._check_inspection(event, rental, p["inspection_id"], "outbound"):
            return
        custody = self.custody_at(rental["asset_id"], event.occurred_at)
        if custody is None or custody["role"] != ROLE_PLATFORM:
            return self._violate(event, "custody_not_platform")
        rental["outbound_inspection_id"] = p["inspection_id"]
        rental["outbound_at"] = event.occurred_at
        rental["status"] = "outbound"

    def _on_rental_delivered(self, event):
        rental = self._rental(event, event.payload["rental_id"])
        if rental is None:
            return
        if rental["status"] != "outbound":
            return self._violate(event, "rental_not_outbound")
        self._custody_transition(
            event, rental["asset_id"], ROLE_PLATFORM, ROLE_RENTER, rental["renter_id"]
        )
        rental["delivered_at"] = event.occurred_at
        rental["status"] = "delivered"

    def _on_rental_returned(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        if rental["status"] != "delivered":
            return self._violate(event, "rental_not_delivered")
        if not self._check_inspection(event, rental, p["inspection_id"], "return"):
            return
        self._custody_transition(
            event, rental["asset_id"], ROLE_RENTER, ROLE_PLATFORM, p["warehouse_id"]
        )
        self.assets[rental["asset_id"]]["warehouse_id"] = p["warehouse_id"]
        rental["return_inspection_id"] = p["inspection_id"]
        rental["returned_at"] = event.occurred_at
        # 差异由规则在归还时计算，之后进入双方确认流程
        rental["difference"] = compute_difference(self, rental)
        if rental["difference"] is None:
            return self._violate(event, "difference_not_computable")
        rental["status"] = "difference_pending"

    def _on_difference_confirmed(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        if rental["status"] not in ("difference_pending", "difference_confirmed"):
            return self._violate(event, "difference_not_pending")
        party = p["party"]
        if party in rental["confirmations"]:
            return self._violate(event, "party_already_confirmed")
        rental["confirmations"].append(party)
        rental["confirmations"].sort()
        if set(rental["confirmations"]) == {"platform", "renter"}:
            rental["status"] = "difference_confirmed"

    def _on_settlement_completed(self, event):
        rental = self._rental(event, event.payload["rental_id"])
        if rental is None:
            return
        if rental["status"] != "difference_confirmed":
            return self._violate(event, "difference_not_confirmed")
        if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
            return self._violate(event, "dispute_open")
        rental["status"] = "settled"
        rental["settled_at"] = event.occurred_at

    def _on_dispute_opened(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        if rental["status"] not in ("difference_pending", "difference_confirmed"):
            return self._violate(event, "rental_not_disputable")
        if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
            return self._violate(event, "dispute_already_open")
        rental["dispute"] = {
            "opened_at": event.occurred_at,
            "reason": p["reason"],
            "resolved_at": None,
            "resolution": None,
        }

    def _on_dispute_resolved(self, event):
        p = event.payload
        rental = self._rental(event, p["rental_id"])
        if rental is None:
            return
        dispute = rental["dispute"]
        if not dispute or dispute["resolved_at"] is not None:
            return self._violate(event, "dispute_not_open")
        dispute["resolved_at"] = event.occurred_at
        dispute["resolution"] = p["resolution"]

    # ---------------------------------------------------------------- 阻断状态

    def _series_open(self, event, series_map, label):
        p = event.payload
        if p["asset_id"] not in self.assets:
            return self._violate(event, "asset_unknown")
        series = series_map.setdefault(p["asset_id"], [])
        if series and series[-1]["end"] is None:
            return self._violate(event, label + "_already_open")
        series.append({"start": event.occurred_at, "end": None, "reason": p["reason"]})

    def _series_close(self, event, series_map, label):
        p = event.payload
        series = series_map.get(p["asset_id"], [])
        if not series or series[-1]["end"] is not None:
            return self._violate(event, label + "_not_open")
        if parse_instant(event.occurred_at) < parse_instant(series[-1]["start"]):
            return self._violate(event, label + "_time_regression")
        series[-1]["end"] = event.occurred_at

    def _on_maintenance_started(self, event):
        self._series_open(event, self.maintenance, "maintenance")

    def _on_maintenance_ended(self, event):
        self._series_close(event, self.maintenance, "maintenance")

    def _on_recall_issued(self, event):
        self._series_open(event, self.recalls, "recall")

    def _on_recall_lifted(self, event):
        self._series_close(event, self.recalls, "recall")

    def _on_preservation_started(self, event):
        self._series_open(event, self.preservations, "preservation")

    def _on_preservation_ended(self, event):
        self._series_close(event, self.preservations, "preservation")

    # ---------------------------------------------------------------- 物流

    def _on_shipment_created(self, event):
        p = event.payload
        if p["shipment_id"] in self.shipments:
            return self._violate(event, "shipment_exists")
        reasons = self.blocked_reasons(p["asset_id"], event.occurred_at)
        if reasons:
            return self._violate(event, "shipment_blocked", blockers=reasons)
        custody = self.custody_at(p["asset_id"], event.occurred_at)
        if custody["holder_id"] != p["from_warehouse"]:
            return self._violate(event, "asset_not_at_origin")
        self.shipments[p["shipment_id"]] = {
            "shipment_id": p["shipment_id"],
            "asset_id": p["asset_id"],
            "from_warehouse": p["from_warehouse"],
            "to_warehouse": p["to_warehouse"],
            "created_at": event.occurred_at,
            "scans": {},
            "status": "created",
            "location": p["from_warehouse"],
        }

    def _on_shipment_scan(self, event):
        """扫码事件：重复投递按 id 去重，乱序按 (时间, id) 取最大，结果确定。

        资产处于维修、召回或证据保全状态时，扫码不得推进物流（不得穿越）。
        """
        p = event.payload
        ship = self.shipments.get(p["shipment_id"])
        if ship is None:
            return self._violate(event, "shipment_unknown")
        if parse_instant(event.occurred_at) < parse_instant(ship["created_at"]):
            return self._violate(event, "scan_before_creation")
        blockers = self.transit_blockers(ship["asset_id"], event.occurred_at)
        if blockers:
            return self._violate(event, "scan_while_blocked", blockers=blockers)
        expected = {
            "departed": ship["from_warehouse"],
            "arrived": ship["to_warehouse"],
            "delivered": ship["to_warehouse"],
        }.get(p["scan_type"])
        if expected is None:
            return self._violate(event, "scan_type_unknown")
        if p["warehouse_id"] != expected:
            return self._violate(event, "scan_warehouse_mismatch", expected=expected)
        ship["scans"][event.id] = {
            "type": p["scan_type"],
            "warehouse_id": p["warehouse_id"],
            "at": event.occurred_at,
        }
        _, latest = max(ship["scans"].items(), key=lambda kv: (parse_instant(kv[1]["at"]), kv[0]))
        ship["status"] = _SCAN_STATUS[latest["type"]]
        ship["location"] = None if latest["type"] == "departed" else latest["warehouse_id"]
        if latest["type"] == "delivered":
            self._set_platform_holder(ship["asset_id"], ship["to_warehouse"])

    # ---------------------------------------------------------------- 免押政策

    def _on_waiver_policy_updated(self, event):
        p = event.payload
        series = self.waiver_policies.setdefault(p["subject"], [])
        for policy in series:
            if policy["effective_from"] == p["effective_from"]:
                return self._violate(event, "policy_exists")
        series.append({"eligible": p["eligible"], "effective_from": p["effective_from"]})
        series.sort(key=lambda item: item["effective_from"])
