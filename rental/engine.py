"""命令与查询：校验业务规则、生成事件、维护账本收敛。

命令在当前账本上校验后生成事件；事件落库时若插入到规范顺序中间
（补传/乱序到达），自动整体重折叠，保证在线账本与任意重放结果一致。
"""

from __future__ import annotations

import re
import threading

from . import stats as stats_mod
from .core import Event, EventStore, format_instant, make_event, now_iso, parse_instant
from .ledger import (
    INSPECTION_CONTEXTS,
    RENTAL_ACTIVE_STATES,
    ROLE_CONSIGNOR,
    ROLE_PLATFORM,
    SCAN_TYPES,
    Ledger,
    asset_identity,
)

_EVIDENCE_RE = re.compile(r"[0-9a-f]{64}")


class CommandRejected(Exception):
    """命令违反业务规则；reasons 为机器可读的原因列表。"""

    def __init__(self, reasons):
        self.reasons = [str(r) for r in reasons]
        super().__init__("; ".join(self.reasons))


class UnknownQuery(Exception):
    pass


COMMANDS = {
    "register_asset",
    "intake_asset",
    "scrap_asset",
    "publish_standard",
    "record_inspection",
    "create_rental",
    "confirm_rental",
    "cancel_rental",
    "outbound_rental",
    "deliver_rental",
    "return_rental",
    "confirm_difference",
    "settle_rental",
    "open_dispute",
    "resolve_dispute",
    "start_maintenance",
    "end_maintenance",
    "issue_recall",
    "lift_recall",
    "start_preservation",
    "end_preservation",
    "create_shipment",
    "record_scan",
    "update_waiver_policy",
}

QUERIES = {
    "custody_at",
    "circulation_eligibility",
    "standard_at",
    "asset",
    "rental",
    "shipment",
    "lifecycle_stats",
    "ewaste_stats",
    "ledger_digest",
    "verify_replay",
    "events",
}


def _need(params: dict, *fields: str):
    missing = [f for f in fields if params.get(f) is None]
    if missing:
        raise CommandRejected([f"missing_field:{f}" for f in missing])


