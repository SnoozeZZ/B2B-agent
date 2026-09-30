from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class InquiryStatus(str, Enum):
    RECEIVED = "received"
    NEED_INFO = "need_info"
    NEED_OPS = "need_ops"
    READY_TO_QUOTE = "ready_to_quote"
    QUOTED = "quoted"
    DISTRIBUTED = "distributed"
    CANCELLED = "cancelled"


class TaskStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class RawMessage(Base):
    __tablename__ = "raw_messages"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_raw_messages_dedup_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_name: Mapped[str] = mapped_column(String(128))
    sender_contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    dedup_key: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inquiry(Base):
    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_message_id: Mapped[int] = mapped_column(ForeignKey("raw_messages.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32), default=InquiryStatus.RECEIVED.value, index=True)
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_region: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    moq: Mapped[int | None] = mapped_column(nullable=True)
    delivery_location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trade_term: Mapped[str] = mapped_column(String(16), default="EXW")
    payment_terms: Mapped[str] = mapped_column(String(128), default="30%预付，70%发货前付清")
    lead_time: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stock_type: Mapped[str] = mapped_column(String(32), default="现货")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal(0))
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    extraction_source: Mapped[str] = mapped_column(String(32), default="rules")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    raw_message: Mapped[RawMessage] = relationship(lazy="joined")
    quotes: Mapped[list[Quote]] = relationship(back_populates="inquiry")


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("inquiry_id", "version", name="uq_quote_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.id"), index=True)
    version: Mapped[int] = mapped_column(default=1)
    scenario: Mapped[str] = mapped_column(String(16))
    output_currency: Mapped[str] = mapped_column(String(8))
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    freight: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    margin_rate: Mapped[Decimal] = mapped_column(Numeric(8, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    pricing_snapshot: Mapped[dict] = mapped_column(JSON)
    downstream_snapshot: Mapped[dict] = mapped_column(JSON)
    sensitive_scan_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    inquiry: Mapped[Inquiry] = relationship(back_populates="quotes", lazy="joined")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    channel_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    industries: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    sku_patterns: Mapped[list] = mapped_column(JSON, default=list)
    match_threshold: Mapped[int] = mapped_column(Integer, default=45)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    brand_needs: Mapped[list[CustomerBrandNeed]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class CustomerBrandNeed(Base):
    __tablename__ = "customer_brand_needs"
    __table_args__ = (
        UniqueConstraint("customer_id", "brand", name="uq_customer_brand_need"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    brand: Mapped[str] = mapped_column(String(128), index=True)
    priority: Mapped[int] = mapped_column(default=0)
    customer: Mapped[Customer] = relationship(back_populates="brand_needs")


class OpsTask(Base):
    __tablename__ = "ops_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    inquiry_id: Mapped[int] = mapped_column(ForeignKey("inquiries.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.OPEN.value, index=True)
    priority: Mapped[str] = mapped_column(String(16), default="normal")
    title: Mapped[str] = mapped_column(String(256))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Distribution(Base):
    __tablename__ = "distributions"
    __table_args__ = (
        UniqueConstraint("quote_id", "customer_id", name="uq_quote_customer_distribution"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending_confirmation", index=True)
    payload_snapshot: Mapped[dict] = mapped_column(JSON)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    provider_response: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer] = relationship(lazy="joined")
    quote: Mapped[Quote] = relationship(lazy="joined")
    receipts: Mapped[list[DeliveryReceipt]] = relationship(
        back_populates="distribution", cascade="all, delete-orphan"
    )


class DeliveryReceipt(Base):
    __tablename__ = "delivery_receipts"
    __table_args__ = (
        UniqueConstraint("channel", "provider_event_id", name="uq_delivery_receipt_event"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    distribution_id: Mapped[int] = mapped_column(ForeignKey("distributions.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    distribution: Mapped[Distribution] = relationship(back_populates="receipts")


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "rate_date", name="uq_fx_rate_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(8), index=True)
    quote_currency: Mapped[str] = mapped_column(String(8), index=True)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    rate_date: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WecomArchiveCursor(Base):
    """Durable cursor and lease for one Enterprise WeChat archive source."""

    __tablename__ = "wecom_archive_cursors"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_seq: Mapped[str] = mapped_column(String(32), default="0")
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WecomArchiveFailure(Base):
    """Persistent retry/dead-letter record for one poisoned archive message."""

    __tablename__ = "wecom_archive_failures"
    __table_args__ = (
        UniqueConstraint("source_id", "seq", name="uq_wecom_archive_failure_source_seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[str] = mapped_column(String(32))
    msgid: Mapped[str] = mapped_column(String(128), index=True)
    publickey_ver: Mapped[int | None] = mapped_column(Integer, nullable=True)
    encrypt_random_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypt_chat_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="retrying", index=True)
    last_error: Mapped[str] = mapped_column(Text)
    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32), default="viewer", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)
    warehouse: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    movements: Mapped[list[InventoryMovement]] = relationship(back_populates="item")


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    movement_type: Mapped[str] = mapped_column(String(32), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    item: Mapped[InventoryItem] = relationship(back_populates="movements")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    payment_status: Mapped[str] = mapped_column(String(32), default="unpaid", index=True)
    fulfillment_status: Mapped[str] = mapped_column(String(32), default="unfulfilled", index=True)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=Decimal(0))
    shipping_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer] = relationship(lazy="joined")
    quote: Mapped[Quote | None] = relationship(lazy="joined")
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    shipments: Mapped[list[Shipment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(128), index=True)
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 4))

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(8))
    method: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="payments")


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    carrier: Mapped[str] = mapped_column(String(128))
    tracking_number: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    inventory_committed: Mapped[bool] = mapped_column(Boolean, default=False)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[Order] = relationship(back_populates="shipments")
    events: Mapped[list[ShipmentEvent]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", lazy="selectin"
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"), index=True)
    status: Mapped[str] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    shipment: Mapped[Shipment] = relationship(back_populates="events")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
