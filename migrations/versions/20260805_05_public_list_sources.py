"""Add public list-only source ingestion.

Revision ID: 20260805_05
Revises: 20260731_04
Create Date: 2026-08-05
"""
from __future__ import annotations

from alembic import op


revision = "20260805_05"
down_revision = "20260731_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase5_tables

    phase5_tables[0].metadata.create_all(op.get_bind(), tables=phase5_tables)


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase5_tables

    phase5_tables[0].metadata.drop_all(op.get_bind(), tables=tuple(reversed(phase5_tables)))
