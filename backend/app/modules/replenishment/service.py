"""Replenishment recommendations: inputs come from real balances, open POs, forecasts and supplier terms."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import select

from app.core.exceptions import InvalidState, NotFound
from app.domain.forecasting.demand import prepare_demand
from app.domain.replenishment import ReplenishmentInput, apply_budget, compute_recommendation
from app.modules.common import Ctx, D, money
from app.modules.forecasting.service import _net_daily, _stockout_days
from app.modules.models import (ForecastResult, ForecastRun, InventoryBalance, Recommendation, SupplierProduct,
                                Variant, Warehouse, utcnow)
from app.modules.purchasing.service import create_po, on_order_by_variant

EXPIRY_DAYS = 7


def generate(ctx: Ctx, branch_id, service_level: float = 0.95, review_period_days: int = 7,
             budget: float | None = None) -> list[Recommendation]:
    ctx.require("replenishment.decide")
    branch = ctx.check_branch(branch_id)
    if not 0.5 <= service_level < 1:
        raise InvalidState("service_level must be in [0.5, 1).")
    whs = list(ctx.db.scalars(select(Warehouse).where(Warehouse.organization_id == ctx.org_id,
                                                      Warehouse.branch_id == branch.id, Warehouse.status == "active")
                              .order_by(Warehouse.created_at)))
    if not whs:
        raise InvalidState("Branch has no active warehouse.")
    wh_ids = [w.id for w in whs]
    for old in ctx.db.scalars(select(Recommendation).where(
            Recommendation.organization_id == ctx.org_id, Recommendation.branch_id == branch.id,
            Recommendation.status == "pending")):
        old.status = "expired"          # superseded by this refresh

    bal = {}
    for b in ctx.db.scalars(select(InventoryBalance).where(InventoryBalance.organization_id == ctx.org_id,
                                                           InventoryBalance.warehouse_id.in_(wh_ids))):
        cur = bal.setdefault(b.variant_id, [Decimal(0), Decimal(0)])
        cur[0] += b.on_hand_quantity
        cur[1] += b.reserved_quantity
    on_order = on_order_by_variant(ctx, branch.id)

    # preferred supplier term per variant
    terms: dict = {}
    for sp in ctx.db.scalars(select(SupplierProduct).where(SupplierProduct.organization_id == ctx.org_id)
                             .order_by(SupplierProduct.is_preferred.desc(), SupplierProduct.lead_time_days)):
        terms.setdefault(sp.variant_id, sp)
    variants = {v.id: v for v in ctx.db.scalars(select(Variant).where(Variant.organization_id == ctx.org_id,
                                                                       Variant.status == "active",
                                                                       Variant.id.in_(list(terms) or [None])))}

    run = ctx.db.scalar(select(ForecastRun).where(ForecastRun.organization_id == ctx.org_id,
                                                  ForecastRun.branch_id == branch.id,
                                                  ForecastRun.status == "completed")
                        .order_by(ForecastRun.completed_at.desc()))
    fc: dict = {}
    if run:
        for r in ctx.db.scalars(select(ForecastResult).where(ForecastResult.forecast_run_id == run.id)
                                .order_by(ForecastResult.forecast_period)):
            per_day = float(r.predicted_quantity) / (7 if run.granularity == "weekly" else 1)
            span = 7 if run.granularity == "weekly" else 1
            fc.setdefault(r.variant_id, []).extend([per_day] * span)

    today = date.today()
    start = today - timedelta(days=90)
    demand = _net_daily(ctx.db, ctx.org_id, branch.id, start, today - timedelta(days=1))
    stockouts = _stockout_days(ctx.db, ctx.org_id, branch.id, start, today - timedelta(days=1))

    computed = []
    for vid, sp in terms.items():
        v = variants.get(vid)
        if not v:
            continue
        series = prepare_demand(demand.get(vid, {}), start, today - timedelta(days=1), stockouts.get(vid, set()))
        std = float(np.std(series.fit_series)) if series.n > 1 else 0.0
        warnings = []
        daily = fc.get(vid)
        if not daily:
            recent = series.fit_series[-28:]
            daily = [float(np.mean(recent))] * 30 if len(recent) else []
            warnings.append("No forecast run covers this item; using its 28-day average demand.")
        on_hand, reserved = bal.get(vid, [Decimal(0), Decimal(0)])
        if not (series.fit_series.sum() > 0) and on_hand <= 0:
            continue  # never sold and no stock: nothing to recommend
        inp = ReplenishmentInput(
            on_hand=float(on_hand), reserved=float(reserved), on_order=float(on_order.get(vid, 0)),
            daily_forecast=daily, demand_std_daily=std, lead_time_days=sp.lead_time_days,
            review_period_days=review_period_days, service_level=service_level, moq=float(sp.minimum_order_quantity),
            pack_size=float(sp.pack_size), unit_cost=float(sp.unit_cost), shelf_life_days=v.product.shelf_life_days)
        rec = compute_recommendation(inp)
        rec.warnings.extend(warnings)
        computed.append((vid, sp, rec, inp))

    qty_override = {}
    if budget is not None:
        qty_override = apply_budget([(str(vid), rec, float(sp.pack_size), float(sp.unit_cost))
                                     for vid, sp, rec, _ in computed], float(budget))
    out = []
    for vid, sp, rec, inp in computed:
        q = rec.recommended_quantity
        if budget is not None and qty_override.get(str(vid), q) < q:
            q = qty_override[str(vid)]
            rec.warnings.append(f"Quantity reduced to fit budget {budget:,.2f}.")
        if q <= 0:
            continue
        details = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.__dict__.items()
                   if k not in ("reason", "warnings")}
        details.update({"warnings": rec.warnings, "forecast_source": "forecast_run" if vid in fc else "recent_average",
                        "lead_time_days": sp.lead_time_days, "moq": float(sp.minimum_order_quantity),
                        "pack_size": float(sp.pack_size), "service_level": service_level,
                        "review_period_days": review_period_days})
        r = Recommendation(organization_id=ctx.org_id, branch_id=branch.id, warehouse_id=whs[0].id, variant_id=vid,
                           supplier_id=sp.supplier_id, recommended_quantity=D(q), estimated_cost=money(q * float(sp.unit_cost)),
                           urgency=rec.urgency, reason=rec.reason, details=details,
                           expires_at=utcnow() + timedelta(days=EXPIRY_DAYS))
        ctx.db.add(r)
        out.append(r)
    ctx.db.flush()
    ctx.audit("replenishment.generate", "branch", branch.id, count=len(out))
    return out


def _get(ctx: Ctx, rec_id) -> Recommendation:
    r = ctx.db.scalar(select(Recommendation).where(Recommendation.id == rec_id,
                                                   Recommendation.organization_id == ctx.org_id))
    if not r:
        raise NotFound("Recommendation not found.")
    ctx.check_branch(r.branch_id)
    return r


def decide(ctx: Ctx, rec_id, accept: bool) -> Recommendation:
    ctx.require("replenishment.decide")
    r = _get(ctx, rec_id)
    if r.status != "pending":
        raise InvalidState(f"Recommendation is already {r.status}.")
    r.status, r.decided_by = ("accepted" if accept else "rejected"), ctx.user_id
    ctx.audit("replenishment.accept" if accept else "replenishment.reject", "recommendation", r.id)
    return r


def convert_to_draft_po(ctx: Ctx, rec_ids: list) -> list:
    """Accepted recommendations -> DRAFT purchase orders (one per supplier+warehouse). Never submits or approves."""
    ctx.require("replenishment.decide")
    ctx.require("purchasing.create")
    recs = [_get(ctx, i) for i in rec_ids]
    if any(r.status != "accepted" for r in recs):
        raise InvalidState("Only accepted recommendations can be converted.")
    if any(r.supplier_id is None for r in recs):
        raise InvalidState("Recommendation has no supplier.")
    groups: dict = {}
    for r in recs:
        groups.setdefault((r.supplier_id, r.branch_id, r.warehouse_id), []).append(r)
    pos = []
    for (sid, bid, wid), rs in groups.items():
        sp = {s.variant_id: s for s in ctx.db.scalars(select(SupplierProduct).where(
            SupplierProduct.organization_id == ctx.org_id, SupplierProduct.supplier_id == sid))}
        lead = max((sp[r.variant_id].lead_time_days for r in rs if r.variant_id in sp), default=7)
        po = create_po(ctx, branch_id=bid, warehouse_id=wid, supplier_id=sid,
                       expected_date=date.today() + timedelta(days=lead),
                       items=[{"variant_id": r.variant_id, "ordered_quantity": r.recommended_quantity,
                               "unit_cost": sp[r.variant_id].unit_cost if r.variant_id in sp else None} for r in rs])
        for r in rs:
            r.status, r.purchase_order_id = "converted_to_po", po.id
        pos.append(po)
    ctx.audit("replenishment.convert", "purchase_order", None, count=len(pos))
    return pos
