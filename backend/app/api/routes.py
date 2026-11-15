"""REST API under /api/v1. Routes validate input, call services (which enforce permissions + tenancy)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import schemas as S
from app.api.deps import ctx_dep, current_user
from app.api.serializers import (_v, movement_out, po_out, product_out, rec_out, sale_out, ser)
from app.core.config import get_settings
from app.core.exceptions import Forbidden
from app.database.base import get_db
from app.modules.analytics import service as analytics
from app.modules.auth import service as auth
from app.modules.common import Ctx
from app.modules.forecasting import service as forecasting
from app.modules.inventory import counts as stockcounts
from app.modules.inventory import service as inventory
from app.modules.models import (AuditLog, Branch, ForecastRun, GoodsReceipt, InventoryBalance, Membership,
                                Organization, PurchaseOrder, Recommendation, Sale, StockMovement, Supplier, User,
                                Variant, Warehouse)
from app.modules.products import service as products
from app.modules.purchasing import service as purchasing
from app.modules.replenishment import service as replenishment
from app.modules.sales import service as sales

api = APIRouter(prefix="/api/v1")


def page_args(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
    return page, page_size


def commit(ctx: Ctx):
    ctx.db.commit()


# ------------------------------------------------------------------ auth
@api.post("/auth/register", status_code=201, tags=["auth"])
def register(body: S.RegisterIn, request: Request, db: Session = Depends(get_db)):
    auth.auth_limiter.check(f"reg:{request.client.host if request.client else '-'}")
    u = auth.register(db, body.email, body.password, body.full_name)
    db.commit()
    return {"id": str(u.id), "email": u.email, "full_name": u.full_name}


@api.post("/auth/login", tags=["auth"])
def login(body: S.LoginIn, request: Request, db: Session = Depends(get_db)):
    auth.auth_limiter.check(f"login:{request.client.host if request.client else '-'}:{body.email.lower()}")
    return {"access_token": auth.login(db, body.email, body.password), "token_type": "bearer"}


@api.post("/auth/logout", status_code=204, tags=["auth"])
def logout(user: User = Depends(current_user), db: Session = Depends(get_db)):
    auth.logout_everywhere(db, user)
    db.commit()


@api.get("/auth/me", tags=["auth"])
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(Membership, Organization).join(Organization, Organization.id == Membership.organization_id)
                      .where(Membership.user_id == user.id, Membership.status == "active"))
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name,
            "organizations": [{"id": str(o.id), "name": o.name, "role": m.role, "currency": o.currency,
                               "is_demo": o.is_demo} for m, o in rows]}


@api.post("/auth/password", status_code=204, tags=["auth"])
def password(body: dict, user: User = Depends(current_user), db: Session = Depends(get_db)):
    auth.change_password(db, user, body.get("current_password", ""), body.get("new_password", ""))
    db.commit()


@api.post("/auth/accept-invitation", tags=["auth"])
def accept(body: dict, user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = auth.accept_invitation(db, user, body.get("token", ""))
    db.commit()
    return {"organization_id": str(m.organization_id), "role": m.role}


# --------------------------------------------------------- organizations
@api.post("/organizations", status_code=201, tags=["organizations"])
def create_org(body: S.OrgIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    org = auth.create_organization(db, user, **body.model_dump())
    db.commit()
    return ser(org, ["id", "name", "industry", "currency", "timezone"])


@api.get("/organizations/current", tags=["organizations"])
def current_org(ctx: Ctx = Depends(ctx_dep)):
    org = ctx.db.get(Organization, ctx.org_id)
    return ser(org, ["id", "name", "industry", "currency", "timezone", "settings", "is_demo"]) | {"role": ctx.role}


@api.patch("/organizations/current/settings", tags=["organizations"])
def update_settings(body: dict, ctx: Ctx = Depends(ctx_dep)):
    ctx.require("org.manage")
    org = ctx.db.get(Organization, ctx.org_id)
    allowed = {"low_stock_threshold", "overstock_days", "dead_stock_days", "slow_units_per_30d"}
    th = {k: v for k, v in body.get("thresholds", {}).items() if k in allowed and isinstance(v, (int, float)) and v >= 0}
    org.settings = {**(org.settings or {}), "thresholds": {**(org.settings or {}).get("thresholds", {}), **th}}
    ctx.audit("org.settings", "organization", org.id, thresholds=th)
    commit(ctx)
    return org.settings


@api.post("/memberships/invitations", status_code=201, tags=["memberships"])
def invite(body: S.InviteIn, ctx: Ctx = Depends(ctx_dep)):
    inv, raw = auth.invite(ctx, body.email, body.role, body.branch_ids)
    commit(ctx)
    return {"id": str(inv.id), "email": inv.email, "role": inv.role, "expires_at": _v(inv.expires_at),
            "token": raw, "note": "Shown once. Deliver it to the invitee; only its hash is stored."}


@api.get("/memberships", tags=["memberships"])
def members(ctx: Ctx = Depends(ctx_dep)):
    ctx.require("members.manage")
    rows = ctx.db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                          .where(Membership.organization_id == ctx.org_id))
    return [{"user_id": str(u.id), "email": u.email, "full_name": u.full_name, "role": m.role,
             "branch_ids": m.branch_ids, "status": m.status} for m, u in rows]


@api.patch("/memberships/{user_id}", tags=["memberships"])
def update_member(user_id: uuid.UUID, body: dict, ctx: Ctx = Depends(ctx_dep)):
    ctx.require("members.manage")
    m = ctx.db.scalar(select(Membership).where(Membership.organization_id == ctx.org_id, Membership.user_id == user_id))
    if not m:
        from app.core.exceptions import NotFound
        raise NotFound("Member not found.")
    if (m.role == "owner" or body.get("role") == "owner") and ctx.role != "owner":
        raise Forbidden("Only an owner can change owner memberships.")
    if m.role == "owner" and body.get("status") == "disabled" and ctx.db.scalar(
            select(func.count()).select_from(Membership).where(Membership.organization_id == ctx.org_id,
                                                               Membership.role == "owner", Membership.status == "active")) <= 1:
        raise Forbidden("Cannot disable the last owner.")
    for k in ("role", "branch_ids", "status"):
        if k in body:
            setattr(m, k, body[k])
    ctx.audit("member.update", "membership", m.id, **{k: body[k] for k in body if k in ("role", "status")})
    commit(ctx)
    return {"user_id": str(user_id), "role": m.role, "status": m.status}


# ------------------------------------------------ branches & warehouses
@api.get("/branches", tags=["branches"])
def list_branches(ctx: Ctx = Depends(ctx_dep)):
    q = select(Branch).where(Branch.organization_id == ctx.org_id).order_by(Branch.name)
    allowed = ctx.allowed_branches
    return [ser(b, ["id", "name", "code", "address", "status"]) for b in ctx.db.scalars(q)
            if allowed is None or str(b.id) in allowed]


@api.post("/branches", status_code=201, tags=["branches"])
def create_branch(body: S.BranchIn, ctx: Ctx = Depends(ctx_dep)):
    ctx.require("branches.manage")
    b = Branch(organization_id=ctx.org_id, **body.model_dump())
    ctx.db.add(b)
    ctx.audit("branch.create", "branch", None, code=body.code)
    commit(ctx)
    return ser(b, ["id", "name", "code", "address", "status"])


@api.get("/warehouses", tags=["warehouses"])
def list_warehouses(branch_id: uuid.UUID | None = None, ctx: Ctx = Depends(ctx_dep)):
    q = select(Warehouse).where(Warehouse.organization_id == ctx.org_id)
    if branch_id:
        q = q.where(Warehouse.branch_id == branch_id)
    allowed = ctx.allowed_branches
    return [ser(w, ["id", "branch_id", "name", "code", "status"]) for w in ctx.db.scalars(q)
            if allowed is None or str(w.branch_id) in allowed]


@api.post("/warehouses", status_code=201, tags=["warehouses"])
def create_warehouse(body: S.WarehouseIn, ctx: Ctx = Depends(ctx_dep)):
    ctx.require("branches.manage")
    ctx.check_branch(body.branch_id)
    w = Warehouse(organization_id=ctx.org_id, **body.model_dump())
    ctx.db.add(w)
    commit(ctx)
    return ser(w, ["id", "branch_id", "name", "code", "status"])


# --------------------------------------------------------------- products
@api.post("/products", status_code=201, tags=["products"])
def create_product(body: S.ProductIn, ctx: Ctx = Depends(ctx_dep)):
    p = products.create_product(ctx, **{**body.model_dump(exclude={"variants"}),
                                        "variants": [v.model_dump() for v in body.variants] if body.variants else None})
    commit(ctx)
    return product_out(p)


@api.get("/products", tags=["products"])
def list_products(q: str | None = None, status: str | None = None, sort: str = "name", pg=Depends(page_args),
                  ctx: Ctx = Depends(ctx_dep)):
    rows, total = products.list_products(ctx, q, status, sort, *pg)
    return {"items": [product_out(p) for p in rows], "total": total, "page": pg[0], "page_size": pg[1]}


@api.get("/products/export.csv", tags=["products"], response_class=PlainTextResponse)
def export_products(ctx: Ctx = Depends(ctx_dep)):
    return PlainTextResponse(products.export_products_csv(ctx), media_type="text/csv")


@api.post("/products/import", tags=["products"])
def import_products(body: S.CsvIn, ctx: Ctx = Depends(ctx_dep)):
    res = products.import_products_csv(ctx, body.csv, body.commit, body.mapping)
    commit(ctx)
    return res


@api.get("/products/{product_id}", tags=["products"])
def get_product(product_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return product_out(products.get_product(ctx, product_id))


# -------------------------------------------------------------- inventory
@api.get("/inventory/balances", tags=["inventory"])
def balances(warehouse_id: uuid.UUID | None = None, branch_id: uuid.UUID | None = None, pg=Depends(page_args),
             ctx: Ctx = Depends(ctx_dep)):
    ctx.require("inventory.read")
    q = (select(InventoryBalance, Variant.sku).join(Variant, Variant.id == InventoryBalance.variant_id)
         .join(Warehouse, Warehouse.id == InventoryBalance.warehouse_id)
         .where(InventoryBalance.organization_id == ctx.org_id))
    if warehouse_id:
        q = q.where(InventoryBalance.warehouse_id == warehouse_id)
    if branch_id:
        ctx.check_branch(branch_id)
        q = q.where(Warehouse.branch_id == branch_id)
    allowed = ctx.allowed_branches
    if allowed is not None:
        q = q.where(Warehouse.branch_id.in_([uuid.UUID(a) for a in allowed] or [uuid.uuid4()]))
    page, size = pg
    rows = ctx.db.execute(q.order_by(Variant.sku).offset((page - 1) * size).limit(size)).all()
    return {"items": [{**ser(b, ["warehouse_id", "variant_id", "on_hand_quantity", "unavailable_quantity",
                                 "reserved_quantity"]), "sku": sku,
                       "available": _v(b.on_hand_quantity - b.reserved_quantity)} for b, sku in rows]}


@api.get("/inventory/movements", tags=["inventory"])
def movements(variant_id: uuid.UUID | None = None, warehouse_id: uuid.UUID | None = None, pg=Depends(page_args),
              ctx: Ctx = Depends(ctx_dep)):
    ctx.require("inventory.read")
    q = (select(StockMovement).join(Warehouse, Warehouse.id == StockMovement.warehouse_id)
         .where(StockMovement.organization_id == ctx.org_id))
    if variant_id:
        q = q.where(StockMovement.variant_id == variant_id)
    if warehouse_id:
        q = q.where(StockMovement.warehouse_id == warehouse_id)
    allowed = ctx.allowed_branches
    if allowed is not None:
        q = q.where(Warehouse.branch_id.in_([uuid.UUID(a) for a in allowed] or [uuid.uuid4()]))
    page, size = pg
    return {"items": [movement_out(m) for m in ctx.db.scalars(
        q.order_by(StockMovement.occurred_at.desc()).offset((page - 1) * size).limit(size))]}


@api.post("/inventory/opening-balances", status_code=201, tags=["inventory"])
def opening(body: S.OpeningIn, ctx: Ctx = Depends(ctx_dep), idempotency_key: str | None = Header(default=None)):
    m = inventory.opening_balance(ctx, body.warehouse_id, body.variant_id, body.quantity, idempotency_key)
    commit(ctx)
    return movement_out(m)


@api.post("/inventory/adjustments", status_code=201, tags=["inventory"])
def adjust(body: S.AdjustIn, ctx: Ctx = Depends(ctx_dep), idempotency_key: str | None = Header(default=None)):
    ms = inventory.adjust_stock(ctx, body.warehouse_id, body.variant_id, body.quantity, body.reason, body.note,
                                idempotency_key)
    commit(ctx)
    return [movement_out(m) for m in ms]


@api.post("/inventory/transfers", status_code=201, tags=["inventory"])
def transfer(body: S.TransferIn, ctx: Ctx = Depends(ctx_dep), idempotency_key: str | None = Header(default=None)):
    t = inventory.transfer_stock(ctx, body.source_warehouse_id, body.destination_warehouse_id,
                                 [i.model_dump() for i in body.items], idempotency_key)
    commit(ctx)
    return {"id": str(t.id), "status": t.status}


@api.get("/inventory/reconciliation", tags=["inventory"])
def reconciliation(ctx: Ctx = Depends(ctx_dep)):
    ctx.require("inventory.read")
    problems = inventory.reconcile(ctx)
    return {"consistent": not problems, "mismatches": problems}


# ------------------------------------------------------------------ sales
@api.post("/sales", status_code=201, tags=["sales"])
def create_sale(body: S.SaleIn, ctx: Ctx = Depends(ctx_dep), idempotency_key: str | None = Header(default=None)):
    s = sales.create_sale(ctx, branch_id=body.branch_id, items=[i.model_dump() for i in body.items],
                          sale_date=body.sale_date, status=body.status, source=body.source,
                          warehouse_id=body.warehouse_id, idempotency_key=idempotency_key)
    commit(ctx)
    return sale_out(s)


@api.get("/sales", tags=["sales"])
def list_sales(branch_id: uuid.UUID | None = None, status: str | None = None, pg=Depends(page_args),
               ctx: Ctx = Depends(ctx_dep)):
    ctx.require("sales.read")
    q = select(Sale).where(Sale.organization_id == ctx.org_id)
    if branch_id:
        ctx.check_branch(branch_id)
        q = q.where(Sale.branch_id == branch_id)
    allowed = ctx.allowed_branches
    if allowed is not None:
        q = q.where(Sale.branch_id.in_([uuid.UUID(a) for a in allowed] or [uuid.uuid4()]))
    if status:
        q = q.where(Sale.status == status)
    page, size = pg
    return {"items": [sale_out(s) for s in ctx.db.scalars(
        q.order_by(Sale.sale_date.desc(), Sale.created_at.desc()).offset((page - 1) * size).limit(size))]}


@api.post("/sales/import", tags=["sales"])
def import_sales(body: S.CsvIn, ctx: Ctx = Depends(ctx_dep)):
    res = products.import_sales_csv(ctx, body.csv, body.commit)
    commit(ctx)
    return res


@api.get("/sales/{sale_id}", tags=["sales"])
def get_sale(sale_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return sale_out(sales.get_sale(ctx, sale_id))


@api.post("/sales/{sale_id}/complete", tags=["sales"])
def complete_sale(sale_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    s = sales.complete_draft(ctx, sale_id)
    commit(ctx)
    return sale_out(s)


@api.post("/sales/{sale_id}/cancel", tags=["sales"])
def cancel_sale(sale_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    s = sales.cancel_sale(ctx, sale_id)
    commit(ctx)
    return sale_out(s)


@api.post("/sales/{sale_id}/returns", status_code=201, tags=["sales"])
def create_return(sale_id: uuid.UUID, body: S.ReturnIn, ctx: Ctx = Depends(ctx_dep),
                  idempotency_key: str | None = Header(default=None)):
    r = sales.create_return(ctx, sale_id, [i.model_dump() for i in body.items], body.reason, idempotency_key)
    commit(ctx)
    return {"id": str(r.id), "return_number": r.return_number,
            "items": [ser(i, ["sale_item_id", "variant_id", "quantity", "restock", "refund_amount"]) for i in r.items]}


# -------------------------------------------------------------- suppliers
@api.post("/suppliers", status_code=201, tags=["suppliers"])
def create_supplier(body: S.SupplierIn, ctx: Ctx = Depends(ctx_dep)):
    s = purchasing.create_supplier(ctx, **body.model_dump())
    commit(ctx)
    return ser(s, ["id", "name", "contact_name", "email", "phone", "status"])


@api.get("/suppliers", tags=["suppliers"])
def list_suppliers(ctx: Ctx = Depends(ctx_dep)):
    ctx.require("suppliers.read")
    return [ser(s, ["id", "name", "contact_name", "email", "phone", "status"]) for s in
            ctx.db.scalars(select(Supplier).where(Supplier.organization_id == ctx.org_id).order_by(Supplier.name))]


@api.put("/suppliers/{supplier_id}/products", tags=["suppliers"])
def set_supplier_product(supplier_id: uuid.UUID, body: S.SupplierProductIn, ctx: Ctx = Depends(ctx_dep)):
    sp = purchasing.set_supplier_product(ctx, supplier_id, body.variant_id, body.unit_cost, body.lead_time_days,
                                         body.minimum_order_quantity, body.pack_size, body.supplier_sku)
    commit(ctx)
    return ser(sp, ["id", "supplier_id", "variant_id", "unit_cost", "lead_time_days", "minimum_order_quantity",
                    "pack_size"])


@api.get("/suppliers/{supplier_id}/performance", tags=["suppliers"])
def supplier_perf(supplier_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return purchasing.supplier_performance(ctx, supplier_id)


# --------------------------------------------------------- purchase orders
@api.post("/purchase-orders", status_code=201, tags=["purchasing"])
def create_po(body: S.POIn, ctx: Ctx = Depends(ctx_dep)):
    po = purchasing.create_po(ctx, branch_id=body.branch_id, warehouse_id=body.warehouse_id,
                              supplier_id=body.supplier_id, items=[i.model_dump() for i in body.items],
                              expected_date=body.expected_date)
    commit(ctx)
    return po_out(po)


@api.get("/purchase-orders", tags=["purchasing"])
def list_pos(status: str | None = None, pg=Depends(page_args), ctx: Ctx = Depends(ctx_dep)):
    ctx.require("purchasing.read")
    q = select(PurchaseOrder).where(PurchaseOrder.organization_id == ctx.org_id)
    allowed = ctx.allowed_branches
    if allowed is not None:
        q = q.where(PurchaseOrder.branch_id.in_([uuid.UUID(a) for a in allowed] or [uuid.uuid4()]))
    if status:
        q = q.where(PurchaseOrder.status == status)
    page, size = pg
    return {"items": [po_out(p) for p in ctx.db.scalars(
        q.order_by(PurchaseOrder.created_at.desc()).offset((page - 1) * size).limit(size))]}


@api.get("/purchase-orders/{po_id}", tags=["purchasing"])
def get_po(po_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return po_out(purchasing.get_po(ctx, po_id))


@api.post("/purchase-orders/{po_id}/{action}", tags=["purchasing"])
def po_action(po_id: uuid.UUID, action: str, ctx: Ctx = Depends(ctx_dep)):
    fn = {"submit": purchasing.submit_po, "approve": purchasing.approve_po, "cancel": purchasing.cancel_po}.get(action)
    if not fn:
        from app.core.exceptions import NotFound
        raise NotFound("Unknown action.")
    po = fn(ctx, po_id)
    commit(ctx)
    return po_out(po)


@api.post("/goods-receipts", status_code=201, tags=["purchasing"])
def receive(body: S.ReceiptIn, ctx: Ctx = Depends(ctx_dep), idempotency_key: str | None = Header(default=None)):
    gr = purchasing.receive_goods(ctx, body.purchase_order_id, [i.model_dump() for i in body.items], idempotency_key)
    commit(ctx)
    return {"id": str(gr.id), "receipt_number": gr.receipt_number,
            "items": [ser(i, ["purchase_order_item_id", "received_quantity", "accepted_quantity",
                              "rejected_quantity"]) for i in gr.items]}


# ------------------------------------------------------------ forecasting
@api.post("/forecasts", status_code=202, tags=["forecasting"])
def create_forecast(body: S.ForecastIn, ctx: Ctx = Depends(ctx_dep)):
    run = forecasting.create_run(ctx, body.branch_id, body.horizon_days, body.granularity, body.variant_ids,
                                 body.history_days)
    commit(ctx)
    from app.workers.dispatch import dispatch_forecast
    dispatch_forecast(ctx.db, run.id)   # Celery when configured, otherwise inline
    ctx.db.refresh(run)
    return ser(run, ["id", "branch_id", "horizon_days", "granularity", "status", "metrics", "error"])


@api.get("/forecasts", tags=["forecasting"])
def list_forecasts(ctx: Ctx = Depends(ctx_dep)):
    ctx.require("forecast.read")
    allowed = ctx.allowed_branches
    q = select(ForecastRun).where(ForecastRun.organization_id == ctx.org_id).order_by(ForecastRun.created_at.desc()).limit(100)
    return [ser(r, ["id", "branch_id", "horizon_days", "granularity", "status", "metrics", "error", "created_at",
                    "completed_at"]) for r in ctx.db.scalars(q) if allowed is None or str(r.branch_id) in allowed]


@api.get("/forecasts/{run_id}", tags=["forecasting"])
def get_forecast(run_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    r = forecasting.get_run(ctx, run_id)
    return ser(r, ["id", "branch_id", "horizon_days", "granularity", "status", "metrics", "error", "configuration",
                   "created_at", "completed_at"])


@api.get("/forecasts/{run_id}/results", tags=["forecasting"])
def forecast_results(run_id: uuid.UUID, variant_id: uuid.UUID | None = None, ctx: Ctx = Depends(ctx_dep)):
    rows = forecasting.run_results(ctx, run_id, variant_id)
    run = forecasting.get_run(ctx, run_id)
    out = {"run_status": run.status, "results": [
        ser(r, ["variant_id", "forecast_period", "predicted_quantity", "lower_bound", "upper_bound", "model_name",
                "confidence_metadata"]) for r in rows]}
    if variant_id:
        out["history"] = forecasting.history_with_actuals(ctx, run.branch_id, variant_id)
    return out


# ----------------------------------------------------------- replenishment
@api.post("/replenishment/recommendations/generate", status_code=201, tags=["replenishment"])
def gen_recs(body: S.GenerateRecsIn, ctx: Ctx = Depends(ctx_dep)):
    recs = replenishment.generate(ctx, body.branch_id, body.service_level, body.review_period_days, body.budget)
    commit(ctx)
    return [rec_out(r) for r in recs]


@api.get("/replenishment/recommendations", tags=["replenishment"])
def list_recs(branch_id: uuid.UUID | None = None, status: str | None = "pending", ctx: Ctx = Depends(ctx_dep)):
    ctx.require("replenishment.read")
    q = select(Recommendation).where(Recommendation.organization_id == ctx.org_id)
    if branch_id:
        ctx.check_branch(branch_id)
        q = q.where(Recommendation.branch_id == branch_id)
    if status:
        q = q.where(Recommendation.status == status)
    allowed = ctx.allowed_branches
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = [r for r in ctx.db.scalars(q) if allowed is None or str(r.branch_id) in allowed]
    return [rec_out(r) for r in sorted(rows, key=lambda r: order.get(r.urgency, 9))]


@api.post("/replenishment/recommendations/convert", tags=["replenishment"])
def convert(body: S.ConvertIn, ctx: Ctx = Depends(ctx_dep)):
    pos = replenishment.convert_to_draft_po(ctx, body.recommendation_ids)
    commit(ctx)
    return [po_out(p) for p in pos]


@api.post("/replenishment/recommendations/{rec_id}/{decision}", tags=["replenishment"])
def decide(rec_id: uuid.UUID, decision: str, ctx: Ctx = Depends(ctx_dep)):
    if decision not in ("accept", "reject"):
        from app.core.exceptions import NotFound
        raise NotFound("Unknown decision.")
    r = replenishment.decide(ctx, rec_id, decision == "accept")
    commit(ctx)
    return rec_out(r)


# --------------------------------------------------------------- analytics
@api.get("/analytics/overview", tags=["analytics"])
def overview(branch_id: uuid.UUID | None = None, days: int = Query(30, ge=1, le=365), ctx: Ctx = Depends(ctx_dep)):
    return analytics.overview(ctx, branch_id, days)


@api.get("/analytics/inventory-health", tags=["analytics"])
def health(branch_id: uuid.UUID | None = None, days: int = Query(90, ge=7, le=365), ctx: Ctx = Depends(ctx_dep)):
    return analytics.inventory_health(ctx, branch_id, days)


@api.get("/audit-logs", tags=["audit"])
def audit_logs(pg=Depends(page_args), ctx: Ctx = Depends(ctx_dep)):
    ctx.require("audit.read")
    page, size = pg
    rows = ctx.db.scalars(select(AuditLog).where(AuditLog.organization_id == ctx.org_id)
                          .order_by(AuditLog.occurred_at.desc()).offset((page - 1) * size).limit(size))
    return {"items": [ser(a, ["id", "actor_user_id", "action", "entity_type", "entity_id", "meta", "occurred_at"])
                      for a in rows]}


@api.get("/assistant/status", tags=["assistant"])
def assistant_status():
    s = get_settings()
    return {"configured": bool(s.llm_provider and s.llm_api_key), "implemented": False,
            "message": "The AI assistant is not implemented in this build."}


# ------------------------------------------------------------ stock counts
def _count_out(c) -> dict:
    d = ser(c, ["id", "warehouse_id", "status", "note", "created_at"])
    d["lines"] = [ser(ln, ["variant_id", "expected_quantity", "counted_quantity"]) for ln in c.lines]
    return d


@api.post("/stock-counts", status_code=201, tags=["inventory"])
def start_count(body: S.CountStartIn, ctx: Ctx = Depends(ctx_dep)):
    c = stockcounts.start_count(ctx, body.warehouse_id, body.variant_ids, body.note)
    commit(ctx)
    return _count_out(c)


@api.get("/stock-counts/{count_id}", tags=["inventory"])
def get_count(count_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return _count_out(stockcounts.get_count(ctx, count_id))


@api.put("/stock-counts/{count_id}/counts", tags=["inventory"])
def record_counts(count_id: uuid.UUID, body: S.CountEntryIn, ctx: Ctx = Depends(ctx_dep)):
    c = stockcounts.record_counts(ctx, count_id, body.counts)
    commit(ctx)
    return _count_out(c)


@api.post("/stock-counts/{count_id}/submit", tags=["inventory"])
def submit_count(count_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    c = stockcounts.submit_count(ctx, count_id)
    commit(ctx)
    return _count_out(c)


@api.get("/stock-counts/{count_id}/review", tags=["inventory"])
def review_count(count_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    return stockcounts.review(ctx, count_id)


@api.post("/stock-counts/{count_id}/approve", tags=["inventory"])
def approve_count(count_id: uuid.UUID, ctx: Ctx = Depends(ctx_dep)):
    c = stockcounts.approve_count(ctx, count_id)
    commit(ctx)
    return _count_out(c)


# ----------------------------------------------------------------- reports
@api.get("/reports/inventory.xlsx", tags=["reports"])
def inventory_report(branch_id: uuid.UUID | None = None, days: int = Query(90, ge=7, le=365),
                     ctx: Ctx = Depends(ctx_dep)):
    from app.domain.reports import build_workbook
    h = analytics.inventory_health(ctx, branch_id, days)
    recs = list(ctx.db.scalars(select(Recommendation).where(
        Recommendation.organization_id == ctx.org_id, Recommendation.status == "pending")))
    allowed = ctx.allowed_branches
    recs = [r for r in recs if allowed is None or str(r.branch_id) in allowed]
    data = build_workbook({
        "Summary": (["Metric", "Value"], [[k, v if not isinstance(v, dict) else str(v)] for k, v in h["summary"].items()]),
        "Inventory": (["SKU", "Name", "On hand", "Unavailable", "Value at cost", "Status", "Units sold",
                       "Days of inventory", "ABC", "Last sale", "Dead-stock estimate"],
                      [[i["sku"], i["name"], i["on_hand"], i["unavailable"], i["value_at_cost"], i["status"],
                        i["units_sold_period"], i["days_of_inventory"], i["abc"], i["last_sale"],
                        "yes" if i["dead_stock_estimate"] else "no"] for i in h["items"]]),
        "Replenishment": (["Variant", "Quantity", "Est. cost", "Urgency", "Reason"],
                          [[str(r.variant_id), float(r.recommended_quantity), float(r.estimated_cost), r.urgency,
                            r.reason] for r in recs]),
    })
    ctx.audit("report.export", "report", None, kind="inventory.xlsx")
    commit(ctx)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="stocksense-inventory.xlsx"'})
