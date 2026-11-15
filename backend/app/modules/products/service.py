"""Catalog: products/variants, listing, CSV import (validate-then-commit) and export."""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import Conflict, InvalidState, NotFound
from app.domain.analytics import to_csv
from app.modules.common import Ctx, D, money
from app.modules.models import Branch, Category, Product, Variant

MAX_IMPORT_ROWS = 20000
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def create_product(ctx: Ctx, *, sku: str, name: str, cost_price=0, selling_price=0, category: str | None = None,
                   unit: str = "unit", tracking_mode: str = "standard", shelf_life_days: int | None = None,
                   description: str | None = None, attributes: dict | None = None,
                   variants: list[dict] | None = None) -> Product:
    ctx.require("products.write")
    if tracking_mode not in ("standard", "batch", "serial"):
        raise InvalidState("tracking_mode must be standard, batch or serial.")
    if D(cost_price) < 0 or D(selling_price) < 0:
        raise InvalidState("Prices cannot be negative.")
    cat_id = None
    if category:
        cat = ctx.db.scalar(select(Category).where(Category.organization_id == ctx.org_id, Category.name == category))
        if not cat:
            cat = Category(organization_id=ctx.org_id, name=category)
            ctx.db.add(cat)
            ctx.db.flush()
        cat_id = cat.id
    p = Product(organization_id=ctx.org_id, sku=sku.strip(), name=name.strip(), description=description,
                category_id=cat_id, unit=unit, cost_price=money(cost_price), selling_price=money(selling_price),
                tracking_mode=tracking_mode, shelf_life_days=shelf_life_days, attributes=attributes or {})
    for v in (variants or [{"sku": sku.strip()}]):
        p.variants.append(Variant(organization_id=ctx.org_id, sku=v["sku"].strip(), barcode=v.get("barcode"),
                                  attributes=v.get("attributes", {}),
                                  cost_price_override=money(v["cost_price"]) if v.get("cost_price") is not None else None,
                                  selling_price_override=money(v["selling_price"]) if v.get("selling_price") is not None else None))
    ctx.db.add(p)
    try:
        ctx.db.flush()
    except IntegrityError as e:
        ctx.db.rollback()
        raise Conflict("A product or variant with this SKU already exists in your organization.") from e
    ctx.audit("product.create", "product", p.id, sku=p.sku)
    return p


def get_product(ctx: Ctx, product_id) -> Product:
    ctx.require("products.read")
    p = ctx.db.scalar(select(Product).where(Product.id == product_id, Product.organization_id == ctx.org_id))
    if not p:
        raise NotFound("Product not found.")
    return p


def list_products(ctx: Ctx, q: str | None = None, status: str | None = None, sort: str = "name",
                  page: int = 1, page_size: int = 25):
    ctx.require("products.read")
    base = select(Product).where(Product.organization_id == ctx.org_id)
    if q:
        like = f"%{q.lower()}%"
        base = base.where(or_(func.lower(Product.name).like(like), func.lower(Product.sku).like(like)))
    if status:
        base = base.where(Product.status == status)
    total = ctx.db.scalar(select(func.count()).select_from(base.subquery()))
    col = {"name": Product.name, "sku": Product.sku, "created": Product.created_at}.get(sort, Product.name)
    rows = ctx.db.scalars(base.order_by(col).offset((max(page, 1) - 1) * page_size).limit(min(page_size, 200))).all()
    return rows, total


def _parse_rows(text: str):
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise InvalidState("CSV too large (max 5 MB).")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if len(rows) > MAX_IMPORT_ROWS:
        raise InvalidState(f"Too many rows (max {MAX_IMPORT_ROWS}).")
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in rows]


