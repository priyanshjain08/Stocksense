"""Pure stock-count variance logic.

Variance = counted - expected_snapshot (snapshot frozen when the count starts). Applying THAT delta on
approval, instead of overwriting the balance with the counted figure, preserves any sales/receipts that
happened while counting was in progress.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LineVariance:
    expected: float
    counted: float
    variance: float
    value_impact: float          # variance * unit cost (negative = shrinkage)
    needs_review: bool           # outside tolerance


def count_variance(expected: float, counted: float, unit_cost: float = 0.0,
                   tolerance_pct: float = 0.02, tolerance_units: float = 0.0) -> LineVariance:
    if counted < 0:
        raise ValueError("Counted quantity cannot be negative.")
    var = counted - expected
    limit = max(abs(expected) * tolerance_pct, tolerance_units)
    return LineVariance(expected, counted, var, var * unit_cost, abs(var) > limit)


def adjustment_for(variance: float) -> tuple[str, float] | None:
    """Map a variance to (adjustment reason, positive quantity); None when there is nothing to adjust."""
    if variance == 0:
        return None
    return ("correction_in", variance) if variance > 0 else ("correction_out", -variance)
