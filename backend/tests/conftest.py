import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-secret-key-test-secret-key-0123456789")
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ["ENVIRONMENT"] = "test"

import pytest
from fastapi.testclient import TestClient

from app.database import base as dbbase
from app.main import create_app


@pytest.fixture()
def client():
    dbbase.Base.metadata.drop_all(dbbase.engine)
    app = create_app()
    with TestClient(app) as c:
        yield c


class Biz:
    """Helper that registers a user + organization and exposes authed requests."""

    def __init__(self, client, email, org="Shop"):
        self.c = client
        pw = "a-long-password-1"
        assert client.post("/api/v1/auth/register", json={"email": email, "password": pw, "full_name": "T"}).status_code == 201
        tok = client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["access_token"]
        self.h = {"Authorization": f"Bearer {tok}"}
        self.org = client.post("/api/v1/organizations", json={"name": org}, headers=self.h).json()["id"]
        self.h["X-Organization-Id"] = self.org
        self.branch = client.get("/api/v1/branches", headers=self.h).json()[0]["id"]
        self.wh = client.get("/api/v1/warehouses", headers=self.h).json()[0]["id"]

    def post(self, url, json=None, **kw):
        return self.c.post("/api/v1" + url, json=json, headers={**self.h, **kw.get("headers", {})})

    def get(self, url):
        return self.c.get("/api/v1" + url, headers=self.h)

    def product(self, sku="P1", cost="10", price="20", opening="100"):
        p = self.post("/products", {"sku": sku, "name": sku, "cost_price": cost, "selling_price": price}).json()
        vid = p["variants"][0]["id"]
        if opening:
            assert self.post("/inventory/opening-balances", {"warehouse_id": self.wh, "variant_id": vid,
                                                             "quantity": opening}).status_code == 201
        return vid

    def on_hand(self, vid):
        items = self.get(f"/inventory/balances?warehouse_id={self.wh}").json()["items"]
        return float(next(i["on_hand_quantity"] for i in items if i["variant_id"] == vid))


@pytest.fixture()
def biz(client):
    return Biz(client, "owner@a.test")
