"""Add command-item, job queue, request ledger, and control state.

Revision ID: 20260730_02
Revises: 20260730_01
Create Date: 2026-07-30
"""
from __future__ import annotations

from alembic import op


revision = "20260730_02"
down_revision = "20260730_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase2_tables, request_control_state

    bind = op.get_bind()
    request_control_state.metadata.create_all(bind, tables=phase2_tables)
    bind.execute(request_control_state.insert().values(id=1, rolling_window_limit=60, consecutive_rate_limit_count=0, version=0))


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase2_tables, request_control_state

    request_control_state.metadata.drop_all(op.get_bind(), tables=tuple(reversed(phase2_tables)))
