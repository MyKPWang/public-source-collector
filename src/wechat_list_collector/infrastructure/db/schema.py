from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Float, ForeignKey, Index, Integer, LargeBinary, MetaData, String, Table, Text, UniqueConstraint, text


metadata = MetaData()

official_accounts = Table(
    "official_accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_account_id", String, nullable=False, unique=True),
    Column("fakeid_ciphertext", LargeBinary, nullable=False),
    Column("name", String, nullable=False),
    Column("avatar_url", String, nullable=False, server_default=""),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("archived_at", String),
    Column("display_order", Integer, nullable=False, server_default="0"),
    Column("watermark_article_id", String),
    Column("last_success_at", String),
    Column("consecutive_failure_count", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("enabled IN (0, 1)", name="ck_official_accounts_enabled"),
)
Index("ix_official_accounts_active_order", official_accounts.c.archived_at, official_accounts.c.enabled, official_accounts.c.display_order)
Index("ix_official_accounts_last_success", official_accounts.c.last_success_at)

articles = Table(
    "articles",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("official_accounts.id"), nullable=False),
    Column("source_article_id", String, nullable=False),
    Column("title", String, nullable=False),
    Column("digest", Text, nullable=False, server_default=""),
    Column("url", Text, nullable=False),
    Column("cover_url", Text, nullable=False, server_default=""),
    Column("published_at", String, nullable=False),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("source_deleted", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("source_deleted IN (0, 1)", name="ck_articles_source_deleted"),
    UniqueConstraint("account_id", "source_article_id", name="uq_articles_account_source"),
)
Index("ix_articles_published", articles.c.published_at.desc(), articles.c.id.desc())
Index("ix_articles_account_published", articles.c.account_id, articles.c.published_at.desc(), articles.c.id.desc())
Index("ix_articles_first_seen", articles.c.first_seen_at.desc())

