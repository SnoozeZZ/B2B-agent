"""Persist encrypted WeCom dead-letter envelopes for replay.

Revision ID: 0004_wecom_archive_replay
Revises: 0003_wecom_archive
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_wecom_archive_replay"
down_revision: str | None = "0003_wecom_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wecom_archive_failures") as batch_op:
        # Nullable keeps pre-0004 dead letters readable. They report a clear
        # non-replayable error because their original encrypted envelope was
        # not retained by the earlier schema.
        batch_op.add_column(sa.Column("publickey_ver", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("encrypt_random_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("encrypt_chat_msg", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("wecom_archive_failures") as batch_op:
        batch_op.drop_column("encrypt_chat_msg")
        batch_op.drop_column("encrypt_random_key")
        batch_op.drop_column("publickey_ver")
