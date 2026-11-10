"""Add field-level list rejection diagnostics.

Revision ID: 20260809_09
Revises: 20260808_08
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260809_09"
down_revision = "20260808_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("public_endpoint_runs")}
    if "rejection_summary_json" not in columns:
        op.add_column("public_endpoint_runs", sa.Column("rejection_summary_json", sa.Text(), nullable=False, server_default="{}"))
    if "rejection_samples_json" not in columns:
        op.add_column("public_endpoint_runs", sa.Column("rejection_samples_json", sa.Text(), nullable=False, server_default="[]"))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("public_endpoint_runs")}
    if "rejection_samples_json" in columns:
        op.drop_column("public_endpoint_runs", "rejection_samples_json")
    if "rejection_summary_json" in columns:
        op.drop_column("public_endpoint_runs", "rejection_summary_json")
