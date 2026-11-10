"""Scope request-control state per request channel.

The gate used to keep a single global ``request_control_state`` row, so the
6h cooldown earned by the abandoned WeChat-backend article route also locked
the WeRead reader out for hours.  This adds a per-channel state table plus a
``channel`` column on the request ledger.

The legacy ``request_control_state`` row is deliberately left untouched: it
stays as the frozen audit trail of the abandoned route.  Its cooldown is *not*
copied into any live channel, because it was earned by an operation that no
longer exists.  Each live channel owns its cooldown from now on and applies it
only to failures it actually observes.

Revision ID: 20260810_10
Revises: 20260809_09
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260810_10"
down_revision = "20260809_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from wechat_list_collector.infrastructure.db.schema import phase7_tables, request_channel_state

    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # The request ledger gains a channel dimension.  On a fresh database the
    # table is created by migration 02 straight from the current metadata, so
    # the column may already be there.
    columns = {column["name"] for column in inspector.get_columns("wechat_requests")}
    if "channel" not in columns:
        op.add_column(
            "wechat_requests",
            sa.Column("channel", sa.String(), nullable=False, server_default="mp_admin"),
        )

    request_channel_state.metadata.create_all(bind, tables=phase7_tables)

    existing = {
        row[0]
        for row in bind.execute(sa.text("SELECT channel FROM request_channel_state")).fetchall()
    }

    # The admin / account-search channel keeps its own pacing and cooldown
    # policy, but starts without a penalty inherited from the abandoned route.
    if "mp_admin" not in existing:
        legacy = bind.execute(
            sa.text(
                "SELECT consecutive_rate_limit_count, last_rate_limit_at FROM request_control_state WHERE id = 1"
            )
        ).mappings().first()
        bind.execute(
            sa.text(
                "INSERT INTO request_channel_state (channel, last_request_started_at, cooldown_until, "
                "rolling_window_limit, consecutive_rate_limit_count, last_rate_limit_at, version) "
                "VALUES ('mp_admin', NULL, NULL, 60, :consecutive_rate_limit_count, :last_rate_limit_at, 0)"
            ),
            {
                "consecutive_rate_limit_count": int(legacy["consecutive_rate_limit_count"]) if legacy else 0,
                "last_rate_limit_at": legacy["last_rate_limit_at"] if legacy else None,
            },
        )

    # The WeRead reader starts clean and gets its own generous budget.
    if "weread_reader" not in existing:
        bind.execute(
            sa.text(
                "INSERT INTO request_channel_state (channel, last_request_started_at, cooldown_until, "
                "rolling_window_limit, consecutive_rate_limit_count, last_rate_limit_at, version) "
                "VALUES ('weread_reader', NULL, NULL, 600, 0, NULL, 0)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM request_channel_state WHERE channel IN ('weread_reader', 'mp_admin')"))
    columns = {column["name"] for column in sa.inspect(bind).get_columns("wechat_requests")}
    if "channel" in columns:
        with op.batch_alter_table("wechat_requests") as batch:
            batch.drop_column("channel")
