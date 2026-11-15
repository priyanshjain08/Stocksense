"""Forecast runs: build demand series from real sales + ledger, run the models, persist results."""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import InvalidState, NotFound
from app.domain.forecasting.demand import PreparedDemand, prepare_demand
from app.domain.forecasting.engine import run_forecast
from app.modules.common import Ctx
from app.modules.models import (ForecastResult, ForecastRun, Sale, SaleItem, SalesReturn, SalesReturnItem,
                                StockMovement, Variant, Warehouse, utcnow)

HORIZONS = (7, 30, 60, 90)
HISTORY_DAYS = 365


def _net_daily(db: Session, org_id, branch_id, start: date, end: date) -> dict[uuid.UUID, dict[date, float]]:
    out: dict = defaultdict(lambda: defaultdict(float))
    sold = db.execute(select(SaleItem.variant_id, Sale.sale_date, func.sum(SaleItem.quantity))
                      .join(Sale, Sale.id == SaleItem.sale_id)
                      .where(Sale.organization_id == org_id, Sale.branch_id == branch_id, Sale.status == "completed",
                             Sale.sale_date.between(start, end)).group_by(SaleItem.variant_id, Sale.sale_date))
    for v, d, q in sold:
        out[v][d] += float(q)
    ret = db.execute(select(SalesReturnItem.variant_id, SalesReturn.return_date, func.sum(SalesReturnItem.quantity))
                     .join(SalesReturn, SalesReturn.id == SalesReturnItem.return_id)
                     .join(Sale, Sale.id == SalesReturn.sale_id)
                     .where(SalesReturn.organization_id == org_id, Sale.branch_id == branch_id,
                            SalesReturn.return_date.between(start, end))
                     .group_by(SalesReturnItem.variant_id, SalesReturn.return_date))
    for v, d, q in ret:
        out[v][d] -= float(q)   # returns reduce net demand on the return date
    return out


def _stockout_days(db: Session, org_id, branch_id, start: date, end: date) -> dict[uuid.UUID, set[date]]:
    """Days where end-of-day sellable stock was <= 0 (after the first recorded movement). Zero-sale days
    in this set are treated as censored demand, not true zeros."""
    wh_ids = [w for (w,) in db.execute(select(Warehouse.id).where(Warehouse.organization_id == org_id,
                                                                  Warehouse.branch_id == branch_id))]
    if not wh_ids:
        return {}
    rows = db.execute(select(StockMovement.variant_id, StockMovement.occurred_at, StockMovement.quantity_delta)
                      .where(StockMovement.organization_id == org_id, StockMovement.warehouse_id.in_(wh_ids),
                             StockMovement.bucket == "sellable").order_by(StockMovement.occurred_at))
    per: dict = defaultdict(list)
    for v, t, q in rows:
        per[v].append((t.date(), float(q)))
    result: dict = {}
    for v, mv in per.items():
        daily: dict[date, float] = defaultdict(float)
        for d, q in mv:
            daily[d] += q
        cum, out, day = 0.0, set(), min(daily)
        while day <= end:
            cum += daily.get(day, 0.0)
            if cum <= 1e-9 and day >= start:
                out.add(day)
            day += timedelta(days=1)
        result[v] = out
    return result


def create_run(ctx: Ctx, branch_id, horizon_days: int, granularity: str = "daily",
               variant_ids: list | None = None, history_days: int = 180) -> ForecastRun:
    ctx.require("forecast.run")
    ctx.check_branch(branch_id)
    if horizon_days not in HORIZONS:
        raise InvalidState(f"horizon_days must be one of {HORIZONS}.")
    if granularity not in ("daily", "weekly"):
        raise InvalidState("granularity must be daily or weekly.")
    run = ForecastRun(organization_id=ctx.org_id, branch_id=branch_id, horizon_days=horizon_days,
                      granularity=granularity, created_by=ctx.user_id,
                      configuration={"variant_ids": [str(v) for v in variant_ids] if variant_ids else None,
                                     "history_days": min(history_days, HISTORY_DAYS)})
    ctx.db.add(run)
    ctx.db.flush()
    ctx.audit("forecast.create", "forecast_run", run.id, horizon=horizon_days)
    return run


