"""Suppliers, purchase orders (state machine) and goods receiving."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.exceptions import InvalidState, NotFound
from app.modules.common import Ctx, D, money, next_number
from app.modules.inventory.service import apply_movement
from app.modules.models import (GoodsReceipt, GoodsReceiptItem, PurchaseOrder, PurchaseOrderItem, Supplier,
                                SupplierProduct, utcnow)

TRANSITIONS = {
    "draft": {"pending_approval", "cancelled"},
    "pending_approval": {"approved", "draft", "cancelled"},
    "approved": {"partially_received", "received", "cancelled"},
    "partially_received": {"received", "cancelled"},
    "received": set(),
    "cancelled": set(),
}
OPEN_STATES = ("approved", "partially_received")


def _transition(po: PurchaseOrder, new: str) -> None:
    if new not in TRANSITIONS[po.status]:
        raise InvalidState(f"Cannot move purchase order from '{po.status}' to '{new}'.")
    po.status = new


def create_supplier(ctx: Ctx, **data) -> Supplier:
    ctx.require("suppliers.write")
    s = Supplier(organization_id=ctx.org_id, **data)
    ctx.db.add(s)
    ctx.db.flush()
    ctx.audit("supplier.create", "supplier", s.id)
    return s


def get_supplier(ctx: Ctx, supplier_id) -> Supplier:
    s = ctx.db.scalar(select(Supplier).where(Supplier.id == supplier_id, Supplier.organization_id == ctx.org_id))
    if not s:
        raise NotFound("Supplier not found.")
    return s


def set_supplier_product(ctx: Ctx, supplier_id, variant_id, unit_cost, lead_time_days=7, moq=0, pack_size=1,
                         supplier_sku=None, is_preferred=True) -> SupplierProduct:
    ctx.require("suppliers.write")
    get_supplier(ctx, supplier_id)
    ctx.get_variant(variant_id)
    if D(pack_size) <= 0 or D(moq) < 0 or lead_time_days < 0:
        raise InvalidState("Invalid pack size, MOQ or lead time.")
    sp = ctx.db.scalar(select(SupplierProduct).where(SupplierProduct.organization_id == ctx.org_id,
                                                     SupplierProduct.supplier_id == supplier_id,
                                                     SupplierProduct.variant_id == variant_id))
    if not sp:
        sp = SupplierProduct(organization_id=ctx.org_id, supplier_id=supplier_id, variant_id=variant_id,
                             unit_cost=money(unit_cost))
        ctx.db.add(sp)
    sp.unit_cost, sp.lead_time_days, sp.minimum_order_quantity = money(unit_cost), lead_time_days, D(moq)
    sp.pack_size, sp.supplier_sku, sp.is_preferred = D(pack_size), supplier_sku, is_preferred
    ctx.db.flush()
    return sp


def create_po(ctx: Ctx, *, branch_id, warehouse_id, supplier_id, items: list[dict], expected_date: date | None = None,
              order_date: date | None = None) -> PurchaseOrder:
    ctx.require("purchasing.create")
    branch = ctx.check_branch(branch_id)
    wh = ctx.check_warehouse(warehouse_id)
    if wh.branch_id != branch.id:
        raise InvalidState("Warehouse does not belong to the branch.")
    get_supplier(ctx, supplier_id)
    if not items:
        raise InvalidState("A purchase order needs at least one item.")
    po = PurchaseOrder(organization_id=ctx.org_id, branch_id=branch.id, warehouse_id=wh.id, supplier_id=supplier_id,
                       order_number=next_number(ctx, "po", "PO"), order_date=order_date or date.today(),
                       expected_date=expected_date, created_by=ctx.user_id)
    total = Decimal(0)
    seen = set()
    for it in items:
        v = ctx.get_variant(it["variant_id"])
        if v.id in seen:
            raise InvalidState("Duplicate variant on purchase order; combine into one line.")
        seen.add(v.id)
        q = D(it["ordered_quantity"])
        if q <= 0:
            raise InvalidState("Ordered quantities must be positive.")
        cost = D(it["unit_cost"]) if it.get("unit_cost") is not None else v.cost
        po.items.append(PurchaseOrderItem(organization_id=ctx.org_id, variant_id=v.id, ordered_quantity=q,
                                          unit_cost=money(cost), line_total=money(cost * q)))
        total += money(cost * q)
    po.total = money(total)
    ctx.db.add(po)
    ctx.db.flush()
    ctx.audit("po.create", "purchase_order", po.id, total=str(po.total))
    return po


def get_po(ctx: Ctx, po_id) -> PurchaseOrder:
    ctx.require("purchasing.read")
    po = ctx.db.scalar(select(PurchaseOrder).where(PurchaseOrder.id == po_id,
                                                   PurchaseOrder.organization_id == ctx.org_id))
    if not po:
        raise NotFound("Purchase order not found.")
    ctx.check_branch(po.branch_id)
    return po


def submit_po(ctx: Ctx, po_id) -> PurchaseOrder:
    ctx.require("purchasing.create")
    po = get_po(ctx, po_id)
    _transition(po, "pending_approval")
    ctx.audit("po.submit", "purchase_order", po.id)
    return po


def approve_po(ctx: Ctx, po_id) -> PurchaseOrder:
    ctx.require("purchasing.approve")
    po = get_po(ctx, po_id)
    _transition(po, "approved")
    po.approved_by, po.approved_at = ctx.user_id, utcnow()
    ctx.audit("po.approve", "purchase_order", po.id)
    return po


def cancel_po(ctx: Ctx, po_id) -> PurchaseOrder:
    ctx.require("purchasing.create")
    po = get_po(ctx, po_id)
    if po.status == "partially_received":
        raise InvalidState("Partially received orders cannot be cancelled; receive or close the remainder.")
    _transition(po, "cancelled")
    ctx.audit("po.cancel", "purchase_order", po.id)
    return po


def receive_goods(ctx: Ctx, po_id, lines: list[dict], idempotency_key: str | None = None,
                  allow_over_receipt: bool = False) -> GoodsReceipt:
    """lines: [{purchase_order_item_id, received_quantity, accepted_quantity, rejected_quantity}].
    Only ACCEPTED quantity increases stock. received must equal accepted + rejected. A PO alone never
    changes stock. Over-receipt is refused unless explicitly allowed."""
    ctx.require("purchasing.receive")
    po = get_po(ctx, po_id)
    if idempotency_key:
        dup = ctx.db.scalar(select(GoodsReceipt).where(GoodsReceipt.organization_id == ctx.org_id,
                                                       GoodsReceipt.idempotency_key == idempotency_key))
        if dup:
            return dup
    if po.status not in OPEN_STATES:
        raise InvalidState(f"Cannot receive goods against an order in state '{po.status}'.")
    if not lines:
        raise InvalidState("No receipt lines supplied.")
    by_id = {i.id: i for i in po.items}
    gr = GoodsReceipt(organization_id=ctx.org_id, purchase_order_id=po.id, receipt_number=next_number(ctx, "receipt", "GR"),
                      received_by=ctx.user_id, idempotency_key=idempotency_key)
    pending: list[tuple[PurchaseOrderItem, Decimal, Decimal]] = []
    touched: dict = {}
    for ln in lines:
        item = by_id.get(ln["purchase_order_item_id"])
        if not item:
            raise NotFound("Purchase order item not found on this order.")
        rec, acc, rej = D(ln["received_quantity"]), D(ln["accepted_quantity"]), D(ln.get("rejected_quantity", 0))
        if rec <= 0 or acc < 0 or rej < 0 or acc + rej != rec:
            raise InvalidState("received must be positive and equal accepted + rejected.")
        touched[item.id] = touched.get(item.id, Decimal(0)) + rec
        delivered = item.received_quantity + item.rejected_quantity + touched[item.id]
        if delivered > item.ordered_quantity and not allow_over_receipt:
            raise InvalidState(f"Over-receipt: ordered {item.ordered_quantity:g}, already delivered "
                               f"{item.received_quantity + item.rejected_quantity:g}, this receipt {touched[item.id]:g}.")
        gr.items.append(GoodsReceiptItem(purchase_order_item_id=item.id, variant_id=item.variant_id,
                                         received_quantity=rec, accepted_quantity=acc, rejected_quantity=rej))
        pending.append((item, acc, rej))
    ctx.db.add(gr)
    ctx.db.flush()
    for n_, (item, acc, rej) in enumerate(pending):
        item.received_quantity += acc
        item.rejected_quantity += rej
        if acc > 0:
            apply_movement(ctx, warehouse_id=po.warehouse_id, variant_id=item.variant_id, movement_type="receipt",
                           delta=acc, reference_type="goods_receipt", reference_id=gr.id,
                           reason=f"{gr.receipt_number} / {po.order_number}",
                           idempotency_key=f"gr:{gr.id}:{n_}")
    complete = all(i.received_quantity + i.rejected_quantity >= i.ordered_quantity for i in po.items)
    if complete:
        _transition(po, "received")
        po.completed_at = utcnow()
    elif po.status == "approved":
        _transition(po, "partially_received")
    ctx.audit("po.receive", "goods_receipt", gr.id, po=str(po.id), complete=complete)
    return gr


def on_order_by_variant(ctx: Ctx, branch_id) -> dict:
    """Outstanding quantity on approved / partially received POs (not yet delivered)."""
    rows = ctx.db.execute(
        select(PurchaseOrderItem.variant_id,
               func.sum(PurchaseOrderItem.ordered_quantity - PurchaseOrderItem.received_quantity
                        - PurchaseOrderItem.rejected_quantity))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .where(PurchaseOrder.organization_id == ctx.org_id, PurchaseOrder.branch_id == branch_id,
               PurchaseOrder.status.in_(OPEN_STATES)).group_by(PurchaseOrderItem.variant_id))
    return {v: max(D(q), Decimal(0)) for v, q in rows}


def supplier_performance(ctx: Ctx, supplier_id) -> dict:
    """Average lead time (order -> completion, days) and on-time rate for completed POs."""
    ctx.require("suppliers.read")
    get_supplier(ctx, supplier_id)
    pos = ctx.db.scalars(select(PurchaseOrder).where(
        PurchaseOrder.organization_id == ctx.org_id, PurchaseOrder.supplier_id == supplier_id,
        PurchaseOrder.status == "received", PurchaseOrder.completed_at.is_not(None))).all()
    if not pos:
        return {"completed_orders": 0, "avg_lead_time_days": None, "on_time_rate": None}
    leads = [(p.completed_at.date() - p.order_date).days for p in pos]
    with_exp = [p for p in pos if p.expected_date]
    on_time = (sum(1 for p in with_exp if p.completed_at.date() <= p.expected_date) / len(with_exp)
               if with_exp else None)
    return {"completed_orders": len(pos), "avg_lead_time_days": sum(leads) / len(leads), "on_time_rate": on_time}
