"""Stock counts: snapshot -> count -> submit -> (separate) approve -> audited adjustments."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from app.core.exceptions import Forbidden, InvalidState, NotFound
from app.domain.stockcount import adjustment_for, count_variance
from app.modules.common import Ctx, D
from app.modules.inventory.service import apply_movement
from app.modules.models import InventoryBalance, StockCount, StockCountLine, Variant


def start_count(ctx: Ctx, warehouse_id, variant_ids: list | None = None, note: str | None = None) -> StockCount:
    ctx.require("inventory.adjust")
    ctx.check_warehouse(warehouse_id)
    q = select(InventoryBalance).where(InventoryBalance.organization_id == ctx.org_id,
                                       InventoryBalance.warehouse_id == warehouse_id)
    if variant_ids:
        q = q.where(InventoryBalance.variant_id.in_(variant_ids))
    balances = list(ctx.db.scalars(q))
    if not balances:
        raise InvalidState("No stock records to count in this warehouse.")
    c = StockCount(organization_id=ctx.org_id, warehouse_id=warehouse_id, created_by=ctx.user_id, note=note)
    for b in balances:
        c.lines.append(StockCountLine(variant_id=b.variant_id, expected_quantity=b.on_hand_quantity))
    ctx.db.add(c)
    ctx.db.flush()
    ctx.audit("count.start", "stock_count", c.id, lines=len(c.lines))
    return c


def get_count(ctx: Ctx, count_id) -> StockCount:
    ctx.require("inventory.read")
    c = ctx.db.scalar(select(StockCount).where(StockCount.id == count_id, StockCount.organization_id == ctx.org_id))
    if not c:
        raise NotFound("Stock count not found.")
    ctx.check_warehouse(c.warehouse_id)
    return c


def record_counts(ctx: Ctx, count_id, counted: dict) -> StockCount:
    """counted: {variant_id: quantity}. Can be called repeatedly while the count is open."""
    ctx.require("inventory.adjust")
    c = get_count(ctx, count_id)
    if c.status != "open":
        raise InvalidState(f"Count is {c.status}; counted quantities can no longer be changed.")
    by_var = {ln.variant_id: ln for ln in c.lines}
    for vid, qty in counted.items():
        ln = by_var.get(vid)
        if not ln:
            raise NotFound(f"Variant {vid} is not part of this count.")
        if D(qty) < 0:
            raise InvalidState("Counted quantity cannot be negative.")
        ln.counted_quantity = D(qty)
    return c


def submit_count(ctx: Ctx, count_id) -> StockCount:
    ctx.require("inventory.adjust")
    c = get_count(ctx, count_id)
    if c.status != "open":
        raise InvalidState(f"Count is already {c.status}.")
    missing = [str(ln.variant_id) for ln in c.lines if ln.counted_quantity is None]
    if missing:
        raise InvalidState(f"{len(missing)} line(s) have not been counted.", details={"uncounted": missing[:50]})
    c.status = "submitted"
    ctx.audit("count.submit", "stock_count", c.id)
    return c


def review(ctx: Ctx, count_id) -> list[dict]:
    c = get_count(ctx, count_id)
    costs = {v.id: float(v.cost) for v in ctx.db.scalars(select(Variant).where(
        Variant.id.in_([ln.variant_id for ln in c.lines])))}
    out = []
    for ln in c.lines:
        if ln.counted_quantity is None:
            continue
        r = count_variance(float(ln.expected_quantity), float(ln.counted_quantity), costs.get(ln.variant_id, 0.0))
        out.append({"variant_id": str(ln.variant_id), "expected": r.expected, "counted": r.counted,
                    "variance": r.variance, "value_impact": round(r.value_impact, 2), "needs_review": r.needs_review})
    return out


def approve_count(ctx: Ctx, count_id) -> StockCount:
    """Approver must hold inventory.count_approve and must not be the person who started the count."""
    ctx.require("inventory.count_approve")
    c = get_count(ctx, count_id)
    if c.status != "submitted":
        raise InvalidState("Only submitted counts can be approved.")
    if c.created_by == ctx.user_id and ctx.role != "owner":
        raise Forbidden("A count must be approved by someone other than the person who started it.")
    for n, ln in enumerate(c.lines):
        adj = adjustment_for(float(ln.counted_quantity - ln.expected_quantity))
        if adj is None:
            continue
        reason, qty = adj
        apply_movement(ctx, warehouse_id=c.warehouse_id, variant_id=ln.variant_id,
                       movement_type="adjustment_in" if reason == "correction_in" else "adjustment_out",
                       delta=D(qty) if reason == "correction_in" else -D(qty), reference_type="stock_count",
                       reference_id=c.id, reason=f"Stock count {str(c.id)[:8]}: {reason}",
                       idempotency_key=f"count:{c.id}:{n}")
    c.status, c.approved_by = "approved", ctx.user_id
    ctx.audit("count.approve", "stock_count", c.id)
    return c
