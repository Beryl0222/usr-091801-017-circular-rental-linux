"""生命周期与电子废弃物减量统计：只从事件派生，不接受客户端上报。

没有对应的写入命令——任何"上报统计值"的入口在设计上就不存在。
"""

from __future__ import annotations

from .core import now_iso, parse_instant

SECONDS_PER_DAY = 86400

# 减量折算常量（公开、可调，公式见 README）
PER_RENTAL_AVOIDED_KG = 0.5        # 每完成一次租用，替代一次新机制造的折算
PER_REFURBISHMENT_AVOIDED_KG = 2.0  # 每完成一次维修延寿，避免整机报废的折算


def _seconds(start: str, end: str) -> float:
    return (parse_instant(end) - parse_instant(start)).total_seconds()


def lifecycle_stats(ledger, at: str | None = None) -> dict:
    """每件资产的流转画像：租次、在租时长、保管分布、维修与终态。"""
    at = at or now_iso()
    at_dt = parse_instant(at)
    assets = []
    for asset_id in sorted(ledger.assets):
        asset = ledger.assets[asset_id]
        rentals = [r for r in ledger.rentals.values() if r["asset_id"] == asset_id]
        completed = [r for r in rentals if r["returned_at"]]
        rental_seconds = sum(
            _seconds(r["delivered_at"], r["returned_at"]) for r in completed if r["delivered_at"]
        )
        custody_days: dict[str, float] = {}
        for interval in ledger.custody.get(asset_id, []):
            start = parse_instant(interval["start"])
            end = parse_instant(interval["end"]) if interval["end"] else at_dt
            end = min(end, at_dt)
            if end > start:
                days = (end - start).total_seconds() / SECONDS_PER_DAY
                custody_days[interval["role"]] = round(custody_days.get(interval["role"], 0.0) + days, 6)
        maintenance = ledger.maintenance.get(asset_id, [])
        assets.append(
            {
                "asset_id": asset_id,
                "category": asset["category"],
                "status": "scrapped" if asset["scrapped"] else "active",
                "registered_at": asset["registered_at"],
                "scrapped_at": asset["scrapped"]["at"] if asset["scrapped"] else None,
                "rentals_total": len([r for r in rentals if r["confirmed_at"]]),
                "rentals_completed": len(completed),
                "rental_days": round(rental_seconds / SECONDS_PER_DAY, 6),
                "inspections": len([i for i in ledger.inspections.values() if i["asset_id"] == asset_id]),
                "maintenance_episodes": len(maintenance),
                "shipments": len([s for s in ledger.shipments.values() if s["asset_id"] == asset_id]),
                "custody_days": custody_days,
            }
        )
    return {
        "at": at,
        "assets": assets,
        "totals": {
            "assets": len(assets),
            "rentals_completed": sum(a["rentals_completed"] for a in assets),
            "rental_days": round(sum(a["rental_days"] for a in assets), 6),
        },
        "source": "derived_from_events",
    }


def ewaste_stats(ledger, at: str | None = None) -> dict:
    """电子废弃物减量：由完成租次与维修延寿次数按公开公式折算。"""
    at = at or now_iso()
    completed_rentals = len([r for r in ledger.rentals.values() if r["returned_at"]])
    refurbishments = sum(
        1 for series in ledger.maintenance.values() for episode in series if episode["end"]
    )
    assets_active = len([a for a in ledger.assets.values() if not a["scrapped"]])
    assets_scrapped = len(ledger.assets) - assets_active
    avoided = completed_rentals * PER_RENTAL_AVOIDED_KG + refurbishments * PER_REFURBISHMENT_AVOIDED_KG
    return {
        "at": at,
        "completed_rentals": completed_rentals,
        "refurbishments": refurbishments,
        "assets_active": assets_active,
        "assets_scrapped": assets_scrapped,
        "avoided_waste_kg": round(avoided, 6),
        "formula": {
            "per_rental_avoided_kg": PER_RENTAL_AVOIDED_KG,
            "per_refurbishment_avoided_kg": PER_REFURBISHMENT_AVOIDED_KG,
        },
        "source": "derived_from_events",
    }
