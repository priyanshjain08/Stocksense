"""Transparent replenishment calculations.

ROP = demand during lead time + safety stock
IP  = on_hand + on_order - reserved
Q   = max(0, target - IP), then MOQ, pack size, shelf-life, capacity and budget constraints.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist


@dataclass
class ReplenishmentInput:
    on_hand: float                 # sellable on-hand only (exclude damaged/quarantined/expired)
    reserved: float
    on_order: float                # open, not-yet-received PO quantity
    daily_forecast: list[float]    # forecast per day, at least lead_time + review_period long preferred
    demand_std_daily: float        # std dev of daily demand (history residuals)
    lead_time_days: int
    review_period_days: int = 7
    service_level: float = 0.95
    lead_time_std_days: float = 0.0
    moq: float = 0.0
    pack_size: float = 1.0
    unit_cost: float = 0.0
    shelf_life_days: int | None = None
    max_capacity: float | None = None   # max units allowed in stock for this item


@dataclass
class Recommendation:
    on_hand: float
    reserved: float
    on_order: float
    inventory_position: float
    lead_time_demand: float
    safety_stock: float
    reorder_point: float
    target_stock: float
    raw_quantity: float
    recommended_quantity: float
    estimated_cost: float
    days_of_cover: float | None
    urgency: str                    # critical | high | medium | low | none
    reason: str
    warnings: list[str] = field(default_factory=list)


def _horizon_sum(fc: list[float], days: int) -> float:
    if days <= 0:
        return 0.0
    if not fc:
        return 0.0
    if len(fc) >= days:
        return float(sum(fc[:days]))
    avg = sum(fc) / len(fc)             # extend with the average if forecast is shorter
    return float(sum(fc) + avg * (days - len(fc)))


def round_to_pack(qty: float, pack: float) -> float:
    if qty <= 0:
        return 0.0
    pack = pack if pack and pack > 0 else 1.0
    return float(math.ceil(qty / pack - 1e-9) * pack)


def compute_recommendation(i: ReplenishmentInput) -> Recommendation:
    warnings: list[str] = []
    ip = i.on_hand + i.on_order - i.reserved
    ltd = _horizon_sum(i.daily_forecast, i.lead_time_days)
    review = _horizon_sum(i.daily_forecast, i.review_period_days)
    avg_daily = (sum(i.daily_forecast) / len(i.daily_forecast)) if i.daily_forecast else 0.0

    z = NormalDist().inv_cdf(min(max(i.service_level, 0.5), 0.9999))
    var = i.lead_time_days * i.demand_std_daily ** 2 + (avg_daily ** 2) * (i.lead_time_std_days ** 2)
    safety = z * math.sqrt(var)
    rop = ltd + safety
    target = ltd + review + safety

    available = i.on_hand - i.reserved
    cover = (available / avg_daily) if avg_daily > 0 else None

    raw = max(0.0, target - ip) if ip <= rop else 0.0

    qty = raw
    if qty > 0 and i.moq and qty < i.moq:
        qty = i.moq
        warnings.append(f"Raised to supplier minimum order quantity ({i.moq:g}).")
    qty = round_to_pack(qty, i.pack_size)

    if qty > 0 and i.shelf_life_days and avg_daily > 0:
        cap_units = avg_daily * i.shelf_life_days
        if ip + qty > cap_units:
            allowed = max(0.0, cap_units - ip)
            warnings.append(f"Shelf life ({i.shelf_life_days}d) limits stock to ~{cap_units:.0f} units; quantity reduced.")
            qty = math.floor(allowed / (i.pack_size or 1)) * (i.pack_size or 1)
    if qty > 0 and i.max_capacity is not None and ip + qty > i.max_capacity:
        allowed = max(0.0, i.max_capacity - ip)
        warnings.append(f"Warehouse capacity ({i.max_capacity:g}) limits quantity.")
        qty = math.floor(allowed / (i.pack_size or 1)) * (i.pack_size or 1)
    if i.moq and 0 < qty < i.moq:
        warnings.append("Constraints reduce the order below the supplier minimum; review manually.")
    if not i.daily_forecast:
        warnings.append("No forecast available; quantities are based on zero expected demand.")

    if qty <= 0 and raw <= 0:
        urgency = "none"
    elif cover is not None and cover < i.lead_time_days * 0.5:
        urgency = "critical"
    elif cover is not None and cover < i.lead_time_days:
        urgency = "high"
    elif ip <= rop:
        urgency = "medium"
    else:
        urgency = "low"
    if i.on_hand <= 0 and (qty > 0):
        urgency = "critical"

    reason = (f"Inventory position {ip:g} (on hand {i.on_hand:g} + on order {i.on_order:g} - reserved {i.reserved:g}). "
              f"Expected demand over {i.lead_time_days}d lead time = {ltd:.1f}; safety stock = {safety:.1f} "
              f"(service level {i.service_level:.0%}, z={z:.2f}); reorder point = {rop:.1f}; "
              f"target stock = {target:.1f}. ")
    reason += (f"IP is at or below the reorder point, so order {qty:g}." if qty > 0
               else ("IP is above the reorder point; no order needed." if ip > rop else "No quantity after constraints."))
    return Recommendation(i.on_hand, i.reserved, i.on_order, ip, ltd, safety, rop, target, raw, qty,
                          qty * i.unit_cost, cover, urgency, reason, warnings)


_URG = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}


def apply_budget(recs: list[tuple[str, Recommendation, float, float]], budget: float):
    """recs: (key, rec, pack_size, unit_cost). Greedy by urgency then lowest days of cover.
    Returns {key: affordable_qty}. Quantities shrink in whole packs; items never exceed budget."""
    left = budget
    out: dict[str, float] = {}
    ordered = sorted(recs, key=lambda r: (_URG[r[1].urgency], r[1].days_of_cover if r[1].days_of_cover is not None else 1e9))
    for key, rec, pack, cost in ordered:
        q = rec.recommended_quantity
        if q <= 0:
            out[key] = 0.0
            continue
        pack = pack or 1.0
        if cost <= 0:
            out[key] = q
            continue
        affordable = math.floor(min(q, left / cost) / pack + 1e-9) * pack
        out[key] = max(0.0, affordable)
        left -= out[key] * cost
    return out
