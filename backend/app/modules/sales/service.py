"""Sales, cancellations and returns. Stock changes happen only via the inventory engine."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from app.core.exceptions import InvalidState, NotFound
from app.modules.common import Ctx, D, money, next_number
from app.modules.inventory.service import SELLABLE, UNAVAILABLE, apply_movement
from app.modules.models import Sale, SaleItem, SalesReturn, SalesReturnItem, Variant, Warehouse


def create_sale(ctx: Ctx, *, branch_id, items: list[dict], sale_date: date | None = None, status: str = "completed",
                source: str = "manual", warehouse_id=None, idempotency_key: str | None = None,
                apply_stock: bool = True) -> Sale:
    ctx.require("sales.create")
    if status not in ("draft", "completed"):
        raise InvalidState("New sales must be 'draft' or 'completed'.")
    if not items:
        raise InvalidState("A sale needs at least one line item.")
    branch = ctx.check_branch(branch_id)
    if idempotency_key:
        dup = ctx.db.scalar(select(Sale).where(Sale.organization_id == ctx.org_id,
                                               Sale.idempotency_key == idempotency_key))
        if dup:
            return dup  # retried request: return the original, change nothing
    if warehouse_id:
        wh = ctx.check_warehouse(warehouse_id)
        if wh.branch_id != branch.id:
            raise InvalidState("Warehouse does not belong to the branch.")
    else:
        wh = ctx.db.scalar(select(Warehouse).where(Warehouse.organization_id == ctx.org_id,
                                                   Warehouse.branch_id == branch.id,
                                                   Warehouse.status == "active").order_by(Warehouse.created_at))
        if not wh:
            raise InvalidState("Branch has no active warehouse.")
    sale = Sale(organization_id=ctx.org_id, branch_id=branch.id, warehouse_id=wh.id,
                sale_number=next_number(ctx, "sale", "S"), sale_date=sale_date or date.today(),
                status=status, source=source, created_by=ctx.user_id, idempotency_key=idempotency_key)
    subtotal = disc = tax = Decimal("0")
    for it in items:
        v: Variant = ctx.get_variant(it["variant_id"])
        qty = D(it["quantity"])
        if qty <= 0:
            raise InvalidState("Quantities must be positive.")
        price = D(it["unit_price"]) if it.get("unit_price") is not None else v.price
        d, t = D(it.get("discount", 0)), D(it.get("tax", 0))
        gross = money(price * qty)
        line = gross - d + t
        if line < 0:
            raise InvalidState("Line discount exceeds line value.")
        sale.items.append(SaleItem(organization_id=ctx.org_id, variant_id=v.id, quantity=qty, unit_price=money(price),
                                   discount=money(d), tax=money(t), line_total=money(line), cost_snapshot=money(v.cost)))
        subtotal, disc, tax = subtotal + gross, disc + d, tax + t
    sale.subtotal, sale.discount_total, sale.tax_total = money(subtotal), money(disc), money(tax)
    sale.grand_total = money(subtotal - disc + tax)
    ctx.db.add(sale)
    ctx.db.flush()
    if status == "completed" and apply_stock:
        for n, line in enumerate(sale.items):
            apply_movement(ctx, warehouse_id=wh.id, variant_id=line.variant_id, movement_type="sale",
                           delta=-line.quantity, reference_type="sale", reference_id=sale.id,
                           reason=f"Sale {sale.sale_number}", idempotency_key=f"sale:{sale.id}:{n}")
    ctx.audit("sale.create", "sale", sale.id, total=str(sale.grand_total), status=status)
    return sale


def get_sale(ctx: Ctx, sale_id) -> Sale:
    ctx.require("sales.read")
    s = ctx.db.scalar(select(Sale).where(Sale.id == sale_id, Sale.organization_id == ctx.org_id))
    if not s:
        raise NotFound("Sale not found.")
    ctx.check_branch(s.branch_id)
    return s


def complete_draft(ctx: Ctx, sale_id) -> Sale:
    ctx.require("sales.create")
    s = get_sale(ctx, sale_id)
    if s.status != "draft":
        raise InvalidState(f"Only draft sales can be completed (status is {s.status}).")
    for n, line in enumerate(s.items):
        apply_movement(ctx, warehouse_id=s.warehouse_id, variant_id=line.variant_id, movement_type="sale",
                       delta=-line.quantity, reference_type="sale", reference_id=s.id,
                       reason=f"Sale {s.sale_number}", idempotency_key=f"sale:{s.id}:{n}")
    s.status = "completed"
    ctx.audit("sale.complete", "sale", s.id)
    return s


def cancel_sale(ctx: Ctx, sale_id) -> Sale:
    ctx.require("sales.cancel")
    s = get_sale(ctx, sale_id)
    if s.status == "cancelled":
        return s
    if s.status == "completed":
        returned = ctx.db.scalar(select(func.count()).select_from(SalesReturn).where(SalesReturn.sale_id == s.id))
        if returned:
            raise InvalidState("Sale has returns; cancel is not allowed. Process further returns instead.")
        for n, line in enumerate(s.items):
            apply_movement(ctx, warehouse_id=s.warehouse_id, variant_id=line.variant_id, movement_type="sale_reversal",
                           delta=line.quantity, reference_type="sale", reference_id=s.id,
                           reason=f"Cancel {s.sale_number}", idempotency_key=f"salecancel:{s.id}:{n}")
    s.status = "cancelled"
    ctx.audit("sale.cancel", "sale", s.id)
    return s


def returned_quantities(ctx: Ctx, sale_id) -> dict:
    rows = ctx.db.execute(select(SalesReturnItem.sale_item_id, func.sum(SalesReturnItem.quantity))
                          .join(SalesReturn, SalesReturn.id == SalesReturnItem.return_id)
                          .where(SalesReturn.sale_id == sale_id, SalesReturn.organization_id == ctx.org_id)
                          .group_by(SalesReturnItem.sale_item_id))
    return {i: D(q) for i, q in rows}


def create_return(ctx: Ctx, sale_id, items: list[dict], reason: str | None = None,
                  idempotency_key: str | None = None, return_date: date | None = None) -> SalesReturn:
    """items: [{sale_item_id, quantity, restock: bool}]. Restock only when approved as sellable;
    otherwise the units go to the unavailable bucket (not counted as sellable)."""
    ctx.require("sales.return")
    s = get_sale(ctx, sale_id)
    if s.status != "completed":
        raise InvalidState("Only completed sales can have returns.")
    if idempotency_key:
        dup = ctx.db.scalar(select(SalesReturn).where(SalesReturn.organization_id == ctx.org_id,
                                                      SalesReturn.idempotency_key == idempotency_key))
        if dup:
            return dup
    sold = {i.id: i for i in s.items}
    already = returned_quantities(ctx, s.id)
    ret = SalesReturn(organization_id=ctx.org_id, sale_id=s.id, return_number=next_number(ctx, "return", "R"),
                      return_date=return_date or date.today(), reason=reason, created_by=ctx.user_id,
                      idempotency_key=idempotency_key)
    batch: dict = {}
    for it in items:
        line = sold.get(it["sale_item_id"])
        if not line:
            raise NotFound("Sale item not found on this sale.")
        q = D(it["quantity"])
        batch[line.id] = batch.get(line.id, Decimal(0)) + q
        if q <= 0 or already.get(line.id, Decimal(0)) + batch[line.id] > line.quantity:
            raise InvalidState(f"Cannot return {q:g}: sold {line.quantity:g}, already returned "
                               f"{already.get(line.id, Decimal(0)):g}.")
        refund = money(line.line_total / line.quantity * q)
        ret.items.append(SalesReturnItem(sale_item_id=line.id, variant_id=line.variant_id, quantity=q,
                                         restock=bool(it.get("restock", False)), refund_amount=refund))
    ctx.db.add(ret)
    ctx.db.flush()
    for n, ri in enumerate(ret.items):
        apply_movement(ctx, warehouse_id=s.warehouse_id, variant_id=ri.variant_id,
                       movement_type="return_restock" if ri.restock else "return_unsellable", delta=ri.quantity,
                       bucket=SELLABLE if ri.restock else UNAVAILABLE, reference_type="return", reference_id=ret.id,
                       reason=reason, idempotency_key=f"return:{ret.id}:{n}")
    ctx.audit("sale.return", "sales_return", ret.id, sale_id=str(s.id))
    return ret
