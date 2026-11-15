"""Overview + inventory-health analytics. Definitions are documented in docs/DATABASE.md."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.domain.analytics import abc_classification, days_of_inventory, inventory_turnover, is_dead_stock, stock_status
from app.modules.common import Ctx, D
from app.modules.models import (InventoryBalance, Organization, Recommendation, Sale, SaleItem, Variant, Warehouse)

DEFAULTS = {"low_stock_threshold": 5, "overstock_days": 180, "dead_stock_days": 90, "slow_units_per_30d": 1.0}


def _settings(ctx: Ctx) -> dict:
    org = ctx.db.get(Organization, ctx.org_id)
    return {**DEFAULTS, **(org.settings or {}).get("thresholds", {})}


def _scope_branch_ids(ctx: Ctx, branch_id):
    if branch_id:
        ctx.check_branch(branch_id)
        return [branch_id]
    allowed = ctx.allowed_branches
    if allowed is None:
        return None
    return [uuid.UUID(b) for b in allowed] or [uuid.uuid4()]  # empty scope matches nothing


def _balances(ctx: Ctx, branch_ids):
    q = select(InventoryBalance.variant_id, func.sum(InventoryBalance.on_hand_quantity),
              func.sum(InventoryBalance.unavailable_quantity)).where(InventoryBalance.organization_id == ctx.org_id)
    if branch_ids is not None:
        q = q.join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id).where(Warehouse.branch_id.in_(branch_ids))
    return {v: (D(a), D(u)) for v, a, u in ctx.db.execute(q.group_by(InventoryBalance.variant_id))}


def _sales_rows(ctx: Ctx, branch_ids, start: date, end: date):
    q = (select(SaleItem.variant_id, Sale.sale_date, SaleItem.quantity, SaleItem.line_total, SaleItem.cost_snapshot)
         .join(Sale, Sale.id == SaleItem.sale_id)
         .where(Sale.organization_id == ctx.org_id, Sale.status == "completed", Sale.sale_date.between(start, end)))
    if branch_ids is not None:
        q = q.where(Sale.branch_id.in_(branch_ids))
    return ctx.db.execute(q).all()


def inventory_health(ctx: Ctx, branch_id=None, days: int = 90) -> dict:
    ctx.require("analytics.read")
    cfg = _settings(ctx)
    bids = _scope_branch_ids(ctx, branch_id)
    today = date.today()
    start = today - timedelta(days=days - 1)
    bal = _balances(ctx, bids)
    units, revenue, last_sale, cogs = defaultdict(float), defaultdict(float), {}, 0.0
    for vid, d, q, line, cost in _sales_rows(ctx, bids, start, today):
        units[vid] += float(q)
        revenue[vid] += float(line)
        cogs += float(q) * float(cost)
        last_sale[vid] = max(last_sale.get(vid, d), d)
    variants = ctx.db.scalars(select(Variant).where(Variant.organization_id == ctx.org_id, Variant.status == "active"))
    abc = abc_classification({str(v): r for v, r in revenue.items()})
    rows, totals = [], defaultdict(float)
    for v in variants:
        on_hand, unavailable = bal.get(v.id, (Decimal(0), Decimal(0)))
        cost = float(v.cost)
        avg_daily = units.get(v.id, 0.0) / days
        status = stock_status(float(on_hand), cfg["low_stock_threshold"], avg_daily, cfg["overstock_days"])
        dead = is_dead_stock(float(on_hand), last_sale.get(v.id), today, cfg["dead_stock_days"]) \
            if (days >= cfg["dead_stock_days"]) else None
        value = float(on_hand) * cost
        totals["valuation"] += value
        totals["unavailable_value"] += float(unavailable) * cost
        totals["sellable_units"] += float(on_hand)
        totals[status] += 1
        if dead:
            totals["dead_stock_value_estimate"] += value
            totals["dead_stock_count"] += 1
        slow = float(on_hand) > 0 and (units.get(v.id, 0.0) / days) * 30 < cfg["slow_units_per_30d"]
        totals["slow_moving_count"] += bool(slow)
        rows.append({"variant_id": str(v.id), "sku": v.sku, "name": v.product.name, "on_hand": float(on_hand),
                     "unavailable": float(unavailable), "value_at_cost": round(value, 2), "status": status,
                     "units_sold_period": units.get(v.id, 0.0),
                     "days_of_inventory": days_of_inventory(float(on_hand), avg_daily),
                     "last_sale": str(last_sale[v.id]) if v.id in last_sale else None, "abc": abc.get(str(v.id), "C"),
                     "dead_stock_estimate": bool(dead), "slow_moving": bool(slow)})
    turnover = inventory_turnover(cogs, totals["valuation"])
    return {"period_days": days, "thresholds": cfg, "items": rows,
            "summary": {"inventory_valuation_at_cost": round(totals["valuation"], 2),
                        "unavailable_stock_value_at_cost": round(totals["unavailable_value"], 2),
                        "sellable_units": totals["sellable_units"], "low_stock_count": int(totals["low_stock"]),
                        "out_of_stock_count": int(totals["out_of_stock"]), "overstock_count": int(totals["overstock"]),
                        "slow_moving_count": int(totals["slow_moving_count"]),
                        "dead_stock_count": int(totals["dead_stock_count"]),
                        "dead_stock_value_estimate": round(totals["dead_stock_value_estimate"], 2),
                        "cogs_period": round(cogs, 2),
                        "turnover_period": turnover,
                        "turnover_note": "COGS over the period divided by CURRENT inventory value at cost "
                                         "(proxy for average inventory; no historical valuation snapshots yet)."}}


def overview(ctx: Ctx, branch_id=None, days: int = 30) -> dict:
    ctx.require("analytics.read")
    bids = _scope_branch_ids(ctx, branch_id)
    today = date.today()
    start = today - timedelta(days=days - 1)
    rows = _sales_rows(ctx, bids, start, today)
    trend = defaultdict(lambda: {"revenue": 0.0, "units": 0.0})
    cost_total = rev_total = 0.0
    for _, d, q, line, cost in rows:
        trend[d]["revenue"] += float(line)
        trend[d]["units"] += float(q)
        rev_total += float(line)
        cost_total += float(q) * float(cost)
    health = inventory_health(ctx, branch_id, max(days, 90))
    s = health["summary"]
    active_products = ctx.db.scalar(select(func.count()).select_from(Variant)
                                    .where(Variant.organization_id == ctx.org_id, Variant.status == "active"))
    prio = ctx.db.scalars(select(Recommendation).where(
        Recommendation.organization_id == ctx.org_id, Recommendation.status == "pending",
        Recommendation.urgency.in_(("critical", "high"))).limit(10)).all()
    return {"period_days": days,
            "active_variants": active_products, **{k: s[k] for k in (
                "inventory_valuation_at_cost", "sellable_units", "low_stock_count", "out_of_stock_count",
                "overstock_count", "dead_stock_count", "dead_stock_value_estimate")},
            "sales_revenue": round(rev_total, 2), "estimated_gross_margin": round(rev_total - cost_total, 2),
            "margin_note": "Revenue less cost snapshots, before returns; not profit.",
            "sales_trend": [{"date": str(d), **v} for d, v in sorted(trend.items())],
            "replenishment_priorities": [{"id": str(r.id), "variant_id": str(r.variant_id), "urgency": r.urgency,
                                          "quantity": float(r.recommended_quantity)} for r in prio]}
