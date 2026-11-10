"""Add durable, encrypted candidate storage for account-name searches.

Revision ID: 20260731_04
Revises: 20260730_03
Create Date: 2026-07-31
"""
from __future__ import annotations

from alembic import op


revision = "20260731_04"
down_revision = "20260730_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase4_tables

    phase4_tables[0].metadata.create_all(op.get_bind(), tables=phase4_tables)


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase4_tables

    phase4_tables[0].metadata.drop_all(op.get_bind(), tables=phase4_tables)
