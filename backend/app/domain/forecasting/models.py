"""Pluggable demand forecasting models (pure numpy, no I/O).

Each model returns point predictions plus an approximate 80% prediction interval built
from in-sample one-step residuals widened by sqrt(h). Intervals are approximations,
not guarantees (see docs/FORECASTING.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

Z80 = 1.2816  # two-sided 80% normal quantile


@dataclass
class Forecast:
    mean: np.ndarray
    lower: np.ndarray
    upper: np.ndarray
    model_name: str
    params: dict = field(default_factory=dict)


def _interval(mean: np.ndarray, resid: np.ndarray):
    sigma = float(np.std(resid)) if len(resid) > 1 else 0.0
    h = np.arange(1, len(mean) + 1)
    width = Z80 * sigma * np.sqrt(h)
    return np.maximum(mean - width, 0.0), mean + width


class ForecastModel:
    name: ClassVar[str] = "base"
    version: ClassVar[str] = "1.0"
    min_history: int = 1

    def _fit(self, y: np.ndarray, horizon: int):
        """Return (mean forecast, in-sample one-step residuals, params)."""
        raise NotImplementedError

    def forecast(self, history, horizon: int) -> Forecast:
        y = np.asarray(history, dtype=float)
        if len(y) < self.min_history:
            raise ValueError(f"{self.name} needs >= {self.min_history} observations, got {len(y)}")
        mean, resid, params = self._fit(y, horizon)
        mean = np.maximum(np.asarray(mean, float), 0.0)
        lo, hi = _interval(mean, np.asarray(resid, float))
        return Forecast(mean, lo, hi, self.name, params)


class Naive(ForecastModel):
    name = "naive"
    min_history = 2

    def _fit(self, y, horizon):
        return np.repeat(y[-1], horizon), y[1:] - y[:-1], {}


class SeasonalNaive(ForecastModel):
    name = "seasonal_naive"

    def __init__(self, period: int = 7):
        self.period = period
        self.min_history = 2 * period

    def _fit(self, y, horizon):
        m = self.period
        last = y[-m:]
        return np.array([last[i % m] for i in range(horizon)]), y[m:] - y[:-m], {"period": m}


class MovingAverage(ForecastModel):
    name = "moving_average"
    min_history = 3

    def __init__(self, window: int = 14):
        self.window = window

    def _fit(self, y, horizon):
        w = min(self.window, len(y))
        level = float(np.mean(y[-w:]))
        trailing = np.convolve(y, np.ones(w) / w, mode="valid")  # mean of y[i..i+w-1]
        resid = y[w:] - trailing[:-1]                            # predict y[i+w] from prior w
        return np.repeat(level, horizon), resid, {"window": w}


class SimpleExpSmoothing(ForecastModel):
    name = "ses"
    min_history = 8

    def _fit(self, y, horizon):
        best = None
        for alpha in np.linspace(0.05, 0.95, 19):
            level, errs = y[0], []
            for t in range(1, len(y)):
                errs.append(y[t] - level)
                level = alpha * y[t] + (1 - alpha) * level
            sse = float(np.sum(np.square(errs)))
            if best is None or sse < best[0]:
                best = (sse, alpha, level, np.array(errs))
        _, alpha, level, errs = best
        return np.repeat(level, horizon), errs, {"alpha": float(alpha)}


class HoltWintersAdditive(ForecastModel):
    """Additive level + optional trend + additive seasonality; small grid search on SSE."""
    name = "holt_winters"

    def __init__(self, period: int = 7):
        self.period = period
        self.min_history = 3 * period

    def _run(self, y, a, b, g):
        m = self.period
        level = float(np.mean(y[:m]))
        trend = (float(np.mean(y[m:2 * m])) - level) / m if b > 0 else 0.0
        seas = [float(y[i] - level) for i in range(m)]
        errs = []
        for t in range(m, len(y)):
            s = seas[t % m]
            errs.append(y[t] - (level + trend + s))
            new_level = a * (y[t] - s) + (1 - a) * (level + trend)
            trend = b * (new_level - level) + (1 - b) * trend if b > 0 else 0.0
            seas[t % m] = g * (y[t] - new_level) + (1 - g) * s
            level = new_level
        return level, trend, seas, np.array(errs)

    def _fit(self, y, horizon):
        m, n = self.period, len(y)
        best = None
        for a in (0.1, 0.3, 0.5):
            for b in (0.0, 0.05):
                for g in (0.1, 0.3):
                    level, trend, seas, errs = self._run(y, a, b, g)
                    sse = float(np.sum(errs ** 2))
                    if best is None or sse < best[0]:
                        best = (sse, (a, b, g), level, trend, seas, errs)
        _, (a, b, g), level, trend, seas, errs = best
        mean = np.array([level + (h + 1) * trend + seas[(n + h) % m] for h in range(horizon)])
        return mean, errs, {"alpha": a, "beta": b, "gamma": g, "period": m}


class Croston(ForecastModel):
    """Croston's method with Syntetos-Boylan bias correction for intermittent demand."""
    name = "croston_sba"
    min_history = 8

    def __init__(self, alpha: float = 0.1):
        self.alpha = alpha

    def _fit(self, y, horizon):
        a = self.alpha
        nz = np.flatnonzero(y > 0)
        if len(nz) == 0:
            return np.zeros(horizon), np.zeros(max(len(y) - 1, 1)), {"alpha": a}
        # Initialise from the whole window (mean demand size and mean inter-demand interval);
        # initialising from the first demand alone converges very slowly for small alpha.
        z, p, q = float(np.mean(y[nz])), len(y) / len(nz), 1
        fitted = {}
        for t in range(nz[0] + 1, len(y)):
            fitted[t] = (1 - a / 2) * z / p
            if y[t] > 0:
                z = a * y[t] + (1 - a) * z
                p = a * q + (1 - a) * p
                q = 1
            else:
                q += 1
        rate = (1 - a / 2) * z / p
        resid = np.array([y[t] - f for t, f in fitted.items()])
        return np.repeat(rate, horizon), resid, {"alpha": a, "avg_interval": p, "avg_size": z}


def default_candidates(period: int = 7) -> list[ForecastModel]:
    return [MovingAverage(14), SimpleExpSmoothing(), Croston(), HoltWintersAdditive(period)]


def default_baseline(n: int, period: int = 7) -> ForecastModel:
    return SeasonalNaive(period) if n >= 4 * period else Naive()
