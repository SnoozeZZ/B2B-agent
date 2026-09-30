"""Add delivery, FX, matching, accounts, commerce, and tamper-resistant audit data.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _columns(table_name: str) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in _inspector().get_columns(table_name)}


def _add_column(table_name: str, column: sa.Column[Any]) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _index_names(table_name: str) -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(table_name)}


def _ensure_index(
    name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if name not in _index_names(table_name):
        op.create_index(name, table_name, columns, unique=unique)


def _drop_index(name: str, table_name: str) -> None:
    if name in _index_names(table_name):
        op.drop_index(name, table_name=table_name)


def _set_non_nullable(table_name: str, specs: list[tuple[str, sa.TypeEngine[Any]]]) -> None:
    current = _columns(table_name)
    changes = [(name, type_) for name, type_ in specs if current[name].get("nullable", True)]
    if not changes:
        return
    with op.batch_alter_table(table_name) as batch_op:
        for name, type_ in changes:
            batch_op.alter_column(name, existing_type=type_, nullable=False)


def _has_unique_columns(table_name: str, columns: list[str]) -> bool:
    expected = tuple(columns)
    for constraint in _inspector().get_unique_constraints(table_name):
        if tuple(constraint.get("column_names") or ()) == expected:
            return True
    for index in _inspector().get_indexes(table_name):
        if index.get("unique") and tuple(index.get("column_names") or ()) == expected:
            return True
    return False


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _utc_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _audit_digest(row: sa.RowMapping, previous_hash: str | None) -> str:
    created_at = _utc_timestamp(row["created_at"])
    canonical = json.dumps(
        {
            "previous_hash": previous_hash,
            "entity_type": row["entity_type"],
            "entity_id": str(row["entity_id"]),
            "action": row["action"],
            "actor": row["actor"],
            "payload": _json_value(row["payload"]),
            "created_at": created_at.isoformat(timespec="microseconds"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _backfill_audit_chain() -> None:
    bind = op.get_bind()
    audit_logs = sa.table(
        "audit_logs",
        sa.column("id", sa.Integer()),
        sa.column("entity_type", sa.String()),
        sa.column("entity_id", sa.String()),
        sa.column("action", sa.String()),
        sa.column("actor", sa.String()),
        sa.column("payload", sa.JSON()),
        sa.column("previous_hash", sa.String()),
        sa.column("record_hash", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(audit_logs).order_by(audit_logs.c.id)).mappings().all()
    previous_hash: str | None = None
    for row in rows:
        record_hash = _audit_digest(row, previous_hash)
        bind.execute(
            audit_logs.update()
            .where(audit_logs.c.id == row["id"])
            .values(previous_hash=previous_hash, record_hash=record_hash)
        )
        previous_hash = record_hash


def _drop_audit_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite" or dialect == "mysql":
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete")


def _create_audit_triggers() -> None:
    dialect = op.get_bind().dialect.name
    _drop_audit_triggers()
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER audit_logs_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_logs_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only');
            END
            """
        )
    elif dialect == "mysql":
        op.execute(
            """
            CREATE TRIGGER audit_logs_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'
            """
        )
        op.execute(
            """
            CREATE TRIGGER audit_logs_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'
            """
        )


