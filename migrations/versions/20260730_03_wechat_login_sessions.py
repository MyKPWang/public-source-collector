"""Add metadata-only WeChat QR login sessions.

Revision ID: 20260730_03
Revises: 20260730_02
Create Date: 2026-07-30
"""
from __future__ import annotations

from alembic import op


revision = "20260730_03"
down_revision = "20260730_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase3_tables

    phase3_tables[0].metadata.create_all(op.get_bind(), tables=phase3_tables)


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase3_tables

    phase3_tables[0].metadata.drop_all(op.get_bind(), tables=phase3_tables)