def import_products_csv(ctx: Ctx, text: str, commit: bool = False, mapping: dict[str, str] | None = None) -> dict:
    """Columns: sku,name,cost_price,selling_price,category,unit,barcode,tracking_mode. `mapping` maps
    your column names to these. Validation runs for every row; commit happens only when there are zero
    row errors (all-or-nothing) so nothing is silently dropped."""
    ctx.require("products.write")
    rows = _parse_rows(text)
    if mapping:
        rows = [{mapping.get(k, k): v for k, v in r.items()} for r in rows]
    errors, valid, seen = [], [], set()
    existing = set(ctx.db.scalars(select(Product.sku).where(Product.organization_id == ctx.org_id)))
    for n, r in enumerate(rows, start=2):  # header is line 1
        errs = []
        sku = r.get("sku", "")
        if not sku:
            errs.append("sku is required")
        elif sku in existing:
            errs.append("duplicate: SKU already exists")
        elif sku in seen:
            errs.append("duplicate: SKU repeated in file")
        if not r.get("name"):
            errs.append("name is required")
        for f in ("cost_price", "selling_price"):
            try:
                if r.get(f) and Decimal(r[f]) < 0:
                    errs.append(f"{f} cannot be negative")
            except InvalidOperation:
                errs.append(f"{f} is not a number")
        if r.get("tracking_mode") and r["tracking_mode"] not in ("standard", "batch", "serial"):
            errs.append("tracking_mode invalid")
        seen.add(sku)
        (errors.append({"line": n, "sku": sku, "errors": errs}) if errs else valid.append(r))
    result = {"total_rows": len(rows), "valid_rows": len(valid), "error_rows": len(errors), "errors": errors,
              "committed": False}
    if commit and not errors:
        for r in valid:
            create_product(ctx, sku=r["sku"], name=r["name"], cost_price=r.get("cost_price") or 0,
                           selling_price=r.get("selling_price") or 0, category=r.get("category") or None,
                           unit=r.get("unit") or "unit", tracking_mode=r.get("tracking_mode") or "standard",
                           variants=[{"sku": r["sku"], "barcode": r.get("barcode") or None}])
        ctx.audit("product.import", "product", None, rows=len(valid))
        result["committed"] = True
    return result


def import_sales_csv(ctx: Ctx, text: str, commit: bool = False) -> dict:
    """Historical sales import. Columns: date(YYYY-MM-DD), branch_code, sku, quantity, unit_price[, sale_ref].
    Rows sharing (date, branch_code, sale_ref) become one sale. Imported history does NOT change current stock
    (apply_stock=False): your opening balance should reflect today's stock."""
    from app.modules.sales.service import create_sale  # local import avoids cycle
    ctx.require("sales.create")
    rows = _parse_rows(text)
    branches = {b.code: b for b in ctx.db.scalars(select(Branch).where(Branch.organization_id == ctx.org_id))}
    variants = {v.sku: v for v in ctx.db.scalars(select(Variant).where(Variant.organization_id == ctx.org_id))}
    errors, groups = [], {}
    for n, r in enumerate(rows, start=2):
        errs = []
        try:
            d = date.fromisoformat(r.get("date", ""))
        except ValueError:
            d = None
            errs.append("date must be YYYY-MM-DD")
        b = branches.get(r.get("branch_code", ""))
        if not b:
            errs.append("unknown branch_code")
        elif ctx.allowed_branches is not None and str(b.id) not in ctx.allowed_branches:
            errs.append("branch not permitted for your role")
        v = variants.get(r.get("sku", ""))
        if not v:
            errs.append("unknown sku")
        try:
            q = Decimal(r.get("quantity", ""))
            if q <= 0:
                errs.append("quantity must be positive")
        except InvalidOperation:
            errs.append("quantity is not a number")
        price = None
        if r.get("unit_price"):
            try:
                price = Decimal(r["unit_price"])
            except InvalidOperation:
                errs.append("unit_price is not a number")
        if errs:
            errors.append({"line": n, "sku": r.get("sku"), "errors": errs})
            continue
        groups.setdefault((d, b.id, r.get("sale_ref", "")), []).append(
            {"variant_id": v.id, "quantity": q, "unit_price": price})
    result = {"total_rows": len(rows), "sales_to_create": len(groups), "error_rows": len(errors), "errors": errors,
              "committed": False}
    if commit and not errors:
        for (d, bid, _), items in groups.items():
            create_sale(ctx, branch_id=bid, items=items, sale_date=d, source="import", apply_stock=False)
        result["committed"] = True
    return result


def export_products_csv(ctx: Ctx) -> str:
    ctx.require("products.read")
    rows = [{"sku": v.sku, "product": v.product.name, "barcode": v.barcode, "cost_price": v.cost,
             "selling_price": v.price, "status": v.status}
            for v in ctx.db.scalars(select(Variant).where(Variant.organization_id == ctx.org_id))]
    return to_csv(rows, ["sku", "product", "barcode", "cost_price", "selling_price", "status"])
