"""Demand series preparation.

Rules (documented in docs/FORECASTING.md):
* net demand = sold - returned (restocked or not, returns reduce observed demand that day)
  and cancelled/draft sales are excluded upstream.
* Days outside ``coverage`` (no data feed) are *missing* (NaN), not zero.
* Days with zero sales while the item had no sellable stock are *censored* (NaN) and
  are not treated as true zero demand; they are imputed for fitting only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np


@dataclass
class PreparedDemand:
    start: date
    observed: np.ndarray            # raw net demand, NaN where missing/censored
    fit_series: np.ndarray          # NaN-free series used for model fitting
    censored_days: int = 0
    missing_days: int = 0
    flags: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.fit_series)


def prepare_demand(daily_net: dict[date, float], start: date, end: date,
                   stockout_days: set[date] | None = None,
                   coverage: tuple[date, date] | None = None,
                   impute_window: int = 28) -> PreparedDemand:
    if end < start:
        raise ValueError("end before start")
    stockout_days = stockout_days or set()
    n = (end - start).days + 1
    obs = np.zeros(n)
    missing = censored = 0
    for i in range(n):
        d = start + timedelta(days=i)
        if coverage and not (coverage[0] <= d <= coverage[1]):
            obs[i] = np.nan
            missing += 1
            continue
        q = float(daily_net.get(d, 0.0))
        q = max(q, 0.0)  # net negative (returns > sales that day) is floored at 0 demand
        if q == 0.0 and d in stockout_days:
            obs[i] = np.nan
            censored += 1
            continue
        obs[i] = q
    fit = obs.copy()
    for i in np.flatnonzero(np.isnan(fit)):  # impute with mean of recent uncensored days
        window = obs[max(0, i - impute_window):i]
        window = window[~np.isnan(window)]
        if len(window) == 0:
            window = obs[~np.isnan(obs)]
        fit[i] = float(np.mean(window)) if len(window) else 0.0
    flags = []
    if censored:
        flags.append(f"censored_stockout_days:{censored}")
    if missing:
        flags.append(f"missing_days:{missing}")
    valid = obs[~np.isnan(obs)]
    if len(valid) and np.mean(valid == 0) > 0.6:
        flags.append("intermittent_demand")
    return PreparedDemand(start, obs, fit, censored, missing, flags)