def _add_enterprise_columns() -> None:
    _add_column("inquiries", sa.Column("industry", sa.String(length=128), nullable=True))
    _add_column("inquiries", sa.Column("target_region", sa.String(length=128), nullable=True))
    _ensure_index("ix_inquiries_industry", "inquiries", ["industry"])
    _ensure_index("ix_inquiries_target_region", "inquiries", ["target_region"])

    _add_column("customers", sa.Column("industries", sa.JSON(), nullable=True))
    _add_column("customers", sa.Column("regions", sa.JSON(), nullable=True))
    _add_column("customers", sa.Column("sku_patterns", sa.JSON(), nullable=True))
    _add_column("customers", sa.Column("match_threshold", sa.Integer(), nullable=True))
    customers = sa.table(
        "customers",
        sa.column("industries", sa.JSON()),
        sa.column("regions", sa.JSON()),
        sa.column("sku_patterns", sa.JSON()),
        sa.column("match_threshold", sa.Integer()),
    )
    bind = op.get_bind()
    for name in ("industries", "regions", "sku_patterns"):
        bind.execute(
            customers.update().where(customers.c[name].is_(None)).values({name: []})
        )
    bind.execute(
        customers.update()
        .where(customers.c.match_threshold.is_(None))
        .values(match_threshold=45)
    )
    _set_non_nullable(
        "customers",
        [
            ("industries", sa.JSON()),
            ("regions", sa.JSON()),
            ("sku_patterns", sa.JSON()),
            ("match_threshold", sa.Integer()),
        ],
    )

    distribution_columns = (
        sa.Column("attempt_count", sa.Integer(), nullable=True),
        sa.Column("max_attempts", sa.Integer(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=256), nullable=True),
        sa.Column("provider_response", sa.JSON(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in distribution_columns:
        _add_column("distributions", column)
    distributions = sa.table(
        "distributions",
        sa.column("attempt_count", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("provider_response", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    bind.execute(
        distributions.update()
        .where(distributions.c.attempt_count.is_(None))
        .values(attempt_count=0)
    )
    bind.execute(
        distributions.update()
        .where(distributions.c.max_attempts.is_(None))
        .values(max_attempts=5)
    )
    bind.execute(
        distributions.update()
        .where(distributions.c.provider_response.is_(None))
        .values(provider_response={})
    )
    bind.execute(
        distributions.update()
        .where(distributions.c.updated_at.is_(None))
        .values(updated_at=distributions.c.created_at)
    )
    _set_non_nullable(
        "distributions",
        [
            ("attempt_count", sa.Integer()),
            ("max_attempts", sa.Integer()),
            ("provider_response", sa.JSON()),
            ("updated_at", sa.DateTime(timezone=True)),
        ],
    )
    _ensure_index(
        "ix_distributions_provider_message_id",
        "distributions",
        ["provider_message_id"],
    )

    _add_column("audit_logs", sa.Column("previous_hash", sa.String(length=64), nullable=True))
    _add_column("audit_logs", sa.Column("record_hash", sa.String(length=64), nullable=True))
    _backfill_audit_chain()
    _set_non_nullable("audit_logs", [("record_hash", sa.String(length=64))])
    if not _has_unique_columns("audit_logs", ["record_hash"]):
        with op.batch_alter_table("audit_logs") as batch_op:
            batch_op.create_unique_constraint(
                "uq_audit_logs_record_hash", ["record_hash"]
            )


def _create_delivery_receipts() -> None:
    if not _table_exists("delivery_receipts"):
        op.create_table(
            "delivery_receipts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("distribution_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=32), nullable=False),
            sa.Column("provider_event_id", sa.String(length=256), nullable=True),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["distribution_id"], ["distributions.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel", "provider_event_id", name="uq_delivery_receipt_event"
            ),
        )
    _ensure_index("ix_delivery_receipts_channel", "delivery_receipts", ["channel"])
    _ensure_index(
        "ix_delivery_receipts_distribution_id", "delivery_receipts", ["distribution_id"]
    )
    _ensure_index("ix_delivery_receipts_event_type", "delivery_receipts", ["event_type"])


def _create_fx_rates() -> None:
    if not _table_exists("fx_rates"):
        op.create_table(
            "fx_rates",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("base_currency", sa.String(length=8), nullable=False),
            sa.Column("quote_currency", sa.String(length=8), nullable=False),
            sa.Column("rate", sa.Numeric(precision=18, scale=8), nullable=False),
            sa.Column("rate_date", sa.Date(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "base_currency", "quote_currency", "rate_date", name="uq_fx_rate_day"
            ),
        )
    _ensure_index("ix_fx_rates_base_currency", "fx_rates", ["base_currency"])
    _ensure_index("ix_fx_rates_quote_currency", "fx_rates", ["quote_currency"])


def _create_users() -> None:
    if not _table_exists("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=128), nullable=False),
            sa.Column("password_hash", sa.String(length=512), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_users_role", "users", ["role"])
    _ensure_index("ix_users_username", "users", ["username"], unique=True)


def _create_commerce_tables() -> None:
    if not _table_exists("inventory_items"):
        op.create_table(
            "inventory_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("sku", sa.String(length=128), nullable=False),
            sa.Column("brand", sa.String(length=128), nullable=True),
            sa.Column("name", sa.String(length=256), nullable=True),
            sa.Column("on_hand", sa.Integer(), nullable=False),
            sa.Column("reserved", sa.Integer(), nullable=False),
            sa.Column("reorder_point", sa.Integer(), nullable=False),
            sa.Column("warehouse", sa.String(length=128), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_inventory_items_brand", "inventory_items", ["brand"])
    _ensure_index("ix_inventory_items_sku", "inventory_items", ["sku"], unique=True)

    if not _table_exists("orders"):
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_number", sa.String(length=64), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("quote_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("payment_status", sa.String(length=32), nullable=False),
            sa.Column("fulfillment_status", sa.String(length=32), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("total_amount", sa.Numeric(precision=18, scale=4), nullable=False),
            sa.Column("shipping_address", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
            sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_orders_customer_id", "orders", ["customer_id"])
    _ensure_index("ix_orders_fulfillment_status", "orders", ["fulfillment_status"])
    _ensure_index("ix_orders_order_number", "orders", ["order_number"], unique=True)
    _ensure_index("ix_orders_payment_status", "orders", ["payment_status"])
    _ensure_index("ix_orders_quote_id", "orders", ["quote_id"])
    _ensure_index("ix_orders_status", "orders", ["status"])

    if not _table_exists("order_items"):
        op.create_table(
            "order_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("sku", sa.String(length=128), nullable=False),
            sa.Column("brand", sa.String(length=128), nullable=True),
            sa.Column("description", sa.String(length=512), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=False),
            sa.Column("line_total", sa.Numeric(precision=18, scale=4), nullable=False),
            sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_order_items_order_id", "order_items", ["order_id"])
    _ensure_index("ix_order_items_sku", "order_items", ["sku"])

    if not _table_exists("payments"):
        op.create_table(
            "payments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("amount", sa.Numeric(precision=18, scale=4), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("method", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("transaction_reference", sa.String(length=256), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_payments_order_id", "payments", ["order_id"])
    _ensure_index("ix_payments_status", "payments", ["status"])

    if not _table_exists("shipments"):
        op.create_table(
            "shipments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("carrier", sa.String(length=128), nullable=False),
            sa.Column("tracking_number", sa.String(length=256), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("inventory_committed", sa.Boolean(), nullable=False),
            sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_shipments_order_id", "shipments", ["order_id"])
    _ensure_index("ix_shipments_status", "shipments", ["status"])
    _ensure_index("ix_shipments_tracking_number", "shipments", ["tracking_number"])

    if not _table_exists("shipment_events"):
        op.create_table(
            "shipment_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("shipment_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("location", sa.String(length=256), nullable=True),
            sa.Column("description", sa.String(length=512), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index("ix_shipment_events_shipment_id", "shipment_events", ["shipment_id"])

    if not _table_exists("inventory_movements"):
        op.create_table(
            "inventory_movements",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("inventory_item_id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=True),
            sa.Column("movement_type", sa.String(length=32), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("balance_after", sa.Integer(), nullable=False),
            sa.Column("note", sa.String(length=512), nullable=True),
            sa.Column("actor", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_items.id"]),
            sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    _ensure_index(
        "ix_inventory_movements_inventory_item_id",
        "inventory_movements",
        ["inventory_item_id"],
    )
    _ensure_index("ix_inventory_movements_movement_type", "inventory_movements", ["movement_type"])
    _ensure_index("ix_inventory_movements_order_id", "inventory_movements", ["order_id"])


def upgrade() -> None:
    _add_enterprise_columns()
    _create_delivery_receipts()
    _create_fx_rates()
    _create_users()
    _create_commerce_tables()
    _create_audit_triggers()


def downgrade() -> None:
    _drop_audit_triggers()

    for index in (
        "ix_inventory_movements_order_id",
        "ix_inventory_movements_movement_type",
        "ix_inventory_movements_inventory_item_id",
    ):
        _drop_index(index, "inventory_movements")
    if _table_exists("inventory_movements"):
        op.drop_table("inventory_movements")

    _drop_index("ix_shipment_events_shipment_id", "shipment_events")
    if _table_exists("shipment_events"):
        op.drop_table("shipment_events")

    for index in (
        "ix_shipments_tracking_number",
        "ix_shipments_status",
        "ix_shipments_order_id",
    ):
        _drop_index(index, "shipments")
    if _table_exists("shipments"):
        op.drop_table("shipments")

    _drop_index("ix_payments_status", "payments")
    _drop_index("ix_payments_order_id", "payments")
    if _table_exists("payments"):
        op.drop_table("payments")

    _drop_index("ix_order_items_sku", "order_items")
    _drop_index("ix_order_items_order_id", "order_items")
    if _table_exists("order_items"):
        op.drop_table("order_items")

    for index in (
        "ix_orders_status",
        "ix_orders_quote_id",
        "ix_orders_payment_status",
        "ix_orders_order_number",
        "ix_orders_fulfillment_status",
        "ix_orders_customer_id",
    ):
        _drop_index(index, "orders")
    if _table_exists("orders"):
        op.drop_table("orders")

    _drop_index("ix_inventory_items_sku", "inventory_items")
    _drop_index("ix_inventory_items_brand", "inventory_items")
    if _table_exists("inventory_items"):
        op.drop_table("inventory_items")

    _drop_index("ix_users_username", "users")
    _drop_index("ix_users_role", "users")
    if _table_exists("users"):
        op.drop_table("users")

    _drop_index("ix_fx_rates_quote_currency", "fx_rates")
    _drop_index("ix_fx_rates_base_currency", "fx_rates")
    if _table_exists("fx_rates"):
        op.drop_table("fx_rates")

    for index in (
        "ix_delivery_receipts_event_type",
        "ix_delivery_receipts_distribution_id",
        "ix_delivery_receipts_channel",
    ):
        _drop_index(index, "delivery_receipts")
    if _table_exists("delivery_receipts"):
        op.drop_table("delivery_receipts")

    if _table_exists("audit_logs"):
        unique_constraints = _inspector().get_unique_constraints("audit_logs")
        hash_constraint = next(
            (
                constraint
                for constraint in unique_constraints
                if tuple(constraint.get("column_names") or ()) == ("record_hash",)
                and constraint.get("name")
            ),
            None,
        )
        with op.batch_alter_table("audit_logs") as batch_op:
            if hash_constraint is not None:
                batch_op.drop_constraint(hash_constraint["name"], type_="unique")
            if "record_hash" in _columns("audit_logs"):
                batch_op.drop_column("record_hash")
            if "previous_hash" in _columns("audit_logs"):
                batch_op.drop_column("previous_hash")

    if _table_exists("distributions"):
        _drop_index(
            "ix_distributions_provider_message_id", "distributions"
        )
        with op.batch_alter_table("distributions") as batch_op:
            current = _columns("distributions")
            for name in (
                "updated_at",
                "delivered_at",
                "provider_response",
                "provider_message_id",
                "last_attempt_at",
                "next_attempt_at",
                "max_attempts",
                "attempt_count",
            ):
                if name in current:
                    batch_op.drop_column(name)

    if _table_exists("customers"):
        with op.batch_alter_table("customers") as batch_op:
            current = _columns("customers")
            for name in ("match_threshold", "sku_patterns", "regions", "industries"):
                if name in current:
                    batch_op.drop_column(name)

    if _table_exists("inquiries"):
        _drop_index("ix_inquiries_target_region", "inquiries")
        _drop_index("ix_inquiries_industry", "inquiries")
        with op.batch_alter_table("inquiries") as batch_op:
            current = _columns("inquiries")
            if "target_region" in current:
                batch_op.drop_column("target_region")
            if "industry" in current:
                batch_op.drop_column("industry")
