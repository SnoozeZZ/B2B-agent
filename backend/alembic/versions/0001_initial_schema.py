"""Create the original inquiry, quote, customer, distribution, and audit schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("sender_name", sa.String(length=128), nullable=False),
        sa.Column("sender_contact", sa.String(length=256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key", name="uq_raw_messages_dedup_key"),
    )
    op.create_index("ix_raw_messages_channel", "raw_messages", ["channel"])

    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("channel_target", sa.String(length=512), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "inquiries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("raw_message_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=True),
        sa.Column("sku", sa.String(length=128), nullable=True),
        sa.Column("purchase_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("delivery_location", sa.String(length=256), nullable=True),
        sa.Column("trade_term", sa.String(length=16), nullable=False),
        sa.Column("payment_terms", sa.String(length=128), nullable=False),
        sa.Column("lead_time", sa.String(length=128), nullable=True),
        sa.Column("stock_type", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.Column("extraction_source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["raw_message_id"], ["raw_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("raw_message_id"),
    )
    op.create_index("ix_inquiries_brand", "inquiries", ["brand"])
    op.create_index("ix_inquiries_sku", "inquiries", ["sku"])
    op.create_index("ix_inquiries_status", "inquiries", ["status"])

    op.create_table(
        "customer_brand_needs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(length=128), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", "brand", name="uq_customer_brand_need"),
    )
    op.create_index(
        "ix_customer_brand_needs_brand", "customer_brand_needs", ["brand"]
    )
    op.create_index(
        "ix_customer_brand_needs_customer_id", "customer_brand_needs", ["customer_id"]
    )

    op.create_table(
        "quotes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inquiry_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scenario", sa.String(length=16), nullable=False),
        sa.Column("output_currency", sa.String(length=8), nullable=False),
        sa.Column("fx_rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("freight", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("margin_rate", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("pricing_snapshot", sa.JSON(), nullable=False),
        sa.Column("downstream_snapshot", sa.JSON(), nullable=False),
        sa.Column("sensitive_scan_passed", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inquiry_id", "version", name="uq_quote_version"),
    )
    op.create_index("ix_quotes_inquiry_id", "quotes", ["inquiry_id"])

    op.create_table(
        "ops_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inquiry_id", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("suggested_reply", sa.Text(), nullable=True),
        sa.Column("assignee", sa.String(length=128), nullable=True),
        sa.Column("resolved_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["inquiry_id"], ["inquiries.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ops_tasks_inquiry_id", "ops_tasks", ["inquiry_id"])
    op.create_index("ix_ops_tasks_status", "ops_tasks", ["status"])
    op.create_index("ix_ops_tasks_task_type", "ops_tasks", ["task_type"])

    op.create_table(
        "distributions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload_snapshot", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=128), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "quote_id", "customer_id", name="uq_quote_customer_distribution"
        ),
    )
    op.create_index("ix_distributions_customer_id", "distributions", ["customer_id"])
    op.create_index("ix_distributions_quote_id", "distributions", ["quote_id"])
    op.create_index("ix_distributions_status", "distributions", ["status"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"])
    op.create_index("ix_audit_logs_entity_type", "audit_logs", ["entity_type"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_entity_type", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_distributions_status", table_name="distributions")
    op.drop_index("ix_distributions_quote_id", table_name="distributions")
    op.drop_index("ix_distributions_customer_id", table_name="distributions")
    op.drop_table("distributions")

    op.drop_index("ix_ops_tasks_task_type", table_name="ops_tasks")
    op.drop_index("ix_ops_tasks_status", table_name="ops_tasks")
    op.drop_index("ix_ops_tasks_inquiry_id", table_name="ops_tasks")
    op.drop_table("ops_tasks")

    op.drop_index("ix_quotes_inquiry_id", table_name="quotes")
    op.drop_table("quotes")

    op.drop_index("ix_customer_brand_needs_customer_id", table_name="customer_brand_needs")
    op.drop_index("ix_customer_brand_needs_brand", table_name="customer_brand_needs")
    op.drop_table("customer_brand_needs")

    op.drop_index("ix_inquiries_status", table_name="inquiries")
    op.drop_index("ix_inquiries_sku", table_name="inquiries")
    op.drop_index("ix_inquiries_brand", table_name="inquiries")
    op.drop_table("inquiries")

    op.drop_table("customers")
    op.drop_index("ix_raw_messages_channel", table_name="raw_messages")
    op.drop_table("raw_messages")
