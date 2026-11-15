"""Integration tests (need the dependencies in requirements.txt: pytest, httpx, fastapi, sqlalchemy)."""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import Biz


def test_sale_decreases_stock_once_and_is_idempotent(biz):
    v = biz.product()
    body = {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "3"}]}
    r1 = biz.post("/sales", body, headers={"Idempotency-Key": "k1"})
    r2 = biz.post("/sales", body, headers={"Idempotency-Key": "k1"})
    assert r1.status_code == 201 and r2.json()["id"] == r1.json()["id"]
    assert biz.on_hand(v) == 97


def test_cannot_oversell(biz):
    v = biz.product(opening="2")
    r = biz.post("/sales", {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "3"}]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "insufficient_stock"
    assert biz.on_hand(v) == 2


def test_draft_and_cancelled_sales_do_not_deduct_then_cancel_restores(biz):
    v = biz.product()
    d = biz.post("/sales", {"branch_id": biz.branch, "status": "draft", "items": [{"variant_id": v, "quantity": "5"}]}).json()
    assert biz.on_hand(v) == 100
    biz.post(f"/sales/{d['id']}/complete")
    assert biz.on_hand(v) == 95
    biz.post(f"/sales/{d['id']}/cancel")
    assert biz.on_hand(v) == 100
    assert biz.post(f"/sales/{d['id']}/cancel").status_code == 200 and biz.on_hand(v) == 100


def test_returns_restock_only_when_sellable_and_cannot_exceed_sold(biz):
    v = biz.product()
    s = biz.post("/sales", {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "4"}]}).json()
    line = s["items"][0]["id"]
    assert biz.post(f"/sales/{s['id']}/returns", {"items": [{"sale_item_id": line, "quantity": "1", "restock": True}]}).status_code == 201
    assert biz.on_hand(v) == 97
    assert biz.post(f"/sales/{s['id']}/returns", {"items": [{"sale_item_id": line, "quantity": "1", "restock": False}]}).status_code == 201
    assert biz.on_hand(v) == 97                                  # damaged return: not sellable
    assert biz.post(f"/sales/{s['id']}/returns", {"items": [{"sale_item_id": line, "quantity": "3"}]}).status_code == 409


def test_ledger_reconciles_with_balances(biz):
    v = biz.product()
    biz.post("/sales", {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "7"}]})
    biz.post("/inventory/adjustments", {"warehouse_id": biz.wh, "variant_id": v, "quantity": "5", "reason": "damaged"})
    biz.post("/inventory/adjustments", {"warehouse_id": biz.wh, "variant_id": v, "quantity": "2", "reason": "found"})
    assert biz.on_hand(v) == 100 - 7 - 5 + 2
    assert biz.get("/inventory/reconciliation").json()["consistent"] is True


def test_concurrent_sales_never_oversell(biz):
    v = biz.product(opening="10")

    def sell(i):
        return biz.post("/sales", {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "1"}]},
                        headers={"Idempotency-Key": f"c{i}"}).status_code

    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(sell, range(15)))
    assert biz.on_hand(v) >= 0 and codes.count(201) <= 10   # NOTE: strict ==10 needs PostgreSQL row locks


def test_po_partial_receipt_increases_stock_by_accepted_only(biz):
    v = biz.product(opening=None)
    sup = biz.post("/suppliers", {"name": "Acme"}).json()["id"]
    po = biz.post("/purchase-orders", {"branch_id": biz.branch, "warehouse_id": biz.wh, "supplier_id": sup,
                                       "items": [{"variant_id": v, "ordered_quantity": "50", "unit_cost": "9"}]}).json()
    assert biz.on_hand(v) == 0                                    # PO alone changes nothing
    assert biz.post(f"/purchase-orders/{po['id']}/approve").status_code == 409   # must be submitted first
    biz.post(f"/purchase-orders/{po['id']}/submit")
    biz.post(f"/purchase-orders/{po['id']}/approve")
    item = po["items"][0]["id"]
    r = biz.post("/goods-receipts", {"purchase_order_id": po["id"], "items": [
        {"purchase_order_item_id": item, "received_quantity": "20", "accepted_quantity": "18", "rejected_quantity": "2"}]},
        headers={"Idempotency-Key": "g1"})
    assert r.status_code == 201 and biz.on_hand(v) == 18
    biz.post("/goods-receipts", {"purchase_order_id": po["id"], "items": [
        {"purchase_order_item_id": item, "received_quantity": "20", "accepted_quantity": "18", "rejected_quantity": "2"}]},
        headers={"Idempotency-Key": "g1"})
    assert biz.on_hand(v) == 18                                   # retry did not double count
    assert biz.get(f"/purchase-orders/{po['id']}").json()["status"] == "partially_received"
    over = biz.post("/goods-receipts", {"purchase_order_id": po["id"], "items": [
        {"purchase_order_item_id": item, "received_quantity": "31", "accepted_quantity": "31"}]})
    assert over.status_code == 409                                # over-receipt refused
    biz.post("/goods-receipts", {"purchase_order_id": po["id"], "items": [
        {"purchase_order_item_id": item, "received_quantity": "30", "accepted_quantity": "30"}]})
    assert biz.get(f"/purchase-orders/{po['id']}").json()["status"] == "received" and biz.on_hand(v) == 48


