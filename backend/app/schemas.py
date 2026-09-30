from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InboundMessageCreate(BaseModel):
    channel: str = Field(default="manual", max_length=32)
    external_id: str | None = Field(default=None, max_length=128)
    sender_name: str = Field(min_length=1, max_length=128)
    sender_contact: str | None = Field(default=None, max_length=256)
    content: str = Field(min_length=1, max_length=50_000)
    attachments: list[dict] = Field(default_factory=list)


class InquiryPatch(BaseModel):
    brand: str | None = None
    sku: str | None = None
    industry: str | None = None
    target_region: str | None = None
    purchase_price: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None
    moq: int | None = Field(default=None, gt=0)
    delivery_location: str | None = None
    trade_term: str | None = None
    payment_terms: str | None = None
    lead_time: str | None = None
    stock_type: str | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class InquiryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    brand: str | None
    sku: str | None
    industry: str | None
    target_region: str | None
    purchase_price: Decimal | None
    currency: str | None
    moq: int | None
    delivery_location: str | None
    trade_term: str
    payment_terms: str
    lead_time: str | None
    stock_type: str
    confidence: Decimal
    missing_fields: list[str]
    extraction_source: str
    created_at: datetime
    updated_at: datetime
    source_channel: str
    sender_name: str
    sender_contact: str | None
    source_content: str


class PricingPreviewRequest(BaseModel):
    scenario: Literal["A", "B"] = "A"
    output_currency: str = "CNY"
    fx_rate: Decimal | None = Field(default=None, gt=0)
    freight_per_unit: Decimal = Field(default=Decimal(0), ge=0)


class PricingPreview(BaseModel):
    purchase_price: Decimal
    source_currency: str
    output_currency: str
    fx_rate: Decimal
    converted_cost: Decimal
    margin_rate: Decimal
    freight_per_unit: Decimal
    unit_price: Decimal
    formula: str


class QuoteCreate(PricingPreviewRequest):
    pass


class QuoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    inquiry_id: int
    version: int
    scenario: str
    output_currency: str
    fx_rate: Decimal
    freight: Decimal
    margin_rate: Decimal
    unit_price: Decimal
    pricing_snapshot: dict
    downstream_snapshot: dict
    sensitive_scan_passed: bool
    created_by: str
    created_at: datetime


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    email: str | None = None
    channel: Literal["email", "wecom", "feishu"] = "email"
    channel_target: str | None = None
    brands: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    sku_patterns: list[str] = Field(default_factory=list)
    match_threshold: int = Field(default=45, ge=0, le=100)


class CustomerRead(BaseModel):
    id: int
    name: str
    email: str | None
    channel: str
    channel_target: str | None
    active: bool
    brands: list[str]
    industries: list[str]
    regions: list[str]
    sku_patterns: list[str]
    match_threshold: int


class CustomerMatchRead(BaseModel):
    customer: CustomerRead
    matched_brand: str | None
    score: int
    matched_fields: list[str] = Field(default_factory=list)
    reasons: dict[str, int] = Field(default_factory=dict)


class OpsTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    inquiry_id: int
    task_type: str
    status: str
    priority: str
    title: str
    detail: dict
    suggested_reply: str | None
    assignee: str | None
    resolved_by: str | None
    created_at: datetime
    resolved_at: datetime | None


class OpsTaskPatch(BaseModel):
    status: Literal["open", "in_progress", "resolved"]
    assignee: str | None = None


class DistributionCreate(BaseModel):
    customer_ids: list[int] = Field(min_length=1)
    confirm: bool = False


class DistributionRead(BaseModel):
    id: int
    quote_id: int
    customer_id: int
    customer_name: str
    channel: str
    status: str
    confirmed_by: str | None
    attempt_count: int
    max_attempts: int
    provider_message_id: str | None
    error_message: str | None
    sent_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: str
    action: str
    actor: str
    payload: dict
    previous_hash: str | None
    record_hash: str
    created_at: datetime


class DashboardRead(BaseModel):
    inquiries_total: int
    inquiries_need_attention: int
    quotes_total: int
    open_tasks: int
    pending_distributions: int


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class UserCreate(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]+$", min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=10, max_length=256)
    role: Literal["admin", "operator", "viewer"] = "viewer"


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str
    active: bool
    last_login_at: datetime | None
    created_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRead


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["admin", "operator", "viewer"] | None = None
    active: bool | None = None


class FxRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_date: date
    provider: str
    fetched_at: datetime


class DeliveryReceiptCreate(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=256)
    provider_event_id: str | None = Field(default=None, max_length=256)
    status: Literal["accepted", "sent", "delivered", "read", "failed", "bounced"]
    payload: dict = Field(default_factory=dict)


class OrderItemCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=128)
    brand: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class OrderCreate(BaseModel):
    customer_id: int
    quote_id: int | None = None
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    shipping_address: str | None = None
    notes: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_order_currency(cls, value: str) -> str:
        return value.upper()


class OrderPatch(BaseModel):
    status: Literal["draft", "confirmed", "cancelled", "completed"] | None = None
    shipping_address: str | None = None
    notes: str | None = None


class PaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    method: str = Field(min_length=1, max_length=32)
    status: Literal["pending", "paid", "failed", "refunded"] = "pending"
    transaction_reference: str | None = Field(default=None, max_length=256)


class ShipmentCreate(BaseModel):
    carrier: str = Field(min_length=1, max_length=128)
    tracking_number: str | None = Field(default=None, max_length=256)
    status: Literal["pending", "shipped", "in_transit", "delivered"] = "pending"


class ShipmentEventCreate(BaseModel):
    status: Literal["pending", "shipped", "in_transit", "delivered", "exception"]
    location: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=512)
    occurred_at: datetime | None = None


class InventoryAdjust(BaseModel):
    quantity: int
    movement_type: Literal["receipt", "adjustment", "damage", "release"] = "adjustment"
    note: str | None = Field(default=None, max_length=512)
    brand: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=256)
    warehouse: str | None = Field(default=None, max_length=128)
    reorder_point: int | None = Field(default=None, ge=0)


class InventoryRead(BaseModel):
    id: int
    sku: str
    brand: str | None
    name: str | None
    on_hand: int
    reserved: int
    available: int
    reorder_point: int
    warehouse: str | None
    updated_at: datetime


class OrderRead(BaseModel):
    id: int
    order_number: str
    customer_id: int
    customer_name: str
    quote_id: int | None
    status: str
    payment_status: str
    fulfillment_status: str
    currency: str
    total_amount: Decimal
    shipping_address: str | None
    notes: str | None
    items: list[dict]
    payments: list[dict]
    shipments: list[dict]
    created_at: datetime
    updated_at: datetime


class AuditVerifyRead(BaseModel):
    valid: bool
    checked: int
    broken_at_id: int | None = None
