"""Pure-domain tests (forecasting, replenishment, analytics). No DB/framework needed."""
import math
from datetime import date, timedelta

import numpy as np

from app.domain.analytics import (abc_classification, inventory_turnover, is_dead_stock, safe_csv_cell,
                                  stock_status, to_csv)
from app.domain.forecasting.demand import prepare_demand
from app.domain.forecasting.engine import backtest, run_forecast
from app.domain.forecasting.metrics import all_metrics, bias, mae, rmse, wape
from app.domain.forecasting.models import (Croston, HoltWintersAdditive, MovingAverage, Naive, SeasonalNaive,
                                           SimpleExpSmoothing)
from app.domain.reports import build_workbook
from app.domain.stockcount import adjustment_for, count_variance
from app.domain.replenishment import (ReplenishmentInput, apply_budget, compute_recommendation, round_to_pack)


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ---- metrics
def test_metrics_known_values():
    a, p = np.array([10, 0, 5]), np.array([8, 1, 6])
    assert close(mae(a, p), (2 + 1 + 1) / 3)
    assert close(rmse(a, p), math.sqrt((4 + 1 + 1) / 3))
    assert close(wape(a, p), 4 / 15)
    assert close(bias(a, p), (15 - 15) / 15)


def test_wape_and_bias_undefined_when_no_demand():
    z = np.zeros(5)
    assert wape(z, z + 1) is None and bias(z, z + 1) is None
    assert all_metrics(z, z)["mae"] == 0


# ---- models
def test_naive_and_seasonal_naive():
    y = np.arange(1, 22, dtype=float)
    assert list(Naive().forecast(y, 3).mean) == [21, 21, 21]
    sn = SeasonalNaive(7).forecast(y, 8).mean
    assert list(sn[:7]) == list(y[-7:]) and sn[7] == y[-7]


def test_moving_average_level():
    y = np.array([1.0] * 10 + [5.0] * 14)
    assert close(MovingAverage(14).forecast(y, 2).mean[0], 5.0)


def test_croston_intermittent_rate():
    y = np.zeros(60)
    y[::5] = 4.0  # demand of 4 every 5th day => ~0.8/day
    rate = Croston().forecast(y, 5).mean[0]
    assert 0.5 < rate < 0.95


def test_ses_constant_series():
    f = SimpleExpSmoothing().forecast(np.full(30, 7.0), 5)
    assert np.allclose(f.mean, 7.0) and np.allclose(f.lower, 7.0)


def test_holt_winters_recovers_weekly_pattern():
    pattern = np.array([5, 6, 7, 8, 12, 15, 10], float)
    y = np.tile(pattern, 12)
    f = HoltWintersAdditive(7).forecast(y, 7).mean
    assert np.allclose(f, pattern, atol=0.5)


def test_intervals_widen_and_stay_non_negative():
    rng = np.random.default_rng(1)
    y = rng.poisson(3, 60).astype(float)
    f = MovingAverage(14).forecast(y, 10)
    assert np.all(f.lower >= 0) and np.all(f.upper >= f.mean)
    assert (f.upper - f.mean)[-1] > (f.upper - f.mean)[0]


def test_min_history_enforced():
    try:
        HoltWintersAdditive(7).forecast(np.ones(10), 3)
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---- backtest / selection
def test_backtest_is_chronological_and_uses_only_past():
    """A model that records what it was trained on must never see data from the validation window."""
    seen = []

    class Spy(MovingAverage):
        def forecast(self, history, horizon):
            seen.append(len(history))
            return super().forecast(history, horizon)

    y = np.arange(100, dtype=float)
    backtest(Spy(7), y, horizon=7, folds=3)
    assert seen == [100 - 3 * 7, 100 - 2 * 7, 100 - 1 * 7]