article_revisions = Table(
    "article_revisions",
    metadata,
    Column("id", String, primary_key=True),
    Column("article_id", String, ForeignKey("articles.id"), nullable=False),
    Column("revision_number", Integer, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("title", String, nullable=False),
    Column("digest", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("cover_url", Text, nullable=False),
    Column("published_at", String, nullable=False),
    Column("source_payload_json", Text, nullable=False),
    Column("observed_at", String, nullable=False),
    UniqueConstraint("article_id", "revision_number", name="uq_article_revisions_number"),
    UniqueConstraint("article_id", "content_hash", name="uq_article_revisions_hash"),
)

collection_commands = Table(
    "collection_commands",
    metadata,
    Column("id", String, primary_key=True),
    Column("command_type", String, nullable=False),
    Column("trigger", String, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("business_date", String),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("status", String, nullable=False),
    Column("run_id", String, unique=True),
    Column("submitted_by", String, nullable=False),
    Column("lease_owner", String),
    Column("lease_expires_at", String),
    Column("created_at", String, nullable=False),
    Column("claimed_at", String),
    Column("finished_at", String),
    Column("reason_code", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('queued', 'claimed', 'completed', 'rejected', 'failed', 'cancelled')", name="ck_collection_commands_status"),
)

collection_runs = Table(
    "collection_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("command_id", String, ForeignKey("collection_commands.id"), nullable=False, unique=True),
    Column("trigger", String, nullable=False),
    Column("business_date", String),
    Column("status", String, nullable=False),
    Column("planned_count", Integer, nullable=False, server_default="0"),
    Column("success_count", Integer, nullable=False, server_default="0"),
    Column("failed_count", Integer, nullable=False, server_default="0"),
    Column("not_started_count", Integer, nullable=False, server_default="0"),
    Column("request_count", Integer, nullable=False, server_default="0"),
    Column("new_article_count", Integer, nullable=False, server_default="0"),
    Column("updated_article_count", Integer, nullable=False, server_default="0"),
    Column("coverage_ratio", Float, nullable=False, server_default="0"),
    Column("started_at", String),
    Column("finished_at", String),
    Column("stop_reason_code", String, nullable=False, server_default=""),
    Column("stop_message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('queued', 'running', 'completed', 'partial', 'failed', 'rate_limited', 'session_expired', 'protocol_changed', 'rejected', 'cancelled', 'missed')", name="ck_collection_runs_status"),
)
Index("ix_collection_runs_business_date", collection_runs.c.business_date.desc())
Index("ix_collection_runs_started", collection_runs.c.started_at.desc())
Index("ix_collection_runs_status_started", collection_runs.c.status, collection_runs.c.started_at.desc())

collection_run_accounts = Table(
    "collection_run_accounts",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("collection_runs.id"), nullable=False),
    Column("account_id", String, ForeignKey("official_accounts.id"), nullable=False),
    Column("job_id", String),
    Column("plan_position", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("page_count", Integer, nullable=False, server_default="0"),
    Column("request_count", Integer, nullable=False, server_default="0"),
    Column("fetched_count", Integer, nullable=False, server_default="0"),
    Column("new_count", Integer, nullable=False, server_default="0"),
    Column("updated_count", Integer, nullable=False, server_default="0"),
    Column("started_at", String),
    Column("finished_at", String),
    Column("last_source_article_id", String, nullable=False, server_default=""),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('queued', 'running', 'completed', 'empty', 'page_limit_reached', 'failed', 'rate_limited', 'session_expired', 'protocol_changed', 'not_started')", name="ck_collection_run_accounts_status"),
    UniqueConstraint("run_id", "account_id", name="uq_collection_run_accounts_run_account"),
    UniqueConstraint("run_id", "plan_position", name="uq_collection_run_accounts_run_position"),
)

collection_run_articles = Table(
    "collection_run_articles",
    metadata,
    Column("run_id", String, ForeignKey("collection_runs.id"), primary_key=True),
    Column("article_id", String, ForeignKey("articles.id"), primary_key=True),
    Column("account_id", String, ForeignKey("official_accounts.id"), nullable=False),
    Column("page_number", Integer, nullable=False),
    Column("position", Integer, nullable=False),
    Column("observed_at", String, nullable=False),
    Column("is_first_seen", Integer, nullable=False),
    CheckConstraint("page_number >= 1", name="ck_collection_run_articles_page"),
    CheckConstraint("position >= 0", name="ck_collection_run_articles_position"),
    CheckConstraint("is_first_seen IN (0, 1)", name="ck_collection_run_articles_first_seen"),
)

# Revisions use these immutable table groups instead of metadata.create_all()
# without a table list. Adding a later table must never alter an older
# migration's schema.
phase1_tables = (
    official_accounts,
    articles,
    article_revisions,
    collection_commands,
    collection_runs,
    collection_run_accounts,
    collection_run_articles,
)

account_collection_jobs = Table(
    "account_collection_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("account_id", String, ForeignKey("official_accounts.id"), nullable=False),
    Column("origin_command_id", String, ForeignKey("collection_commands.id"), nullable=False),
    Column("status", String, nullable=False),
    Column("priority", Integer, nullable=False, server_default="10"),
    Column("queued_at", String, nullable=False),
    Column("started_at", String),
    Column("finished_at", String),
    Column("lease_owner", String),
    Column("lease_expires_at", String),
    Column("page_count", Integer, nullable=False, server_default="0"),
    Column("request_count", Integer, nullable=False, server_default="0"),
    Column("fetched_count", Integer, nullable=False, server_default="0"),
    Column("new_count", Integer, nullable=False, server_default="0"),
    Column("updated_count", Integer, nullable=False, server_default="0"),
    Column("stop_watermark_source_article_id", String, nullable=False, server_default=""),
    Column("first_page_head_source_article_id", String, nullable=False, server_default=""),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('queued', 'running', 'completed', 'empty', 'page_limit_reached', 'failed', 'rate_limited', 'session_expired', 'protocol_changed', 'cancelled')", name="ck_account_collection_jobs_status"),
)
Index(
    "uq_account_collection_jobs_active",
    account_collection_jobs.c.account_id,
    unique=True,
    sqlite_where=text("status IN ('queued', 'running')"),
)
Index("ix_account_collection_jobs_claim", account_collection_jobs.c.status, account_collection_jobs.c.priority.desc(), account_collection_jobs.c.queued_at)

collection_command_items = Table(
    "collection_command_items",
    metadata,
    Column("command_id", String, ForeignKey("collection_commands.id"), primary_key=True),
    Column("account_id", String, ForeignKey("official_accounts.id"), primary_key=True),
    Column("outcome", String, nullable=False),
    Column("job_id", String, ForeignKey("account_collection_jobs.id")),
    Column("active_run_id", String),
    Column("reason_code", String, nullable=False, server_default=""),
    CheckConstraint("outcome IN ('enqueued', 'promoted_existing_job', 'ignored_already_running', 'rejected')", name="ck_collection_command_items_outcome"),
)

wechat_requests = Table(
    "wechat_requests",
    metadata,
    Column("id", String, primary_key=True),
    Column("command_id", String, ForeignKey("collection_commands.id"), nullable=False),
    Column("job_id", String, ForeignKey("account_collection_jobs.id")),
    Column("run_id", String, ForeignKey("collection_runs.id")),
    Column("account_id", String, ForeignKey("official_accounts.id")),
    Column("channel", String, nullable=False, server_default="mp_admin"),
    Column("operation", String, nullable=False),
    Column("page_number", Integer),
    Column("begin", Integer),
    Column("count", Integer),
    Column("publish_record_count", Integer),
    Column("article_count", Integer),
    Column("status", String, nullable=False),
    Column("gate_wait_ms", Integer, nullable=False, server_default="0"),
    Column("planned_at", String),
    Column("sent_at", String),
    Column("finished_at", String),
    Column("duration_ms", Integer),
    Column("wechat_ret", Integer),
    Column("response_schema", String, nullable=False, server_default=""),
    Column("reason_code", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('planned', 'waiting', 'sent', 'succeeded', 'rate_limited', 'session_expired', 'network_failed', 'protocol_changed', 'unknown')", name="ck_wechat_requests_status"),
)
Index("ix_wechat_requests_run_sent", wechat_requests.c.run_id, wechat_requests.c.sent_at)
Index("ix_wechat_requests_account_sent", wechat_requests.c.account_id, wechat_requests.c.sent_at.desc())
Index("ix_wechat_requests_status_sent", wechat_requests.c.status, wechat_requests.c.sent_at.desc())
Index("ix_wechat_requests_planned", wechat_requests.c.planned_at)

request_control_state = Table(
    "request_control_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("last_request_started_at", String),
    Column("cooldown_until", String),
    Column("rolling_window_limit", Integer, nullable=False, server_default="60"),
    Column("consecutive_rate_limit_count", Integer, nullable=False, server_default="0"),
    Column("last_rate_limit_at", String),
    Column("version", Integer, nullable=False, server_default="0"),
    CheckConstraint("id = 1", name="ck_request_control_state_singleton"),
)

request_channel_state = Table(
    "request_channel_state",
    metadata,
    Column("channel", String, primary_key=True),
    Column("last_request_started_at", String),
    Column("cooldown_until", String),
    Column("rolling_window_limit", Integer, nullable=False, server_default="60"),
    Column("consecutive_rate_limit_count", Integer, nullable=False, server_default="0"),
    Column("last_rate_limit_at", String),
    Column("version", Integer, nullable=False, server_default="0"),
)

collection_events = Table(
    "collection_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("timestamp", String, nullable=False),
    Column("level", String, nullable=False),
    Column("event_type", String, nullable=False),
    Column("request_id", String, ForeignKey("wechat_requests.id")),
    Column("run_id", String, ForeignKey("collection_runs.id")),
    Column("account_id", String, ForeignKey("official_accounts.id")),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("details_json", Text, nullable=False, server_default="{}"),
)
Index("ix_collection_events_timestamp", collection_events.c.timestamp.desc())

phase2_tables = (
    account_collection_jobs,
    collection_command_items,
    wechat_requests,
    request_control_state,
    collection_events,
)

wechat_login_sessions = Table(
    "wechat_login_sessions",
    metadata,
    Column("id", String, primary_key=True),
    Column("channel", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_by", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("completed_at", String),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("channel IN ('local_web', 'feishu')", name="ck_wechat_login_sessions_channel"),
    CheckConstraint(
        "status IN ('creating', 'waiting_scan', 'scanned', 'confirming', 'validating', 'succeeded', 'expired', 'failed', 'cancelled')",
        name="ck_wechat_login_sessions_status",
    ),
)
Index("ix_wechat_login_sessions_active", wechat_login_sessions.c.status, wechat_login_sessions.c.created_at.desc())

phase3_tables = (wechat_login_sessions,)

account_search_sessions = Table(
    "account_search_sessions",
    metadata,
    Column("id", String, primary_key=True),
    Column("command_id", String, ForeignKey("collection_commands.id"), nullable=False, unique=True),
    Column("query", String, nullable=False),
    Column("created_by", String, nullable=False),
    Column("status", String, nullable=False),
    Column("candidate_payload_ciphertext", LargeBinary),
    Column("candidate_count", Integer, nullable=False, server_default="0"),
    Column("created_at", String, nullable=False),
    Column("started_at", String),
    Column("finished_at", String),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('queued', 'waiting', 'running', 'succeeded', 'failed')", name="ck_account_search_sessions_status"),
)
Index("ix_account_search_sessions_active", account_search_sessions.c.status, account_search_sessions.c.created_at.desc())

# Public-source ingestion is intentionally separate from the legacy WeChat
# tables. A website source has no fakeid, login session or article-detail fetch.
public_sources = Table(
    "public_sources",
    metadata,
    Column("id", String, primary_key=True),
    Column("slug", String, nullable=False, unique=True),
    Column("name", String, nullable=False),
    Column("homepage_url", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("enabled IN (0, 1)", name="ck_public_sources_enabled"),
)

public_source_endpoints = Table(
    "public_source_endpoints",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, ForeignKey("public_sources.id"), nullable=False),
    Column("endpoint_key", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("list_url", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("minimum_interval_seconds", Integer, nullable=False, server_default="900"),
    Column("max_items", Integer, nullable=False, server_default="50"),
    Column("last_fetch_started_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    CheckConstraint("kind IN ('rss', 'json', 'browser', 'browser_network')", name="ck_public_source_endpoints_kind"),
    CheckConstraint("enabled IN (0, 1)", name="ck_public_source_endpoints_enabled"),
    UniqueConstraint("source_id", "endpoint_key", name="uq_public_source_endpoints_key"),
)
Index("ix_public_source_endpoints_enabled", public_source_endpoints.c.enabled, public_source_endpoints.c.last_fetch_started_at)

public_collection_runs = Table(
    "public_collection_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("trigger", String, nullable=False),
    Column("business_date", String),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("selected_source_slugs_json", Text, nullable=False, server_default="[]"),
    Column("status", String, nullable=False),
    Column("planned_endpoint_count", Integer, nullable=False, server_default="0"),
    Column("success_endpoint_count", Integer, nullable=False, server_default="0"),
    Column("failed_endpoint_count", Integer, nullable=False, server_default="0"),
    Column("new_article_count", Integer, nullable=False, server_default="0"),
    Column("updated_article_count", Integer, nullable=False, server_default="0"),
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('running', 'completed', 'partial', 'failed')", name="ck_public_collection_runs_status"),
)
Index("ix_public_collection_runs_scheduled", public_collection_runs.c.trigger, public_collection_runs.c.finished_at.desc())

public_endpoint_runs = Table(
    "public_endpoint_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", String, ForeignKey("public_collection_runs.id"), nullable=False),
    Column("endpoint_id", String, ForeignKey("public_source_endpoints.id"), nullable=False),
    Column("status", String, nullable=False),
    Column("fetched_count", Integer, nullable=False, server_default="0"),
    Column("accepted_count", Integer, nullable=False, server_default="0"),
    Column("rejected_count", Integer, nullable=False, server_default="0"),
    Column("new_count", Integer, nullable=False, server_default="0"),
    Column("updated_count", Integer, nullable=False, server_default="0"),
    Column("started_at", String),
    Column("finished_at", String),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    Column("rejection_summary_json", Text, nullable=False, server_default="{}"),
    Column("rejection_samples_json", Text, nullable=False, server_default="[]"),
    CheckConstraint("status IN ('running', 'completed', 'partial', 'failed', 'skipped')", name="ck_public_endpoint_runs_status"),
    UniqueConstraint("run_id", "endpoint_id", name="uq_public_endpoint_runs_run_endpoint"),
)
Index("ix_public_endpoint_runs_run", public_endpoint_runs.c.run_id, public_endpoint_runs.c.status)

public_source_articles = Table(
    "public_source_articles",
    metadata,
    Column("id", String, primary_key=True),
    Column("source_id", String, ForeignKey("public_sources.id"), nullable=False),
    Column("endpoint_id", String, ForeignKey("public_source_endpoints.id"), nullable=False),
    Column("source_item_key", String, nullable=False),
    Column("title", String, nullable=False),
    Column("digest", Text, nullable=False),
    Column("digest_source", String, nullable=False, server_default="listing"),
    Column("url", Text, nullable=False),
    Column("cover_url", Text, nullable=False, server_default=""),
    Column("published_at", String, nullable=False),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("endpoint_id", "source_item_key", name="uq_public_source_articles_item"),
)
Index("ix_public_source_articles_published", public_source_articles.c.published_at.desc(), public_source_articles.c.id.desc())
Index("ix_public_source_articles_source_published", public_source_articles.c.source_id, public_source_articles.c.published_at.desc())
Index(
    "ix_public_source_articles_source_published_id",
    public_source_articles.c.source_id,
    public_source_articles.c.published_at.desc(),
    public_source_articles.c.id.desc(),
)
Index("ix_public_source_articles_source_url", public_source_articles.c.source_id, public_source_articles.c.url)

public_article_enrichment_attempts = Table(
    "public_article_enrichment_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("endpoint_run_id", String, ForeignKey("public_endpoint_runs.id"), nullable=False),
    Column("article_id", String, ForeignKey("public_source_articles.id")),
    Column("source_item_key", String, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("method", String, nullable=False),
    Column("status", String, nullable=False),
    Column("duration_ms", Integer, nullable=False, server_default="0"),
    Column("attempted_at", String, nullable=False),
    Column("reason_code", String, nullable=False, server_default=""),
    Column("message", String, nullable=False, server_default=""),
    CheckConstraint("status IN ('succeeded', 'failed')", name="ck_public_article_enrichment_attempts_status"),
)
Index("ix_public_article_enrichment_attempts_run", public_article_enrichment_attempts.c.endpoint_run_id)
Index("ix_public_article_enrichment_attempts_article", public_article_enrichment_attempts.c.article_id, public_article_enrichment_attempts.c.attempted_at.desc())

public_endpoint_run_articles = Table(
    "public_endpoint_run_articles",
    metadata,
    Column("endpoint_run_id", String, ForeignKey("public_endpoint_runs.id"), primary_key=True),
    Column("article_id", String, ForeignKey("public_source_articles.id"), primary_key=True),
    Column("observed_at", String, nullable=False),
    Column("is_first_seen", Integer, nullable=False),
    CheckConstraint("is_first_seen IN (0, 1)", name="ck_public_endpoint_run_articles_first_seen"),
)

phase5_tables = (
    public_sources,
    public_source_endpoints,
    public_collection_runs,
    public_endpoint_runs,
    public_source_articles,
    public_endpoint_run_articles,
)

phase6_tables = (public_article_enrichment_attempts,)

phase4_tables = (account_search_sessions,)

phase7_tables = (request_channel_state,)
