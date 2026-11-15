"""Pure inventory-health metric definitions (see docs/DATABASE.md for the written definitions)."""
from __future__ import annotations

import csv
import io
from datetime import date


def abc_classification(values: dict[str, float], a_cut: float = 0.8, b_cut: float = 0.95) -> dict[str, str]:
    """ABC by cumulative share of value (e.g. 90-day revenue). Zero-value items are 'C'."""
    total = sum(v for v in values.values() if v > 0)
    out: dict[str, str] = {}
    cum = 0.0
    for k, v in sorted(values.items(), key=lambda kv: -kv[1]):
        if v <= 0 or total == 0:
            out[k] = "C"
            continue
        before = cum / total
        cum += v
        out[k] = "A" if before < a_cut else ("B" if before < b_cut else "C")
    return out


def inventory_turnover(cogs: float, avg_inventory_value: float) -> float | None:
    """COGS over period / average inventory value (at cost). None if no inventory value."""
    return None if avg_inventory_value <= 0 else cogs / avg_inventory_value


def days_of_inventory(on_hand: float, avg_daily_demand: float) -> float | None:
    return None if avg_daily_demand <= 0 else on_hand / avg_daily_demand


def stock_status(on_hand_sellable: float, reorder_point: float | None, avg_daily: float,
                 overstock_days: float = 180) -> str:
    if on_hand_sellable <= 0:
        return "out_of_stock"
    if reorder_point is not None and on_hand_sellable <= reorder_point:
        return "low_stock"
    if avg_daily > 0 and on_hand_sellable / avg_daily > overstock_days:
        return "overstock"
    return "ok"


def is_dead_stock(on_hand: float, last_sale: date | None, today: date, inactive_days: int = 90) -> bool:
    """Estimate only: stock on hand with no sale in ``inactive_days`` (or never sold)."""
    if on_hand <= 0:
        return False
    return last_sale is None or (today - last_sale).days >= inactive_days


def is_slow_moving(on_hand: float, units_sold_period: float, period_days: int, min_units_per_30d: float = 1.0) -> bool:
    if on_hand <= 0:
        return False
    return (units_sold_period / max(period_days, 1)) * 30 < min_units_per_30d


def safe_csv_cell(v) -> str:
    """Neutralise CSV/Excel formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def to_csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([safe_csv_cell(r.get(c)) for c in columns])
    return buf.getvalue()
