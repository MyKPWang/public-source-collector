"""Add source-scoped time-range pagination index.

Revision ID: 20260808_08
Revises: 20260808_07
Create Date: 2026-08-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_08"
down_revision = "20260808_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    index_names = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("public_source_articles")}
    if "ix_public_source_articles_source_published_id" not in index_names:
        op.create_index(
            "ix_public_source_articles_source_published_id",
            "public_source_articles",
            ["source_id", "published_at", "id"],
        )


def downgrade() -> None:
    index_names = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("public_source_articles")}
    if "ix_public_source_articles_source_published_id" in index_names:
        op.drop_index("ix_public_source_articles_source_published_id", table_name="public_source_articles")