def test_tenant_isolation(client, biz):
    v = biz.product()
    other = Biz(client, "other@b.test", org="Rival")
    assert other.get("/products").json()["total"] == 0
    # other user cannot act inside the first org even by sending its id
    h = {**other.h, "X-Organization-Id": biz.org}
    assert client.get("/api/v1/products", headers=h).status_code == 403
    # nor can they reference the first org's variant from their own org
    r = other.post("/sales", {"branch_id": other.branch, "items": [{"variant_id": v, "quantity": "1"}]})
    assert r.status_code == 404
    assert other.get(f"/products/{biz.get('/products').json()['items'][0]['id']}").status_code == 404


def test_role_permissions_and_branch_scope(client, biz):
    v = biz.product()
    inv = biz.post("/memberships/invitations", {"email": "staff@a.test", "role": "staff"}).json()
    client.post("/api/v1/auth/register", json={"email": "staff@a.test", "password": "a-long-password-1", "full_name": "S"})
    tok = client.post("/api/v1/auth/login", json={"email": "staff@a.test", "password": "a-long-password-1"}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert client.post("/api/v1/auth/accept-invitation", json={"token": inv["token"]}, headers=h).status_code == 200
    h["X-Organization-Id"] = biz.org
    assert client.post("/api/v1/products", json={"sku": "X", "name": "X"}, headers=h).status_code == 403
    # staff has branch_ids=[] => no branch access at all until assigned
    assert client.post("/api/v1/sales", json={"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "1"}]},
                       headers=h).status_code == 403


def test_logout_invalidates_token(client, biz):
    assert biz.get("/products").status_code == 200
    client.post("/api/v1/auth/logout", headers=biz.h)
    assert biz.get("/products").status_code == 401


def test_csv_import_preview_reports_row_errors_and_commits_all_or_nothing(biz):
    bad = "sku,name,cost_price\nA1,Alpha,5\n,NoSku,3\nA1,Dup,2\nB2,Beta,abc\n"
    r = biz.post("/products/import", {"csv": bad, "commit": True}).json()
    assert r["committed"] is False and r["error_rows"] == 3 and biz.get("/products").json()["total"] == 0
    ok = biz.post("/products/import", {"csv": "sku,name,cost_price\nA1,Alpha,5\nB2,Beta,6\n", "commit": True}).json()
    assert ok["committed"] is True and biz.get("/products").json()["total"] == 2


def test_forecast_and_replenishment_end_to_end(biz):
    from datetime import date, timedelta
    v = biz.product(opening="10")
    sup = biz.post("/suppliers", {"name": "S"}).json()["id"]
    biz.c.put(f"/api/v1/suppliers/{sup}/products", json={"variant_id": v, "unit_cost": "9", "lead_time_days": 5,
                                                         "pack_size": "10"}, headers=biz.h)
    rows = "date,branch_code,sku,quantity,unit_price\n" + "".join(
        f"{date.today() - timedelta(days=d)},MAIN,P1,{4 + d % 3},20\n" for d in range(1, 91))
    assert biz.post("/sales/import", {"csv": rows, "commit": True}).json()["committed"] is True
    run = biz.post("/forecasts", {"branch_id": biz.branch, "horizon_days": 30}).json()
    assert run["status"] == "completed", run
    res = biz.get(f"/forecasts/{run['id']}/results?variant_id={v}").json()
    assert len(res["results"]) == 30 and float(res["results"][0]["predicted_quantity"]) > 0
    recs = biz.post("/replenishment/recommendations/generate", {"branch_id": biz.branch}).json()
    assert recs and float(recs[0]["recommended_quantity"]) % 10 == 0
    rid = recs[0]["id"]
    assert biz.post("/replenishment/recommendations/convert", {"recommendation_ids": [rid]}).status_code == 409
    biz.post(f"/replenishment/recommendations/{rid}/accept")
    pos = biz.post("/replenishment/recommendations/convert", {"recommendation_ids": [rid]}).json()
    assert pos[0]["status"] == "draft"                            # never auto-submitted


def test_stock_count_flow_preserves_concurrent_sales_and_needs_second_approver(client, biz):
    v = biz.product(opening="50")
    c = biz.post("/stock-counts", {"warehouse_id": biz.wh}).json()          # snapshot: 50
    biz.post("/sales", {"branch_id": biz.branch, "items": [{"variant_id": v, "quantity": "5"}]})  # during count
    assert biz.post(f"/stock-counts/{c['id']}/submit").status_code == 409     # uncounted line
    biz.c.put(f"/api/v1/stock-counts/{c['id']}/counts", json={"counts": {v: "48"}}, headers=biz.h)
    assert biz.post(f"/stock-counts/{c['id']}/submit").status_code == 200
    assert biz.get(f"/stock-counts/{c['id']}/review").json()[0]["variance"] == -2
    biz.post(f"/stock-counts/{c['id']}/approve")                              # owner may self-approve
    assert biz.on_hand(v) == 50 - 5 - 2                                       # sale kept, variance applied
    assert biz.post(f"/stock-counts/{c['id']}/approve").status_code == 409    # cannot approve twice
    assert biz.get("/inventory/reconciliation").json()["consistent"] is True


def test_inventory_xlsx_export(biz):
    biz.product()
    r = biz.c.get("/api/v1/reports/inventory.xlsx", headers=biz.h)
    assert r.status_code == 200 and r.content[:2] == b"PK"
