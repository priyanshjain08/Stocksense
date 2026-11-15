"""SQLAlchemy 2.x models. Every business record carries organization_id (tenant scope).

Money = Numeric(14,2), quantities = Numeric(14,3) (fractional units allowed).
stock_movements is append-only: application code never updates or deletes rows.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
                        UniqueConstraint, Uuid)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(14, 2)
Qty = Numeric(14, 3)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


def _org() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, ForeignKey("organizations.id"), nullable=False, index=True)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DocumentCounter(Base):
    """Per-organization gap-tolerant sequence for human-readable document numbers (locked on increment)."""
    __tablename__ = "document_counters"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- access
class Organization(Base, Timestamps):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _id()
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(50), default="general_retail")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="active")
    is_demo: Mapped[bool] = mapped_column(default=False)


class User(Base, Timestamps):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _id()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # bump to invalidate sessions


class Membership(Base, Timestamps):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(30))
    branch_ids: Mapped[list] = mapped_column(JSONType, default=list)  # [] = no branch restriction for global roles
    status: Mapped[str] = mapped_column(String(20), default="active")


class Invitation(Base, Timestamps):
    __tablename__ = "invitations"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30))
    branch_ids: Mapped[list] = mapped_column(JSONType, default=list)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Branch(Base, Timestamps):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")


class Warehouse(Base, Timestamps):
    __tablename__ = "warehouses"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="active")


# --------------------------------------------------------------- catalog
class Category(Base, Timestamps):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id"), nullable=True)


class Product(Base, Timestamps):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("organization_id", "sku"),
                      CheckConstraint("cost_price >= 0 AND selling_price >= 0", name="ck_product_prices"))
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    sku: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    unit: Mapped[str] = mapped_column(String(20), default="unit")
    cost_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    selling_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tracking_mode: Mapped[str] = mapped_column(String(20), default="standard")  # standard|batch|serial
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)
    variants: Mapped[list["Variant"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Variant(Base, Timestamps):
    __tablename__ = "product_variants"
    __table_args__ = (UniqueConstraint("organization_id", "sku"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    product_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("products.id"), index=True)
    sku: Mapped[str] = mapped_column(String(64))
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)  # size, colour...
    cost_price_override: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    selling_price_override: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    product: Mapped[Product] = relationship(back_populates="variants")

    @property
    def cost(self) -> Decimal:
        return self.cost_price_override if self.cost_price_override is not None else self.product.cost_price

    @property
    def price(self) -> Decimal:
        return self.selling_price_override if self.selling_price_override is not None else self.product.selling_price


# ------------------------------------------------------------- inventory
class InventoryBalance(Base):
    """Derived from stock_movements; only inventory.service may change it, always with a movement."""
    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint("organization_id", "warehouse_id", "variant_id"),
        CheckConstraint("on_hand_quantity >= 0", name="ck_balance_on_hand_nonneg"),
        CheckConstraint("unavailable_quantity >= 0", name="ck_balance_unavail_nonneg"),
        CheckConstraint("reserved_quantity >= 0", name="ck_balance_reserved_nonneg"),
        Index("ix_balance_lookup", "organization_id", "variant_id"),
    )
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"), index=True)
    on_hand_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))        # sellable
    unavailable_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))    # damaged/quarantined
    reserved_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StockMovement(Base):
    """Immutable ledger. Corrections are new rows (type 'reversal'/'adjustment'), never edits."""
    __tablename__ = "stock_movements"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_movement_idempotency"),
        CheckConstraint("quantity_delta <> 0", name="ck_movement_nonzero"),
        Index("ix_movement_wh_var_time", "organization_id", "warehouse_id", "variant_id", "occurred_at"),
    )
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"), index=True)
    movement_type: Mapped[str] = mapped_column(String(30))
    bucket: Mapped[str] = mapped_column(String(12), default="sellable")  # sellable | unavailable
    quantity_delta: Mapped[Decimal] = mapped_column(Qty)
    balance_after: Mapped[Decimal] = mapped_column(Qty)
    reference_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)


class StockTransfer(Base, Timestamps):
    __tablename__ = "stock_transfers"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    source_warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    destination_warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    status: Mapped[str] = mapped_column(String(20), default="completed")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    items: Mapped[list] = mapped_column(JSONType, default=list)  # [{variant_id, quantity}]


class StockCount(Base, Timestamps):
    __tablename__ = "stock_counts"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"), index=True)
    status: Mapped[str] = mapped_column(String(15), default="open")  # open|submitted|approved|cancelled
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    lines: Mapped[list["StockCountLine"]] = relationship(cascade="all, delete-orphan")


class StockCountLine(Base):
    __tablename__ = "stock_count_lines"
    __table_args__ = (UniqueConstraint("stock_count_id", "variant_id"),)
    id: Mapped[uuid.UUID] = _id()
    stock_count_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stock_counts.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    expected_quantity: Mapped[Decimal] = mapped_column(Qty)          # snapshot at count start
    counted_quantity: Mapped[Decimal | None] = mapped_column(Qty, nullable=True)


# ----------------------------------------------------------------- sales
class Sale(Base, Timestamps):
    __tablename__ = "sales"
    __table_args__ = (UniqueConstraint("organization_id", "sale_number"),
                      UniqueConstraint("organization_id", "idempotency_key", name="uq_sale_idempotency"),
                      Index("ix_sale_branch_date", "organization_id", "branch_id", "sale_date"))
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    sale_number: Mapped[str] = mapped_column(String(40))
    sale_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="completed")  # draft|completed|cancelled
    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    discount_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    grand_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    items: Mapped[list["SaleItem"]] = relationship(cascade="all, delete-orphan")


class SaleItem(Base):
    __tablename__ = "sale_items"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_sale_item_qty"),)
    id: Mapped[uuid.UUID] = _id()
    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id"), index=True)
    organization_id: Mapped[uuid.UUID] = _org()
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"), index=True)
    quantity: Mapped[Decimal] = mapped_column(Qty)
    unit_price: Mapped[Decimal] = mapped_column(Money)
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Money)
    cost_snapshot: Mapped[Decimal] = mapped_column(Money)  # unit cost at time of sale


class SalesReturn(Base, Timestamps):
    __tablename__ = "sales_returns"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    sale_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales.id"), index=True)
    return_number: Mapped[str] = mapped_column(String(40))
    return_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    items: Mapped[list["SalesReturnItem"]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key", name="uq_return_idempotency"),)


class SalesReturnItem(Base):
    __tablename__ = "sales_return_items"
    id: Mapped[uuid.UUID] = _id()
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sales_returns.id"), index=True)
    sale_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sale_items.id"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    quantity: Mapped[Decimal] = mapped_column(Qty)
    restock: Mapped[bool] = mapped_column(default=False)  # True => sellable, back to on-hand
    refund_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))


# ------------------------------------------------------------ purchasing
class Supplier(Base, Timestamps):
    __tablename__ = "suppliers"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")


class SupplierProduct(Base, Timestamps):
    __tablename__ = "supplier_products"
    __table_args__ = (UniqueConstraint("organization_id", "supplier_id", "variant_id"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"), index=True)
    supplier_sku: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    minimum_order_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    pack_size: Mapped[Decimal] = mapped_column(Qty, default=Decimal("1"))
    is_preferred: Mapped[bool] = mapped_column(default=True)


class PurchaseOrder(Base, Timestamps):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("organization_id", "order_number"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), index=True)
    order_number: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(25), default="draft")
    order_date: Mapped[date] = mapped_column(Date)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    items: Mapped[list["PurchaseOrderItem"]] = relationship(cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    __table_args__ = (CheckConstraint("ordered_quantity > 0", name="ck_po_item_qty"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"), index=True)
    ordered_quantity: Mapped[Decimal] = mapped_column(Qty)
    received_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))   # accepted into stock
    rejected_quantity: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Money)
    line_total: Mapped[Decimal] = mapped_column(Money)


class GoodsReceipt(Base, Timestamps):
    __tablename__ = "goods_receipts"
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key", name="uq_receipt_idempotency"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    receipt_number: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    items: Mapped[list["GoodsReceiptItem"]] = relationship(cascade="all, delete-orphan")


class GoodsReceiptItem(Base):
    __tablename__ = "goods_receipt_items"
    id: Mapped[uuid.UUID] = _id()
    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goods_receipts.id"), index=True)
    purchase_order_item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("purchase_order_items.id"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    received_quantity: Mapped[Decimal] = mapped_column(Qty)
    accepted_quantity: Mapped[Decimal] = mapped_column(Qty)
    rejected_quantity: Mapped[Decimal] = mapped_column(Qty)


# ----------------------------------------------------------- intelligence
class ForecastRun(Base, Timestamps):
    __tablename__ = "forecast_runs"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), index=True)
    horizon_days: Mapped[int] = mapped_column(Integer)
    granularity: Mapped[str] = mapped_column(String(10), default="daily")
    status: Mapped[str] = mapped_column(String(15), default="queued")  # queued|running|completed|failed
    model_version: Mapped[str] = mapped_column(String(30), default="1.0")
    configuration: Mapped[dict] = mapped_column(JSONType, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ForecastResult(Base):
    __tablename__ = "forecast_results"
    __table_args__ = (Index("ix_fres_run_variant", "forecast_run_id", "variant_id"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    forecast_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("forecast_runs.id"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    forecast_period: Mapped[date] = mapped_column(Date)  # day, or week start when weekly
    predicted_quantity: Mapped[Decimal] = mapped_column(Qty)
    lower_bound: Mapped[Decimal] = mapped_column(Qty)
    upper_bound: Mapped[Decimal] = mapped_column(Qty)
    model_name: Mapped[str] = mapped_column(String(40))
    confidence_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)


class Recommendation(Base, Timestamps):
    __tablename__ = "replenishment_recommendations"
    __table_args__ = (Index("ix_rec_branch_status", "organization_id", "branch_id", "status"),)
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID] = _org()
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"))
    warehouse_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("warehouses.id"))
    variant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("product_variants.id"))
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    recommended_quantity: Mapped[Decimal] = mapped_column(Qty)
    estimated_cost: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    urgency: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)  # all calculation inputs/outputs, warnings
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = _id()
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
