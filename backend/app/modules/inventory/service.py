"""Inventory engine: the only code path that changes stock balances.

Every change = one immutable StockMovement + balance update in the caller's transaction.
Balance rows are locked with SELECT ... FOR UPDATE (no-op on SQLite) to prevent lost updates,
and movements carry an optional idempotency key so retries cannot apply twice.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import InsufficientStock, InvalidState
from app.modules.common import Ctx, D
from app.modules.models import InventoryBalance, StockMovement, StockTransfer, Variant, utcnow

SELLABLE, UNAVAILABLE = "sellable", "unavailable"
ADJUST_REASONS = {"found", "lost", "damaged", "expired", "correction_in", "correction_out"}


def _locked_balance(ctx: Ctx, warehouse_id, variant_id) -> InventoryBalance:
    q = select(InventoryBalance).where(
        InventoryBalance.organization_id == ctx.org_id, InventoryBalance.warehouse_id == warehouse_id,
        InventoryBalance.variant_id == variant_id).with_for_update()
    bal = ctx.db.scalar(q)
    if bal:
        return bal
    try:
        with ctx.db.begin_nested():  # savepoint: a concurrent insert must not abort the outer transaction
            bal = InventoryBalance(organization_id=ctx.org_id, warehouse_id=warehouse_id, variant_id=variant_id)
            ctx.db.add(bal)
            ctx.db.flush()
    except IntegrityError:
        pass
    return ctx.db.scalar(q)


def apply_movement(ctx: Ctx, *, warehouse_id, variant_id, movement_type: str, delta, bucket: str = SELLABLE,
                   reference_type: str | None = None, reference_id=None, reason: str | None = None,
                   idempotency_key: str | None = None, occurred_at: datetime | None = None) -> StockMovement:
    """Apply one signed quantity change. Raises InsufficientStock rather than going negative."""
    delta = D(delta)
    if delta == 0:
        raise InvalidState("Quantity change must be non-zero.")
    if idempotency_key:
        existing = ctx.db.scalar(select(StockMovement).where(
            StockMovement.organization_id == ctx.org_id, StockMovement.idempotency_key == idempotency_key))
        if existing:
            return existing  # retry: already applied exactly once
    bal = _locked_balance(ctx, warehouse_id, variant_id)
    if bucket == SELLABLE:
        new = bal.on_hand_quantity + delta
        if new < 0:
            raise InsufficientStock(f"Insufficient sellable stock: have {bal.on_hand_quantity:g}, need {-delta:g}.",
                                    details={"variant_id": str(variant_id), "available": str(bal.on_hand_quantity)})
        if new < bal.reserved_quantity:
            raise InsufficientStock("Change would leave less stock than is reserved.")
        bal.on_hand_quantity = new
    elif bucket == UNAVAILABLE:
        new = bal.unavailable_quantity + delta
        if new < 0:
            raise InsufficientStock("Insufficient unavailable stock to remove.")
        bal.unavailable_quantity = new
    else:
        raise InvalidState(f"Unknown bucket {bucket}")
    mv = StockMovement(organization_id=ctx.org_id, warehouse_id=warehouse_id, variant_id=variant_id,
                       movement_type=movement_type, bucket=bucket, quantity_delta=delta, balance_after=new,
                       reference_type=reference_type, reference_id=str(reference_id) if reference_id else None,
                       reason=reason, actor_user_id=ctx.user_id, occurred_at=occurred_at or utcnow(),
                       idempotency_key=idempotency_key)
    ctx.db.add(mv)
    ctx.db.flush()
    return mv


def opening_balance(ctx: Ctx, warehouse_id, variant_id, quantity, idempotency_key: str | None = None):
    ctx.require("inventory.adjust")
    ctx.check_warehouse(warehouse_id)
    ctx.get_variant(variant_id)
    if D(quantity) <= 0:
        raise InvalidState("Opening quantity must be positive.")
    mv = apply_movement(ctx, warehouse_id=warehouse_id, variant_id=variant_id, movement_type="opening",
                        delta=quantity, reference_type="opening", reason="Opening balance",
                        idempotency_key=idempotency_key)
    ctx.audit("inventory.opening", "stock_movement", mv.id, quantity=str(quantity))
    return mv


def adjust_stock(ctx: Ctx, warehouse_id, variant_id, quantity, reason: str, note: str | None = None,
                 idempotency_key: str | None = None) -> list[StockMovement]:
    """Audited adjustment. 'damaged' moves stock from sellable to the unavailable bucket;
    'lost'/'expired' remove it; 'found' adds sellable stock."""
    ctx.require("inventory.adjust")
    if reason not in ADJUST_REASONS:
        raise InvalidState(f"Reason must be one of {sorted(ADJUST_REASONS)}.")
    ctx.check_warehouse(warehouse_id)
    ctx.get_variant(variant_id)
    q = D(quantity)
    if q <= 0:
        raise InvalidState("Quantity must be positive; the reason determines direction.")
    key = (lambda s: f"{idempotency_key}:{s}" if idempotency_key else None)
    text = f"{reason}: {note}" if note else reason
    out: list[StockMovement] = []
    if reason in ("found", "correction_in"):
        out.append(apply_movement(ctx, warehouse_id=warehouse_id, variant_id=variant_id, movement_type="adjustment_in",
                                  delta=q, reference_type="adjustment", reason=text, idempotency_key=key("a")))
    elif reason in ("lost", "expired", "correction_out"):
        out.append(apply_movement(ctx, warehouse_id=warehouse_id, variant_id=variant_id, movement_type="adjustment_out",
                                  delta=-q, reference_type="adjustment", reason=text, idempotency_key=key("a")))
    else:  # damaged
        out.append(apply_movement(ctx, warehouse_id=warehouse_id, variant_id=variant_id, movement_type="adjustment_out",
                                  delta=-q, reference_type="adjustment", reason=text, idempotency_key=key("a")))
        out.append(apply_movement(ctx, warehouse_id=warehouse_id, variant_id=variant_id, movement_type="quarantine_in",
                                  delta=q, bucket=UNAVAILABLE, reference_type="adjustment", reason=text,
                                  idempotency_key=key("b")))
    ctx.audit("inventory.adjust", "stock_movement", out[0].id, reason=reason, quantity=str(q))
    return out


def transfer_stock(ctx: Ctx, source_wh, dest_wh, items: list[dict], idempotency_key: str | None = None) -> StockTransfer:
    ctx.require("inventory.transfer")
    if source_wh == dest_wh:
        raise InvalidState("Source and destination must differ.")
    ctx.check_warehouse(source_wh)
    ctx.check_warehouse(dest_wh)
    t = StockTransfer(organization_id=ctx.org_id, source_warehouse_id=source_wh, destination_warehouse_id=dest_wh,
                      created_by=ctx.user_id, items=[{"variant_id": str(i["variant_id"]), "quantity": str(i["quantity"])}
                                                     for i in items])
    ctx.db.add(t)
    ctx.db.flush()
    for n, i in enumerate(items):
        ctx.get_variant(i["variant_id"])
        q = D(i["quantity"])
        if q <= 0:
            raise InvalidState("Transfer quantities must be positive.")
        k = lambda s: f"{idempotency_key}:{n}:{s}" if idempotency_key else None
        apply_movement(ctx, warehouse_id=source_wh, variant_id=i["variant_id"], movement_type="transfer_out",
                       delta=-q, reference_type="transfer", reference_id=t.id, idempotency_key=k("out"))
        apply_movement(ctx, warehouse_id=dest_wh, variant_id=i["variant_id"], movement_type="transfer_in",
                       delta=q, reference_type="transfer", reference_id=t.id, idempotency_key=k("in"))
    ctx.audit("inventory.transfer", "stock_transfer", t.id)
    return t


def reconcile(ctx: Ctx) -> list[dict]:
    """Compare each balance with the sum of its ledger. Returns mismatches (empty = consistent)."""
    problems = []
    sums = {(r.warehouse_id, r.variant_id, r.bucket): r.s for r in ctx.db.execute(
        select(StockMovement.warehouse_id, StockMovement.variant_id, StockMovement.bucket,
               func.sum(StockMovement.quantity_delta).label("s"))
        .where(StockMovement.organization_id == ctx.org_id)
        .group_by(StockMovement.warehouse_id, StockMovement.variant_id, StockMovement.bucket))}
    for b in ctx.db.scalars(select(InventoryBalance).where(InventoryBalance.organization_id == ctx.org_id)):
        for bucket, val in ((SELLABLE, b.on_hand_quantity), (UNAVAILABLE, b.unavailable_quantity)):
            led = D(sums.pop((b.warehouse_id, b.variant_id, bucket), 0))
            if led != val:
                problems.append({"warehouse_id": str(b.warehouse_id), "variant_id": str(b.variant_id),
                                 "bucket": bucket, "balance": str(val), "ledger": str(led)})
    for (w, v, bucket), s in sums.items():  # ledger rows without a balance row
        if D(s) != 0:
            problems.append({"warehouse_id": str(w), "variant_id": str(v), "bucket": bucket, "balance": "0",
                             "ledger": str(s)})
    return problems


def sellable_by_variant(ctx: Ctx, branch_id=None) -> dict[uuid.UUID, Decimal]:
    from app.modules.models import Warehouse
    q = select(InventoryBalance.variant_id, func.sum(InventoryBalance.on_hand_quantity)).where(
        InventoryBalance.organization_id == ctx.org_id).group_by(InventoryBalance.variant_id)
    if branch_id:
        q = q.join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id).where(Warehouse.branch_id == branch_id)
    return {v: D(s) for v, s in ctx.db.execute(q)}
