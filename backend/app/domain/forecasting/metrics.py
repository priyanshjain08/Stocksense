"""Forecast accuracy metrics. MAPE is deliberately not used (undefined at zero demand)."""
from __future__ import annotations

import numpy as np


def mae(actual, pred) -> float:
    return float(np.mean(np.abs(np.asarray(actual, float) - np.asarray(pred, float))))


def rmse(actual, pred) -> float:
    d = np.asarray(actual, float) - np.asarray(pred, float)
    return float(np.sqrt(np.mean(d * d)))


def wape(actual, pred) -> float | None:
    """sum|e| / sum(actual). None when total actual demand is zero (undefined)."""
    a = np.asarray(actual, float)
    total = float(np.sum(np.abs(a)))
    if total == 0:
        return None
    return float(np.sum(np.abs(a - np.asarray(pred, float))) / total)


def bias(actual, pred) -> float | None:
    """sum(pred - actual) / sum(actual). Positive = over-forecast. None if no demand."""
    a = np.asarray(actual, float)
    total = float(np.sum(a))
    if total == 0:
        return None
    return float((np.sum(np.asarray(pred, float)) - total) / total)


def all_metrics(actual, pred) -> dict:
    return {"mae": mae(actual, pred), "rmse": rmse(actual, pred),
            "wape": wape(actual, pred), "bias": bias(actual, pred)}