def execute_run(db: Session, run_id) -> ForecastRun:
    """Executed inline or by the Celery worker. Failures are recorded on the run with a useful message."""
    run = db.get(ForecastRun, run_id)
    if not run:
        raise NotFound("Forecast run not found.")
    run.status = "running"
    db.commit()
    try:
        today = date.today()
        hist_days = run.configuration.get("history_days", 180)
        start = today - timedelta(days=hist_days)
        end = today - timedelta(days=1)
        demand = _net_daily(db, run.organization_id, run.branch_id, start, end)
        stockouts = _stockout_days(db, run.organization_id, run.branch_id, start, end)
        wanted = run.configuration.get("variant_ids")
        variant_ids = [uuid.UUID(v) for v in wanted] if wanted else list(demand.keys())
        valid = set(db.scalars(select(Variant.id).where(Variant.organization_id == run.organization_id,
                                                        Variant.id.in_(variant_ids))))
        summary = {"variants_forecast": 0, "variants_skipped": 0, "models": {}, "insufficient": 0}
        for vid in variant_ids:
            if vid not in valid:
                summary["variants_skipped"] += 1
                continue
            vd = demand.get(vid, {})
            first = min(vd) if vd else start
            series: PreparedDemand = prepare_demand(vd, max(start, first), end, stockouts.get(vid, set()))
            out = run_forecast(series.fit_series, run.horizon_days)
            summary["variants_forecast"] += 1
            summary["models"][out.selected_model] = summary["models"].get(out.selected_model, 0) + 1
            summary["insufficient"] += out.sufficiency == "insufficient"
            meta = {"sufficiency": out.sufficiency, "warnings": out.warnings + [f"data:{f}" for f in series.flags],
                    "training_points": out.training_points, "baseline": out.baseline_model,
                    "evaluation": [{"model": e.model_name, "folds": e.folds, **e.metrics} for e in out.evaluation],
                    "params": out.forecast.params, "training_window": [str(series.start), str(end)]}
            f = out.forecast
            if run.granularity == "daily":
                for i in range(run.horizon_days):
                    db.add(ForecastResult(organization_id=run.organization_id, forecast_run_id=run.id, variant_id=vid,
                                          forecast_period=today + timedelta(days=i),
                                          predicted_quantity=Decimal(str(round(float(f.mean[i]), 3))),
                                          lower_bound=Decimal(str(round(float(f.lower[i]), 3))),
                                          upper_bound=Decimal(str(round(float(f.upper[i]), 3))),
                                          model_name=f.model_name, confidence_metadata=meta if i == 0 else {}))
            else:  # weekly: sum daily means; interval half-widths add in quadrature (independence assumption)
                for w, i in enumerate(range(0, run.horizon_days, 7)):
                    sl = slice(i, min(i + 7, run.horizon_days))
                    mean = float(np.sum(f.mean[sl]))
                    hw_lo = float(np.sqrt(np.sum((f.mean[sl] - f.lower[sl]) ** 2)))
                    hw_hi = float(np.sqrt(np.sum((f.upper[sl] - f.mean[sl]) ** 2)))
                    db.add(ForecastResult(organization_id=run.organization_id, forecast_run_id=run.id, variant_id=vid,
                                          forecast_period=today + timedelta(days=i),
                                          predicted_quantity=Decimal(str(round(mean, 3))),
                                          lower_bound=Decimal(str(round(max(mean - hw_lo, 0), 3))),
                                          upper_bound=Decimal(str(round(mean + hw_hi, 3))),
                                          model_name=f.model_name, confidence_metadata=meta if w == 0 else {}))
        run.metrics, run.status, run.completed_at = summary, "completed", utcnow()
    except Exception as e:  # noqa: BLE001 - recorded for the user, then re-raised for logs
        db.rollback()
        run = db.get(ForecastRun, run_id)
        run.status, run.error = "failed", f"{type(e).__name__}: {e}"
        db.commit()
        raise
    db.commit()
    return run


def get_run(ctx: Ctx, run_id) -> ForecastRun:
    ctx.require("forecast.read")
    run = ctx.db.scalar(select(ForecastRun).where(ForecastRun.id == run_id,
                                                  ForecastRun.organization_id == ctx.org_id))
    if not run:
        raise NotFound("Forecast run not found.")
    ctx.check_branch(run.branch_id)
    return run


def run_results(ctx: Ctx, run_id, variant_id=None) -> list[ForecastResult]:
    run = get_run(ctx, run_id)
    q = select(ForecastResult).where(ForecastResult.forecast_run_id == run.id,
                                     ForecastResult.organization_id == ctx.org_id)
    if variant_id:
        q = q.where(ForecastResult.variant_id == variant_id)
    return list(ctx.db.scalars(q.order_by(ForecastResult.variant_id, ForecastResult.forecast_period)))


def history_with_actuals(ctx: Ctx, branch_id, variant_id, days: int = 90) -> list[dict]:
    """Actual net demand per day, for the actual-vs-predicted chart."""
    ctx.require("forecast.read")
    ctx.check_branch(branch_id)
    end = date.today() - timedelta(days=1)
    d = _net_daily(ctx.db, ctx.org_id, branch_id, end - timedelta(days=days), end).get(variant_id, {})
    return [{"date": str(end - timedelta(days=i)), "actual": max(d.get(end - timedelta(days=i), 0.0), 0.0)}
            for i in range(days, -1, -1)]
