"""Backtesting, model selection and forecast generation."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .metrics import all_metrics
from .models import Forecast, ForecastModel, MovingAverage, default_baseline, default_candidates

MIN_BACKTEST_HISTORY = 28   # days of data needed before we trust a backtest
MIN_IMPROVEMENT = 0.05      # candidate must beat baseline error by >=5% to be selected


@dataclass
class BacktestResult:
    model_name: str
    metrics: dict
    folds: int


@dataclass
class ForecastOutcome:
    forecast: Forecast
    selected_model: str
    baseline_model: str | None
    evaluation: list[BacktestResult]
    sufficiency: str                       # "sufficient" | "limited" | "insufficient"
    warnings: list[str] = field(default_factory=list)
    training_points: int = 0


def backtest(model: ForecastModel, y: np.ndarray, horizon: int, folds: int = 3) -> BacktestResult | None:
    """Rolling-origin evaluation. Chronological only; each fold trains on data before its cutoff."""
    n = len(y)
    h = max(1, min(horizon, n // 4))
    actuals, preds, done = [], [], 0
    for k in range(folds, 0, -1):
        cut = n - k * h
        if cut < model.min_history:
            continue
        try:
            f = model.forecast(y[:cut], h)
        except ValueError:
            continue
        actuals.append(y[cut:cut + h])
        preds.append(f.mean)
        done += 1
    if not done:
        return None
    return BacktestResult(model.name, all_metrics(np.concatenate(actuals), np.concatenate(preds)), done)


def _score(r: BacktestResult) -> float:
    return r.metrics["wape"] if r.metrics["wape"] is not None else r.metrics["mae"]


def run_forecast(history, horizon: int, period: int = 7,
                 candidates: list[ForecastModel] | None = None) -> ForecastOutcome:
    y = np.asarray(history, float)
    n = len(y)
    warnings: list[str] = []
    if n < 7 or float(np.sum(y)) == 0 and n < MIN_BACKTEST_HISTORY:
        warnings.append("Fewer than 7 days of history or no demand: forecast is a simple average and low-confidence.")
        mean = float(np.mean(y)) if n else 0.0
        f = Forecast(np.repeat(mean, horizon), np.zeros(horizon), np.repeat(mean * 2, horizon),
                     "mean_fallback", {"n": n})
        return ForecastOutcome(f, "mean_fallback", None, [], "insufficient", warnings, n)
    if n < MIN_BACKTEST_HISTORY:
        warnings.append(f"Only {n} days of history (<{MIN_BACKTEST_HISTORY}); no backtest possible, using moving average.")
        f = MovingAverage(14).forecast(y, horizon)
        return ForecastOutcome(f, f.model_name, None, [], "insufficient", warnings, n)

    baseline = default_baseline(n, period)
    base_res = backtest(baseline, y, horizon)
    results = []
    for m in (candidates or default_candidates(period)):
        r = backtest(m, y, horizon)
        if r:
            results.append((m, r))
    evaluation = ([base_res] if base_res else []) + [r for _, r in results]
    if not results or not base_res:
        warnings.append("Backtest could not be completed; falling back to moving average.")
        f = MovingAverage(14).forecast(y, horizon)
        return ForecastOutcome(f, f.model_name, baseline.name, evaluation, "limited", warnings, n)

    best_m, best_r = min(results, key=lambda mr: _score(mr[1]))
    if _score(best_r) <= _score(base_res) * (1 - MIN_IMPROVEMENT):
        chosen = best_m
    else:
        chosen = baseline
        warnings.append("No candidate beat the baseline by the required margin; baseline selected.")
    f = chosen.forecast(y, horizon)
    sufficiency = "sufficient" if n >= 90 else "limited"
    if sufficiency == "limited":
        warnings.append("Under 90 days of history: seasonality and accuracy estimates are uncertain.")
    if float(np.mean(y == 0)) > 0.6:
        warnings.append("Intermittent demand: point forecasts are averages; real demand arrives in lumps.")
    return ForecastOutcome(f, chosen.name, baseline.name, evaluation, sufficiency, warnings, n)