def test_seasonal_series_selects_seasonal_model_over_baseline():
    rng = np.random.default_rng(7)
    pattern = np.array([4, 5, 6, 7, 14, 18, 9], float)
    y = np.tile(pattern, 20) + rng.normal(0, 0.8, 140)
    out = run_forecast(np.maximum(y, 0), 14)
    assert out.sufficiency == "sufficient"
    assert out.selected_model in ("holt_winters", "seasonal_naive")
    assert len(out.forecast.mean) == 14 and out.evaluation


def test_insufficient_history_falls_back_with_warning():
    out = run_forecast([1, 2, 3], 7)
    assert out.sufficiency == "insufficient" and out.selected_model == "mean_fallback" and out.warnings
    out2 = run_forecast(list(np.ones(15) * 2), 7)
    assert out2.sufficiency == "insufficient" and out2.selected_model == "moving_average"


def test_intermittent_series_flagged():
    y = np.zeros(120)
    y[::9] = 3
    out = run_forecast(y, 30)
    assert any("Intermittent" in w for w in out.warnings)
    assert np.all(out.forecast.mean >= 0)


# ---- demand prep
def test_prepare_demand_censors_stockout_zeros_but_not_real_zeros():
    start = date(2026, 1, 1)
    sales = {start + timedelta(days=i): 5.0 for i in range(0, 10, 2)}
    stockouts = {start + timedelta(days=3), start + timedelta(days=5)}   # both are zero-sale days => censored
    p = prepare_demand(sales, start, start + timedelta(days=9), stockouts)
    assert p.censored_days == 2
    assert np.isnan(p.observed[3]) and p.observed[1] == 0.0   # day1: true zero (in stock)
    assert not np.isnan(p.fit_series).any()
    assert "censored_stockout_days:2" in p.flags


def test_prepare_demand_missing_outside_coverage_and_returns_floor():
    start = date(2026, 1, 1)
    p = prepare_demand({start + timedelta(days=5): -3.0}, start, start + timedelta(days=9),
                       coverage=(start + timedelta(days=2), start + timedelta(days=9)))
    assert p.missing_days == 2 and np.isnan(p.observed[0])
    assert p.observed[5] == 0.0


# ---- replenishment
def base_input(**kw):
    d = dict(on_hand=20, reserved=0, on_order=0, daily_forecast=[10.0] * 30, demand_std_daily=0.0,
             lead_time_days=5, review_period_days=7, service_level=0.95)
    d.update(kw)
    return ReplenishmentInput(**d)


def test_rop_target_and_quantity_zero_variance():
    r = compute_recommendation(base_input())
    assert r.lead_time_demand == 50 and r.safety_stock == 0
    assert r.reorder_point == 50 and r.target_stock == 120
    assert r.inventory_position == 20 and r.recommended_quantity == 100


def test_safety_stock_uses_service_level_and_variability():
    r = compute_recommendation(base_input(demand_std_daily=4.0, service_level=0.95))
    assert close(r.safety_stock, 1.6448536 * 4 * math.sqrt(5), 1e-3)
    hi = compute_recommendation(base_input(demand_std_daily=4.0, service_level=0.99))
    assert hi.safety_stock > r.safety_stock


def test_no_order_when_position_above_rop():
    r = compute_recommendation(base_input(on_hand=60))
    assert r.recommended_quantity == 0 and r.urgency == "none"


def test_on_order_and_reserved_change_position():
    r = compute_recommendation(base_input(on_hand=20, on_order=100, reserved=10))
    assert r.inventory_position == 110
    assert r.recommended_quantity == 0          # IP 110 > ROP 50 -> no order
    r2 = compute_recommendation(base_input(on_hand=20, on_order=0, reserved=10))
    assert r2.inventory_position == 10 and r2.recommended_quantity == 110


