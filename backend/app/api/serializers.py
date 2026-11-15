"""Explicit serialisers: money and quantities are emitted as strings to avoid float rounding."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def _v(x):
    if isinstance(x, Decimal):
        return format(x, "f")
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if hasattr(x, "hex") and hasattr(x, "int"):  # UUID
        return str(x)
    return x


def ser(obj, fields: list[str]) -> dict:
    return {f: _v(getattr(obj, f)) for f in fields}


def product_out(p) -> dict:
    d = ser(p, ["id", "sku", "name", "description", "unit", "cost_price", "selling_price", "tracking_mode",
                "shelf_life_days", "status", "attributes"])
    d["variants"] = [ser(v, ["id", "sku", "barcode", "attributes", "status"]) | {"cost": _v(v.cost), "price": _v(v.price)}
                     for v in p.variants]
    return d


def sale_out(s) -> dict:
    d = ser(s, ["id", "sale_number", "branch_id", "warehouse_id", "sale_date", "status", "subtotal", "discount_total",
                "tax_total", "grand_total", "source"])
    d["items"] = [ser(i, ["id", "variant_id", "quantity", "unit_price", "discount", "tax", "line_total",
                          "cost_snapshot"]) for i in s.items]
    return d


def po_out(p) -> dict:
    d = ser(p, ["id", "order_number", "branch_id", "warehouse_id", "supplier_id", "status", "order_date",
                "expected_date", "total"])
    d["items"] = [ser(i, ["id", "variant_id", "ordered_quantity", "received_quantity", "rejected_quantity",
                          "unit_cost", "line_total"]) for i in p.items]
    return d


def movement_out(m) -> dict:
    return ser(m, ["id", "warehouse_id", "variant_id", "movement_type", "bucket", "quantity_delta", "balance_after",
                   "reference_type", "reference_id", "reason", "actor_user_id", "occurred_at"])


def rec_out(r) -> dict:
    return ser(r, ["id", "branch_id", "warehouse_id", "variant_id", "supplier_id", "recommended_quantity",
                   "estimated_cost", "urgency", "status", "reason", "details", "expires_at", "purchase_order_id"])