class Engine:
    """领域引擎：命令入口 + 收敛式事件重放 + 时间点查询。"""

    def __init__(self):
        self.store = EventStore()
        self.ledger = Ledger()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 折叠维护

    def _publish(self, events):
        for event in events:
            if not self.store.append(event):
                continue  # 重复投递：幂等忽略
            if self.store.is_tail(event):
                self.ledger.apply(event)
            else:
                self._refold()

    def _refold(self):
        fresh = Ledger()
        for event in self.store.canonical():
            fresh.apply(event)
        self.ledger = fresh

    def replay(self, events=None) -> Ledger:
        """重放（可传入打乱/重复的事件流），返回收敛后的账本。"""
        store = EventStore()
        source = self.store.canonical() if events is None else events
        for event in source:
            store.append(event)
        fresh = Ledger()
        for event in store.canonical():
            fresh.apply(event)
        return fresh

    def ledger_at(self, at: str) -> Ledger:
        """折叠截至某时刻的事件，用于时间点审计查询。"""
        fresh = Ledger()
        for event in self.store.up_to(at):
            fresh.apply(event)
        return fresh

    # ------------------------------------------------------------ 命令入口

    def execute(self, command: str, params: dict | None = None) -> list[Event]:
        with self._lock:
            if command not in COMMANDS:
                raise CommandRejected(["unknown_command"])
            params = params or {}
            if not isinstance(params, dict):
                raise CommandRejected(["invalid_params"])
            handler = getattr(self, "_cmd_" + command)
            try:
                events = handler(params)
            except CommandRejected:
                raise
            except (KeyError, ValueError, TypeError) as exc:
                raise CommandRejected([f"invalid_params: {exc}"]) from exc
            self._publish(events)
            return events

    def _at(self, params: dict) -> str:
        raw = params.get("at")
        return format_instant(parse_instant(raw)) if raw is not None else now_iso()

    def _asset(self, asset_id: str) -> dict:
        asset = self.ledger.assets.get(asset_id)
        if asset is None:
            raise CommandRejected(["asset_unknown"])
        return asset

    def _rental(self, rental_id: str) -> dict:
        rental = self.ledger.rentals.get(rental_id)
        if rental is None:
            raise CommandRejected(["rental_unknown"])
        return rental

    def _open_custody_role(self, asset_id: str):
        intervals = self.ledger.custody.get(asset_id, [])
        if intervals and intervals[-1]["end"] is None:
            return intervals[-1]["role"]
        return None

    # ------------------------------------------------------------ 资产命令

    def _cmd_register_asset(self, p):
        _need(p, "serial_no", "model", "category", "owner_id")
        asset_id = asset_identity(p["serial_no"], p["owner_id"])
        existing = self.ledger.assets.get(asset_id)
        if existing is not None:
            if existing["scrapped"]:
                raise CommandRejected(["asset_scrapped"])  # 报废身份永久占用，不可复活
            return []  # 同一实物重复登记：幂等
        if p["serial_no"] in self.ledger.serial_to_asset:
            raise CommandRejected(["serial_bound_to_other_asset"])
        return [
            make_event(
                "asset_registered",
                self._at(p),
                {
                    "asset_id": asset_id,
                    "serial_no": p["serial_no"],
                    "model": p["model"],
                    "category": p["category"],
                    "owner_id": p["owner_id"],
                },
            )
        ]

    def _cmd_intake_asset(self, p):
        _need(p, "asset_id", "warehouse_id")
        self._asset(p["asset_id"])
        if self._open_custody_role(p["asset_id"]) != ROLE_CONSIGNOR:
            raise CommandRejected(["custody_not_consignor"])
        return [make_event("asset_intake", self._at(p), {"asset_id": p["asset_id"], "warehouse_id": p["warehouse_id"]})]

    def _cmd_scrap_asset(self, p):
        _need(p, "asset_id", "reason")
        asset = self._asset(p["asset_id"])
        if asset["scrapped"]:
            raise CommandRejected(["asset_already_scrapped"])
        for rental in self.ledger.rentals.values():
            if rental["asset_id"] == p["asset_id"] and rental["status"] in RENTAL_ACTIVE_STATES:
                raise CommandRejected(["rental_active"])
        if self._open_custody_role(p["asset_id"]) != ROLE_PLATFORM:
            raise CommandRejected(["custody_not_platform"])
        return [make_event("asset_scrapped", self._at(p), {"asset_id": p["asset_id"], "reason": p["reason"]})]

    # ------------------------------------------------------------ 验机命令

    def _cmd_publish_standard(self, p):
        _need(p, "category", "version", "items", "effective_from")
        items = p["items"]
        if not isinstance(items, list) or not items:
            raise CommandRejected(["items_empty"])
        codes = set()
        for item in items:
            code, charge = item.get("code"), item.get("charge_cents")
            if not code or not isinstance(charge, int) or charge < 0:
                raise CommandRejected(["item_invalid"])
            if code in codes:
                raise CommandRejected(["item_code_duplicated"])
            codes.add(code)
        if self.ledger.find_standard(p["category"], p["version"]):
            raise CommandRejected(["standard_exists"])
        effective_from = format_instant(parse_instant(p["effective_from"]))
        return [
            make_event(
                "standard_published",
                self._at(p),
                {
                    "category": p["category"],
                    "version": p["version"],
                    "items": [
                        {"code": i["code"], "name": i.get("name", i["code"]), "charge_cents": i["charge_cents"]}
                        for i in items
                    ],
                    "effective_from": effective_from,
                },
            )
        ]

    def _cmd_record_inspection(self, p):
        _need(p, "inspection_id", "asset_id", "standard_version", "context", "results", "inspector")
        asset = self._asset(p["asset_id"])
        if p["context"] not in INSPECTION_CONTEXTS:
            raise CommandRejected(["context_unknown"])
        standard = self.ledger.find_standard(asset["category"], p["standard_version"])
        if standard is None:
            raise CommandRejected(["standard_unknown"])
        if p["inspection_id"] in self.ledger.inspections:
            raise CommandRejected(["inspection_exists"])
        codes = {item["code"] for item in standard["items"]}
        normalized = []
        for result in p["results"]:
            code, outcome, evidence = result["code"], result["outcome"], result["evidence_hash"]
            if code not in codes:
                raise CommandRejected([f"item_unknown:{code}"])
            if outcome not in ("pass", "fail"):
                raise CommandRejected(["outcome_unknown"])
            if not _EVIDENCE_RE.fullmatch(str(evidence)):
                raise CommandRejected(["evidence_hash_invalid"])
            normalized.append({"code": code, "outcome": outcome, "evidence_hash": evidence})
        if {r["code"] for r in normalized} != codes:
            raise CommandRejected(["items_incomplete"])
        normalized.sort(key=lambda r: r["code"])  # 规范化，保证同一报告幂等
        return [
            make_event(
                "inspection_completed",
                self._at(p),
                {
                    "inspection_id": p["inspection_id"],
                    "asset_id": p["asset_id"],
                    "standard_version": p["standard_version"],
                    "context": p["context"],
                    "results": normalized,
                    "inspector": p["inspector"],
                },
            )
        ]

    # ------------------------------------------------------------ 租约命令

    def _cmd_create_rental(self, p):
        _need(p, "rental_id", "asset_id", "renter_id", "start", "end")
        asset = self._asset(p["asset_id"])
        if asset["scrapped"]:
            raise CommandRejected(["asset_scrapped"])
        if p["rental_id"] in self.ledger.rentals:
            raise CommandRejected(["rental_exists"])
        start, end = parse_instant(p["start"]), parse_instant(p["end"])
        if not start < end:
            raise CommandRejected(["period_invalid"])
        for rental in self.ledger.rentals.values():
            if rental["asset_id"] != p["asset_id"]:
                continue
            if rental["status"] in RENTAL_ACTIVE_STATES:
                raise CommandRejected(["rental_active"])
            if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
                raise CommandRejected(["dispute_open"])  # 争议期间不可再次出租
        return [
            make_event(
                "rental_created",
                self._at(p),
                {
                    "rental_id": p["rental_id"],
                    "asset_id": p["asset_id"],
                    "renter_id": p["renter_id"],
                    "start": format_instant(start),
                    "end": format_instant(end),
                },
            )
        ]

    def _cmd_confirm_rental(self, p):
        _need(p, "rental_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] != "created":
            raise CommandRejected(["rental_not_created"])
        at = self._at(p)
        waived = self.ledger.waiver_eligible(rental["renter_id"], at)
        return [make_event("rental_confirmed", at, {"rental_id": p["rental_id"], "deposit_waived": waived})]

    def _cmd_cancel_rental(self, p):
        _need(p, "rental_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] not in ("created", "confirmed"):
            raise CommandRejected(["rental_not_cancellable"])
        return [make_event("rental_cancelled", self._at(p), {"rental_id": p["rental_id"]})]

    def _cmd_outbound_rental(self, p):
        _need(p, "rental_id", "inspection_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] != "confirmed":
            raise CommandRejected(["rental_not_confirmed"])
        self._check_inspection(p["inspection_id"], rental, "outbound")
        if self._open_custody_role(rental["asset_id"]) != ROLE_PLATFORM:
            raise CommandRejected(["custody_not_platform"])
        return [make_event("rental_outbound", self._at(p), {"rental_id": p["rental_id"], "inspection_id": p["inspection_id"]})]

    def _cmd_deliver_rental(self, p):
        _need(p, "rental_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] != "outbound":
            raise CommandRejected(["rental_not_outbound"])
        return [make_event("rental_delivered", self._at(p), {"rental_id": p["rental_id"]})]

    def _cmd_return_rental(self, p):
        _need(p, "rental_id", "inspection_id", "warehouse_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] != "delivered":
            raise CommandRejected(["rental_not_delivered"])
        self._check_inspection(p["inspection_id"], rental, "return")
        return [
            make_event(
                "rental_returned",
                self._at(p),
                {"rental_id": p["rental_id"], "inspection_id": p["inspection_id"], "warehouse_id": p["warehouse_id"]},
            )
        ]

    def _check_inspection(self, inspection_id, rental, context):
        insp = self.ledger.inspections.get(inspection_id)
        if insp is None:
            raise CommandRejected(["inspection_unknown"])
        if insp["asset_id"] != rental["asset_id"]:
            raise CommandRejected(["inspection_asset_mismatch"])
        if insp["context"] != context:
            raise CommandRejected(["inspection_context_mismatch"])

    # ------------------------------------------------------------ 结算与争议

    def _cmd_confirm_difference(self, p):
        _need(p, "rental_id", "party")
        rental = self._rental(p["rental_id"])
        if p["party"] not in ("platform", "renter"):
            raise CommandRejected(["party_unknown"])
        if rental["status"] not in ("difference_pending", "difference_confirmed"):
            raise CommandRejected(["difference_not_pending"])
        if p["party"] in rental["confirmations"]:
            raise CommandRejected(["party_already_confirmed"])
        return [make_event("difference_confirmed", self._at(p), {"rental_id": p["rental_id"], "party": p["party"]})]

    def _cmd_settle_rental(self, p):
        _need(p, "rental_id")
        rental = self._rental(p["rental_id"])
        if rental["status"] != "difference_confirmed":
            raise CommandRejected(["difference_not_confirmed"])
        if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
            raise CommandRejected(["dispute_open"])
        return [
            make_event(
                "settlement_completed",
                self._at(p),
                {"rental_id": p["rental_id"], "total_cents": rental["difference"]["total_cents"]},
            )
        ]

    def _cmd_open_dispute(self, p):
        _need(p, "rental_id", "reason")
        rental = self._rental(p["rental_id"])
        if rental["status"] not in ("difference_pending", "difference_confirmed"):
            raise CommandRejected(["rental_not_disputable"])
        if rental["dispute"] and rental["dispute"]["resolved_at"] is None:
            raise CommandRejected(["dispute_already_open"])
        return [make_event("dispute_opened", self._at(p), {"rental_id": p["rental_id"], "reason": p["reason"]})]

    def _cmd_resolve_dispute(self, p):
        _need(p, "rental_id", "resolution")
        rental = self._rental(p["rental_id"])
        dispute = rental["dispute"]
        if not dispute or dispute["resolved_at"] is not None:
            raise CommandRejected(["dispute_not_open"])
        return [make_event("dispute_resolved", self._at(p), {"rental_id": p["rental_id"], "resolution": p["resolution"]})]

    # ------------------------------------------------------------ 阻断状态命令

    def _open_series(self, p, series_map, event_type, label):
        _need(p, "asset_id", "reason")
        self._asset(p["asset_id"])
        series = series_map.get(p["asset_id"], [])
        if series and series[-1]["end"] is None:
            raise CommandRejected([label + "_already_open"])
        return [make_event(event_type, self._at(p), {"asset_id": p["asset_id"], "reason": p["reason"]})]

    def _close_series(self, p, series_map, event_type, label):
        _need(p, "asset_id")
        self._asset(p["asset_id"])
        series = series_map.get(p["asset_id"], [])
        if not series or series[-1]["end"] is not None:
            raise CommandRejected([label + "_not_open"])
        return [make_event(event_type, self._at(p), {"asset_id": p["asset_id"]})]

    def _cmd_start_maintenance(self, p):
        return self._open_series(p, self.ledger.maintenance, "maintenance_started", "maintenance")

    def _cmd_end_maintenance(self, p):
        return self._close_series(p, self.ledger.maintenance, "maintenance_ended", "maintenance")

    def _cmd_issue_recall(self, p):
        return self._open_series(p, self.ledger.recalls, "recall_issued", "recall")

    def _cmd_lift_recall(self, p):
        return self._close_series(p, self.ledger.recalls, "recall_lifted", "recall")

    def _cmd_start_preservation(self, p):
        return self._open_series(p, self.ledger.preservations, "preservation_started", "preservation")

    def _cmd_end_preservation(self, p):
        return self._close_series(p, self.ledger.preservations, "preservation_ended", "preservation")

    # ------------------------------------------------------------ 物流命令

    def _cmd_create_shipment(self, p):
        _need(p, "shipment_id", "asset_id", "from_warehouse", "to_warehouse")
        self._asset(p["asset_id"])
        if p["shipment_id"] in self.ledger.shipments:
            raise CommandRejected(["shipment_exists"])
        at = self._at(p)
        reasons = self.ledger.blocked_reasons(p["asset_id"], at)
        if reasons:
            raise CommandRejected(reasons)  # 维修/召回/保全/争议/在租等不可调度
        custody = self.ledger.custody_at(p["asset_id"], at)
        if custody["holder_id"] != p["from_warehouse"]:
            raise CommandRejected(["asset_not_at_origin"])
        return [
            make_event(
                "shipment_created",
                at,
                {
                    "shipment_id": p["shipment_id"],
                    "asset_id": p["asset_id"],
                    "from_warehouse": p["from_warehouse"],
                    "to_warehouse": p["to_warehouse"],
                },
            )
        ]

    def _cmd_record_scan(self, p):
        _need(p, "shipment_id", "scan_type", "warehouse_id")
        if p["shipment_id"] not in self.ledger.shipments:
            raise CommandRejected(["shipment_unknown"])
        if p["scan_type"] not in SCAN_TYPES:
            raise CommandRejected(["scan_type_unknown"])
        event = make_event(
            "shipment_scan",
            self._at(p),
            {"shipment_id": p["shipment_id"], "scan_type": p["scan_type"], "warehouse_id": p["warehouse_id"]},
        )
        if self.store.contains(event.id):
            return []  # 重复回调：幂等
        return [event]

    # ------------------------------------------------------------ 免押政策

    def _cmd_update_waiver_policy(self, p):
        _need(p, "subject", "eligible", "effective_from")
        if not isinstance(p["eligible"], bool):
            raise CommandRejected(["eligible_not_bool"])
        effective_from = format_instant(parse_instant(p["effective_from"]))
        for policy in self.ledger.waiver_policies.get(p["subject"], []):
            if policy["effective_from"] == effective_from:
                raise CommandRejected(["policy_exists"])
        return [
            make_event(
                "waiver_policy_updated",
                self._at(p),
                {"subject": p["subject"], "eligible": p["eligible"], "effective_from": effective_from},
            )
        ]

    # ------------------------------------------------------------ 查询

    def query(self, name: str, params: dict | None = None) -> dict:
        with self._lock:
            if name not in QUERIES:
                raise UnknownQuery(name)
            params = params or {}
            at = params.get("at")
            ledger = self.ledger_at(at) if at else self.ledger
            at = at or now_iso()
            return self._run_query(name, ledger, at, params)

    def _run_query(self, name, ledger, at, params):
        if name == "custody_at":
            _need(params, "asset_id")
            return {"asset_id": params["asset_id"], "at": at, "custody": ledger.custody_at(params["asset_id"], at)}
        if name == "circulation_eligibility":
            _need(params, "asset_id")
            reasons = ledger.blocked_reasons(params["asset_id"], at)
            return {"asset_id": params["asset_id"], "at": at, "eligible": not reasons, "reasons": reasons}
        if name == "standard_at":
            _need(params, "category")
            return {"category": params["category"], "at": at, "standard": ledger.standard_at(params["category"], at)}
        if name == "asset":
            _need(params, "asset_id")
            return {"asset": ledger.assets.get(params["asset_id"])}
        if name == "rental":
            _need(params, "rental_id")
            return {"rental": ledger.rentals.get(params["rental_id"])}
        if name == "shipment":
            _need(params, "shipment_id")
            return {"shipment": ledger.shipments.get(params["shipment_id"])}
        if name == "lifecycle_stats":
            return stats_mod.lifecycle_stats(ledger, at)
        if name == "ewaste_stats":
            return stats_mod.ewaste_stats(ledger, at)
        if name == "ledger_digest":
            return {"digest": ledger.digest(), "at": at, "events": len(self.store)}
        if name == "verify_replay":
            fresh = self.replay()
            return {
                "converged": fresh.digest() == self.ledger.digest(),
                "digest": self.ledger.digest(),
                "events": len(self.store),
            }
        if name == "events":
            return {"events": [e.as_dict() for e in self.store.canonical()]}
        raise UnknownQuery(name)  # pragma: no cover
