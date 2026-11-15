from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    email: str
    password: str
    full_name: str = Field(min_length=1, max_length=200)


class LoginIn(BaseModel):
    email: str
    password: str


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industry: str = "general_retail"
    currency: str = Field(default="INR", min_length=3, max_length=3)
    timezone: str = "Asia/Kolkata"
    branch_name: str = "Main Store"
    branch_code: str = "MAIN"


class BranchIn(BaseModel):
    name: str
    code: str
    address: str | None = None


class WarehouseIn(BaseModel):
    branch_id: uuid.UUID
    name: str
    code: str


class InviteIn(BaseModel):
    email: str
    role: str
    branch_ids: list[str] = []


class VariantIn(BaseModel):
    sku: str
    barcode: str | None = None
    attributes: dict = {}
    cost_price: Decimal | None = None
    selling_price: Decimal | None = None


class ProductIn(BaseModel):
    sku: str
    name: str
    description: str | None = None
    category: str | None = None
    unit: str = "unit"
    cost_price: Decimal = Decimal("0")
    selling_price: Decimal = Decimal("0")
    tracking_mode: str = "standard"
    shelf_life_days: int | None = None
    attributes: dict = {}
    variants: list[VariantIn] | None = None


class OpeningIn(BaseModel):
    warehouse_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: Decimal


class AdjustIn(BaseModel):
    warehouse_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: Decimal
    reason: str
    note: str | None = None


class TransferItem(BaseModel):
    variant_id: uuid.UUID
    quantity: Decimal


class TransferIn(BaseModel):
    source_warehouse_id: uuid.UUID
    destination_warehouse_id: uuid.UUID
    items: list[TransferItem]


class SaleLine(BaseModel):
    variant_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal | None = None
    discount: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")


class SaleIn(BaseModel):
    branch_id: uuid.UUID
    items: list[SaleLine]
    sale_date: date | None = None
    status: str = "completed"
    source: str = "manual"
    warehouse_id: uuid.UUID | None = None


class ReturnLine(BaseModel):
    sale_item_id: uuid.UUID
    quantity: Decimal
    restock: bool = False


class ReturnIn(BaseModel):
    items: list[ReturnLine]
    reason: str | None = None


class SupplierIn(BaseModel):
    name: str
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    notes: str | None = None


class SupplierProductIn(BaseModel):
    variant_id: uuid.UUID
    unit_cost: Decimal
    lead_time_days: int = 7
    minimum_order_quantity: Decimal = Decimal("0")
    pack_size: Decimal = Decimal("1")
    supplier_sku: str | None = None


class POLine(BaseModel):
    variant_id: uuid.UUID
    ordered_quantity: Decimal
    unit_cost: Decimal | None = None


class POIn(BaseModel):
    branch_id: uuid.UUID
    warehouse_id: uuid.UUID
    supplier_id: uuid.UUID
    items: list[POLine]
    expected_date: date | None = None


class ReceiptLine(BaseModel):
    purchase_order_item_id: uuid.UUID
    received_quantity: Decimal
    accepted_quantity: Decimal
    rejected_quantity: Decimal = Decimal("0")


class ReceiptIn(BaseModel):
    purchase_order_id: uuid.UUID
    items: list[ReceiptLine]


class ForecastIn(BaseModel):
    branch_id: uuid.UUID
    horizon_days: int = 30
    granularity: str = "daily"
    variant_ids: list[uuid.UUID] | None = None
    history_days: int = 180


class GenerateRecsIn(BaseModel):
    branch_id: uuid.UUID
    service_level: float = 0.95
    review_period_days: int = 7
    budget: float | None = None


class ConvertIn(BaseModel):
    recommendation_ids: list[uuid.UUID]


class CsvIn(BaseModel):
    csv: str
    commit: bool = False
    mapping: dict[str, str] | None = None


class CountStartIn(BaseModel):
    warehouse_id: uuid.UUID
    variant_ids: list[uuid.UUID] | None = None
    note: str | None = None


class CountEntryIn(BaseModel):
    counts: dict[uuid.UUID, Decimal]
