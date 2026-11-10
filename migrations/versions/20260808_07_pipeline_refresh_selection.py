"""Track selected public sources and deduplicate by canonical URL.

Revision ID: 20260808_07
Revises: 20260807_06
Create Date: 2026-08-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260808_07"
down_revision = "20260807_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    collection_columns = {column["name"] for column in inspector.get_columns("public_collection_runs")}
    if "selected_source_slugs_json" not in collection_columns:
        op.add_column(
            "public_collection_runs",
            sa.Column("selected_source_slugs_json", sa.Text(), nullable=False, server_default="[]"),
        )
    index_names = {index["name"] for index in inspector.get_indexes("public_source_articles")}
    if "ix_public_source_articles_endpoint_url" in index_names:
        op.drop_index("ix_public_source_articles_endpoint_url", table_name="public_source_articles")
    if "ix_public_source_articles_source_url" not in index_names:
        op.create_index("ix_public_source_articles_source_url", "public_source_articles", ["source_id", "url"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("public_source_articles")}
    if "ix_public_source_articles_source_url" in index_names:
        op.drop_index("ix_public_source_articles_source_url", table_name="public_source_articles")
    collection_columns = {column["name"] for column in inspector.get_columns("public_collection_runs")}
    if "selected_source_slugs_json" in collection_columns:
        op.drop_column("public_collection_runs", "selected_source_slugs_json")
