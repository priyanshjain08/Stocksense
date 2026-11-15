"""Create a clearly-labelled DEMO organization: python -m scripts.seed_demo

Adds a new organization flagged is_demo; never modifies other organizations.
To reset a dev environment completely: `docker compose down -v` then start again.
Demo login: demo@stocksense.local with the password in $DEMO_PASSWORD (generated and printed if unset).
"""
from __future__ import annotations

import math
import os
import random
import secrets
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.database.base import Base, SessionLocal, engine
from app.modules import models as M
from app.modules.auth import service as auth
from app.modules.common import Ctx
from app.modules.inventory.service import opening_balance
from app.modules.products.service import create_product
from app.modules.purchasing import service as purchasing
from app.modules.sales.service import create_sale

CATALOG = [  # sku, name, category, cost, price, pattern, base_daily, supplier_idx, lead, moq, pack
    ("RICE-5", "Basmati Rice 5kg", "Grocery", 310, 399, "weekly", 9, 0, 4, 20, 10),
    ("OIL-1", "Sunflower Oil 1L", "Grocery", 120, 149, "weekly", 12, 0, 4, 24, 12),
    ("TEA-250", "Masala Tea 250g", "Grocery", 70, 95, "steady", 7, 0, 5, 12, 12),
    ("SOAP-4", "Bath Soap 4-pack", "Personal care", 80, 120, "weekly", 6, 1, 7, 12, 12),
    ("CREAM-50", "Face Cream 50g", "Personal care", 140, 220, "intermittent", 0.7, 1, 10, 6, 6),
    ("TSHIRT-M", "Cotton T-Shirt (M)", "Apparel", 180, 399, "seasonal", 4, 2, 14, 10, 5),
    ("TSHIRT-L", "Cotton T-Shirt (L)", "Apparel", 180, 399, "seasonal", 3, 2, 14, 10, 5),
    ("CABLE-C", "USB-C Cable 1m", "Electronics", 90, 249, "intermittent", 1.2, 3, 12, 10, 10),
    ("EARBUD", "Wired Earbuds", "Electronics", 150, 349, "steady", 2.2, 3, 12, 10, 10),
    ("NOTE-A5", "Notebook A5", "Stationery", 25, 55, "steady", 10, 4, 5, 50, 50),
    ("PEN-10", "Gel Pens (10)", "Stationery", 45, 90, "weekly", 5, 4, 5, 20, 20),
    ("DEAD-LAMP", "Novelty Desk Lamp", "Home", 300, 599, "none", 0, 3, 20, 4, 2),
]
SUPPLIERS = [("Sharma Wholesale", "Jaipur"), ("Bright Care Distributors", "Delhi"), ("Fashion Hub Exports", "Surat"),
             ("TechLink Traders", "Mumbai"), ("PaperWorld Supplies", "Ajmer")]


def demand(pattern: str, base: float, d: date, rng: random.Random) -> int:
    dow = [0.8, 0.8, 0.9, 0.9, 1.1, 1.5, 1.2][d.weekday()] if pattern in ("weekly", "seasonal") else 1.0
    season = 1 + 0.35 * math.sin(2 * math.pi * (d.timetuple().tm_yday - 60) / 365) if pattern == "seasonal" else 1.0
    if pattern == "intermittent":
        return rng.choice([1, 1, 2, 3]) if rng.random() < base / 2 else 0
    return max(0, int(rng.gauss(base * dow * season, max(base * 0.25, 0.3)) + 0.5)) if base else 0


def main():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    password = os.environ.get("DEMO_PASSWORD") or secrets.token_urlsafe(12) + "aA1"
    user = db.scalar(select(M.User).where(M.User.email == "demo@stocksense.local"))
    if not user:
        user = auth.register(db, "demo@stocksense.local", password, "Demo Owner")
    org = auth.create_organization(db, user, name="DEMO — Sample Retail Co. (demo data)", industry="general_retail",
                                   branch_name="Main Street Store", branch_code="MAIN", is_demo=True)
    db.flush()
    ctx = Ctx(db, user.id, org.id, "owner", [])
    main_br = db.scalar(select(M.Branch).where(M.Branch.organization_id == org.id))
    main_wh = db.scalar(select(M.Warehouse).where(M.Warehouse.organization_id == org.id))
    br2 = M.Branch(organization_id=org.id, name="Station Road Store", code="STN")
    db.add(br2)
    db.flush()
    wh2 = M.Warehouse(organization_id=org.id, branch_id=br2.id, name="Station Road Stockroom", code="STN-WH1")
    db.add(wh2)
    db.flush()
    sups = []
    for name, city in SUPPLIERS:
        sups.append(purchasing.create_supplier(ctx, name=name, address=city, notes="DEMO supplier"))
    rng = random.Random(42)
    variants = {}
    for sku, name, cat, cost, price, pat, base, si, lead, moq, pack in CATALOG:
        p = create_product(ctx, sku=sku, name=name, category=cat, cost_price=cost, selling_price=price,
                           shelf_life_days=365 if cat == "Grocery" else None)
        v = p.variants[0]
        variants[sku] = (v, pat, base)
        purchasing.set_supplier_product(ctx, sups[si].id, v.id, cost, lead_time_days=lead, moq=moq, pack_size=pack)
        stock = 0 if sku == "RICE-5" else (160 if sku == "DEAD-LAMP" else max(int(base * 12), 6))
        if stock:
            opening_balance(ctx, main_wh.id, v.id, stock)
            opening_balance(ctx, wh2.id, v.id, max(stock // 2, 3))
    today = date.today()
    for br, scale in ((main_br, 1.0), (br2, 0.6)):
        for back in range(120, 0, -1):
            d = today - timedelta(days=back)
            items = []
            for sku, (v, pat, base) in variants.items():
                q = demand(pat, base * scale, d, rng)
                if q > 0:
                    items.append({"variant_id": v.id, "quantity": Decimal(q)})
            if items:  # history is imported-style: it does not move today's stock
                create_sale(ctx, branch_id=br.id, items=items, sale_date=d, source="demo_history", apply_stock=False)
    # one approved open PO and one fully received PO
    rice = variants["RICE-5"][0]
    for qty, receive in ((60, False), (40, True)):
        po = purchasing.create_po(ctx, branch_id=main_br.id, warehouse_id=main_wh.id, supplier_id=sups[0].id,
                                  items=[{"variant_id": rice.id, "ordered_quantity": qty}])
        purchasing.submit_po(ctx, po.id)
        purchasing.approve_po(ctx, po.id)
        if receive:
            purchasing.receive_goods(ctx, po.id, [{"purchase_order_item_id": po.items[0].id, "received_quantity": qty,
                                                   "accepted_quantity": qty}])
    db.commit()
    print(f"Demo organization created (id {org.id}).\nLogin: demo@stocksense.local / {password}")


if __name__ == "__main__":
    main()