def test_moq_and_pack_size():
    r = compute_recommendation(base_input(on_hand=100, daily_forecast=[10.0] * 30, moq=0, pack_size=12))
    assert r.recommended_quantity == 0
    r = compute_recommendation(base_input(on_hand=45, pack_size=12))   # raw 75 -> 84
    assert r.raw_quantity == 75 and r.recommended_quantity == 84
    r = compute_recommendation(base_input(on_hand=48, moq=200))        # raw 72 -> MOQ 200
    assert r.recommended_quantity == 200 and any("minimum" in w for w in r.warnings)
    assert round_to_pack(13, 12) == 24 and round_to_pack(0, 12) == 0


def test_shelf_life_and_capacity_limits():
    r = compute_recommendation(base_input(on_hand=0, shelf_life_days=10))
    assert r.recommended_quantity == 100 and any("Shelf life" in w for w in r.warnings)
    r = compute_recommendation(base_input(on_hand=0, max_capacity=60))
    assert r.recommended_quantity == 60 and any("capacity" in w for w in r.warnings)


def test_urgency_critical_when_stocked_out():
    r = compute_recommendation(base_input(on_hand=0))
    assert r.urgency == "critical" and "target stock" in r.reason


def test_budget_prioritises_urgent_and_never_overspends():
    crit = compute_recommendation(base_input(on_hand=0, unit_cost=10))     # qty 120 -> 1200
    low = compute_recommendation(base_input(on_hand=30, unit_cost=10))     # qty 90 -> 900
    res = apply_budget([("crit", crit, 1, 10), ("low", low, 1, 10)], 1500)
    assert res["crit"] == 120 and res["low"] == 30
    assert res["crit"] * 10 + res["low"] * 10 <= 1500


# ---- analytics
def test_abc_classification():
    abc = abc_classification({"a": 80, "b": 15, "c": 4, "d": 1, "z": 0})
    assert abc["a"] == "A" and abc["b"] == "B" and abc["c"] == "C" and abc["z"] == "C"


def test_turnover_and_status_and_dead_stock():
    assert inventory_turnover(600, 200) == 3.0 and inventory_turnover(1, 0) is None
    assert stock_status(0, 5, 1) == "out_of_stock" and stock_status(4, 5, 1) == "low_stock"
    assert stock_status(500, 5, 1) == "overstock" and stock_status(50, 5, 1) == "ok"
    today = date(2026, 11, 14)
    assert is_dead_stock(5, date(2026, 6, 1), today) and not is_dead_stock(5, date(2026, 11, 1), today)
    assert is_dead_stock(5, None, today) and not is_dead_stock(0, None, today)


def test_csv_formula_injection_is_neutralised():
    assert safe_csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)" and safe_csv_cell("ok") == "ok"
    assert "'=cmd" in to_csv([{"n": "=cmd"}], ["n"])


# ---- stock count
def test_count_variance_and_tolerance():
    r = count_variance(100, 97, unit_cost=10)
    assert r.variance == -3 and r.value_impact == -30 and r.needs_review is True      # 3% > 2% tolerance
    assert count_variance(100, 99, 10).needs_review is False                          # 1% within tolerance
    assert count_variance(0, 0).variance == 0 and count_variance(10, 12, tolerance_units=2).needs_review is False
    try:
        count_variance(5, -1)
        assert False
    except ValueError:
        pass


def test_adjustment_mapping():
    assert adjustment_for(0) is None
    assert adjustment_for(4) == ("correction_in", 4) and adjustment_for(-2.5) == ("correction_out", 2.5)


# ---- reports
def test_workbook_roundtrip_and_formula_guard():
    import io
    from openpyxl import load_workbook
    data = build_workbook({"Inventory": (["SKU", "Qty"], [["=1+1", 5], ["ok", None]]),
                           "A very long sheet title that exceeds thirty-one chars": (["x"], [])})
    wb = load_workbook(io.BytesIO(data))
    ws = wb["Inventory"]
    assert ws["A2"].value == "'=1+1" and ws["B2"].value == 5 and ws["A3"].value == "ok"
    assert all(len(n) <= 31 for n in wb.sheetnames) and ws["A1"].font.bold
