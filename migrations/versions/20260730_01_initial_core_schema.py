"""Create the Phase 1 collection fact model.

Revision ID: 20260730_01
Revises:
Create Date: 2026-07-30
"""
from __future__ import annotations

from alembic import op


revision = "20260730_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The initial schema is intentionally centralized in SQLAlchemy Core. Future
    # changes require a new revision; this revision must not be edited in place.
    from wechat_list_collector.infrastructure.db.schema import metadata, phase1_tables

    metadata.create_all(op.get_bind(), tables=phase1_tables)


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import metadata, phase1_tables

    metadata.drop_all(op.get_bind(), tables=phase1_tables)
