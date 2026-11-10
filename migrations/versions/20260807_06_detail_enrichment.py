"""Add public-detail enrichment audit records.

Revision ID: 20260807_06
Revises: 20260805_05
Create Date: 2026-08-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260807_06"
down_revision = "20260805_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The original Phase 5 constraint omitted kinds that were already handled
    # by the application. SQLite requires a table rebuild to amend CHECKs.
    with op.batch_alter_table("public_source_endpoints", recreate="always") as batch:
        batch.drop_constraint("ck_public_source_endpoints_kind", type_="check")
        batch.create_check_constraint(
            "ck_public_source_endpoints_kind",
            "kind IN ('rss', 'json', 'browser', 'browser_network')",
        )
    existing_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("public_source_articles")}
    if "digest_source" not in existing_columns:
        op.add_column(
            "public_source_articles",
            sa.Column("digest_source", sa.String(), nullable=False, server_default="listing"),
        )
    from wechat_list_collector.infrastructure.db.schema import phase6_tables

    phase6_tables[0].metadata.create_all(op.get_bind(), tables=phase6_tables)


def downgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase6_tables

    phase6_tables[0].metadata.drop_all(op.get_bind(), tables=phase6_tables)
    existing_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("public_source_articles")}
    if "digest_source" in existing_columns:
        op.drop_column("public_source_articles", "digest_source")
    with op.batch_alter_table("public_source_endpoints", recreate="always") as batch:
        batch.drop_constraint("ck_public_source_endpoints_kind", type_="check")
        batch.create_check_constraint("ck_public_source_endpoints_kind", "kind IN ('rss', 'html', 'browser')")
