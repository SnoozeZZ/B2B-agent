"""Add durable Enterprise WeChat conversation archive cursors.

Revision ID: 0003_wecom_archive
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_wecom_archive"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wecom_archive_cursors",
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("last_seq", sa.String(length=32), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_id"),
    )
    op.create_table(
        "wecom_archive_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.String(length=32), nullable=False),
        sa.Column("msgid", sa.String(length=128), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "seq", name="uq_wecom_archive_failure_source_seq"
        ),
    )
    op.create_index(
        "ix_wecom_archive_failures_source_id",
        "wecom_archive_failures",
        ["source_id"],
    )
    op.create_index(
        "ix_wecom_archive_failures_msgid", "wecom_archive_failures", ["msgid"]
    )
    op.create_index(
        "ix_wecom_archive_failures_status", "wecom_archive_failures", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_wecom_archive_failures_status", table_name="wecom_archive_failures")
    op.drop_index("ix_wecom_archive_failures_msgid", table_name="wecom_archive_failures")
    op.drop_index("ix_wecom_archive_failures_source_id", table_name="wecom_archive_failures")
    op.drop_table("wecom_archive_failures")
    op.drop_table("wecom_archive_cursors")
