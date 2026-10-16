"""Durable service journal, compact idempotency outcomes and clock fencing.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("integration_journals", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("generation", sa.String(36), nullable=False), sa.Column("high_water_seq", sa.BigInteger(), nullable=False), sa.Column("oldest_seq", sa.BigInteger(), nullable=False))
    op.create_table("integration_changes", sa.Column("game_id", sa.String(80), sa.ForeignKey("games.id"), primary_key=True), sa.Column("journal_seq", sa.BigInteger(), primary_key=True), sa.Column("recorded_at_ms", sa.BigInteger(), nullable=False), sa.Column("record", sa.JSON(), nullable=False))
    op.create_index("ix_integration_changes_recorded_at_ms", "integration_changes", ["recorded_at_ms"])
    op.add_column("action_receipts", sa.Column("journal_cursor", sa.String(512), nullable=True))
    op.add_column("action_receipts", sa.Column("service_id", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("action_receipts", "service_id")
    op.drop_column("action_receipts", "journal_cursor")
    op.drop_index("ix_integration_changes_recorded_at_ms", table_name="integration_changes")
    op.drop_table("integration_changes")
    op.drop_table("integration_journals")
