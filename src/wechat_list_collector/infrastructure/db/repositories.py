from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ...domain.enums import GLOBAL_FAILURE_ACCOUNT_STATUSES, SUCCESSFUL_ACCOUNT_STATUSES, CollectionRunStatus, RunAccountStatus
from ...domain.ids import new_uuid7
from ...domain.models import AccountCollectionJob, ArticleInput, CommandItemResult, PageStoreResult, RunSummary, SubmittedCommand
from .schema import (
    account_search_sessions,
    account_collection_jobs,
    article_revisions,
    articles,
    collection_command_items,
    collection_commands,
    collection_events,
    collection_run_accounts,
    collection_run_articles,
    collection_runs,
    official_accounts,
    wechat_login_sessions,
    wechat_requests,
)


class RepositoryError(ValueError):
    """A requested state transition cannot be represented by the persisted model."""


# 跨通道去重时允许的发布时间差（小时）。微信读书时间是整分、补采时间是秒级，
# 同一篇文章两条记录的正常差 < 1 分钟；留足余量是为了跨零点的情况。
# 超过这个窗口的同名文章视为**不同文章**（同号公众号会在不同日子发同名文章）。
_RECOVERY_DEDUP_SPAN_HOURS = 6.0


class CollectionRepository:
    """Transactional persistence for the Phase 1 content and collection facts."""

    def __init__(self, engine: Engine, now: Callable[[], datetime] | None = None):
        self.engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def add_account(
        self,
        *,
        source_account_id: str,
        fakeid_ciphertext: bytes,
        name: str,
        avatar_url: str = "",
        enabled: bool = True,
        display_order: int = 0,
        watermark_article_id: str | None = None,
    ) -> str:
        now = self._timestamp()
        with self.engine.begin() as connection:
            row = connection.execute(
                select(official_accounts.c.id).where(official_accounts.c.source_account_id == source_account_id)
            ).scalar_one_or_none()
            if row is not None:
                values: dict[str, object] = {
                    "fakeid_ciphertext": fakeid_ciphertext,
                    "name": name,
                    "avatar_url": avatar_url,
                    "enabled": int(enabled),
                    "archived_at": None,
                    "display_order": display_order,
                    "updated_at": now,
                }
                if watermark_article_id is not None:
                    values["watermark_article_id"] = watermark_article_id
                connection.execute(
                    update(official_accounts)
                    .where(official_accounts.c.id == row)
                    .values(**values)
                )
                return str(row)
            account_id = new_uuid7()
            connection.execute(
                official_accounts.insert().values(
                    id=account_id,
                    source_account_id=source_account_id,
                    fakeid_ciphertext=fakeid_ciphertext,
                    name=name,
                    avatar_url=avatar_url,
                    enabled=int(enabled),
                    display_order=display_order,
                    watermark_article_id=watermark_article_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            return account_id

    def create_account_search_session(self, *, query: str, created_by: str) -> tuple[dict[str, object], bool]:
        now = self._timestamp()
        active_statuses = ("queued", "waiting", "running")
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(account_search_sessions)
                .where(account_search_sessions.c.status.in_(active_statuses))
                .order_by(account_search_sessions.c.created_at.desc())
                .limit(1)
            ).mappings().one_or_none()
            if existing is not None:
                return dict(existing), True

            session_id = new_uuid7()
            command_id = new_uuid7()
            connection.execute(
                collection_commands.insert().values(
                    id=command_id,
                    command_type="account_search",
                    trigger="manual_web",
                    payload_json="{}",
                    idempotency_key=f"account-search:{session_id}",
                    status="claimed",
                    submitted_by=created_by,
                    created_at=now,
                    claimed_at=now,
                )
            )
            connection.execute(
                account_search_sessions.insert().values(
                    id=session_id,
                    command_id=command_id,
                    query=query,
                    created_by=created_by,
                    status="queued",
                    created_at=now,
                    message="等待搜索请求排队",
                )
            )
        session = self.get_account_search_session(session_id)
        assert session is not None
        return session, False

    def get_account_search_session(self, session_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(account_search_sessions).where(account_search_sessions.c.id == session_id)
            ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def latest_account_search_session(self) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(account_search_sessions).order_by(account_search_sessions.c.created_at.desc()).limit(1)
            ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def update_account_search_session(
        self,
        session_id: str,
        *,
        status: str,
        candidate_payload_ciphertext: bytes | None = None,
        candidate_count: int | None = None,
        reason_code: str = "",
        message: str = "",
    ) -> dict[str, object]:
        terminal = {"succeeded", "failed"}
        values: dict[str, object] = {"status": status, "reason_code": reason_code, "message": message}
        if candidate_payload_ciphertext is not None:
            values["candidate_payload_ciphertext"] = candidate_payload_ciphertext
        if candidate_count is not None:
            values["candidate_count"] = candidate_count
        if status == "running":
            values["started_at"] = self._timestamp()
        with self.engine.begin() as connection:
            if status in terminal:
                values["finished_at"] = self._timestamp()
            result = connection.execute(
                update(account_search_sessions).where(account_search_sessions.c.id == session_id).values(**values)
            )
            if result.rowcount != 1:
                raise RepositoryError("account search session does not exist")
            if status in terminal:
                command_status = "completed" if status == "succeeded" else "failed"
                connection.execute(
                    update(collection_commands)
                    .where(collection_commands.c.id == select(account_search_sessions.c.command_id).where(account_search_sessions.c.id == session_id).scalar_subquery())
                    .values(status=command_status, finished_at=self._timestamp(), reason_code=reason_code)
                )
        session = self.get_account_search_session(session_id)
        assert session is not None
        return session

    def submit_collection_command(
        self,
        *,
        command_type: str,
        trigger: str,
        account_ids: Sequence[str],
        idempotency_key: str,
        submitted_by: str = "system",
        business_date: str | None = None,
    ) -> SubmittedCommand:
        account_ids = list(dict.fromkeys(account_ids))
        if not idempotency_key:
            raise RepositoryError("idempotency_key is required")
        now = self._timestamp()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(collection_commands.c.id, collection_commands.c.run_id).where(
                    collection_commands.c.idempotency_key == idempotency_key
                )
            ).mappings().one_or_none()
            if existing is not None:
                return SubmittedCommand(str(existing["id"]), str(existing["run_id"]), reused=True)

            if account_ids:
                found = set(
                    connection.execute(
                        select(official_accounts.c.id).where(official_accounts.c.id.in_(account_ids))
                    ).scalars()
                )
                missing = set(account_ids) - found
                if missing:
                    raise RepositoryError("command references unknown account")

            command_id = new_uuid7()
            run_id = new_uuid7()
            payload = json.dumps({"account_ids": account_ids}, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                collection_commands.insert().values(
                    id=command_id,
                    command_type=command_type,
                    trigger=trigger,
                    payload_json=payload,
                    business_date=business_date,
                    idempotency_key=idempotency_key,
                    status="queued",
                    run_id=run_id,
                    submitted_by=submitted_by,
                    created_at=now,
                )
            )
            connection.execute(
                collection_runs.insert().values(
                    id=run_id,
                    command_id=command_id,
                    trigger=trigger,
                    business_date=business_date,
                    status="queued",
                    planned_count=len(account_ids),
                )
            )
            is_manual = trigger in {"manual_web", "manual_cli", "feishu"}
            for position, account_id in enumerate(account_ids):
                existing_job = connection.execute(
                    select(account_collection_jobs)
                    .where(
                        account_collection_jobs.c.account_id == account_id,
                        account_collection_jobs.c.status.in_(("queued", "running")),
                    )
                    .order_by(account_collection_jobs.c.queued_at)
                ).mappings().one_or_none()
                outcome = "enqueued"
                job_id: str | None = None
                run_account_status = "queued"
                reason_code = ""
                active_run_id: str | None = None
                if existing_job is not None and existing_job["status"] == "running" and is_manual:
                    outcome = "ignored_already_running"
                    run_account_status = "not_started"
                    reason_code = "ALREADY_RUNNING"
                    active_run_id = connection.execute(
                        select(collection_run_accounts.c.run_id)
                        .where(collection_run_accounts.c.job_id == existing_job["id"])
                        .order_by(collection_run_accounts.c.started_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                elif existing_job is not None:
                    job_id = str(existing_job["id"])
                    outcome = "promoted_existing_job"
                    if is_manual and int(existing_job["priority"]) < 100:
                        connection.execute(
                            update(account_collection_jobs)
                            .where(account_collection_jobs.c.id == job_id)
                            .values(priority=100)
                        )
                else:
                    account = connection.execute(
                        select(official_accounts.c.watermark_article_id).where(official_accounts.c.id == account_id)
                    ).mappings().one()
                    job_id = new_uuid7()
                    connection.execute(
                        account_collection_jobs.insert().values(
                            id=job_id,
                            account_id=account_id,
                            origin_command_id=command_id,
                            status="queued",
                            priority=100 if is_manual else 10,
                            queued_at=now,
                            stop_watermark_source_article_id=account["watermark_article_id"] or "",
                        )
                    )
                connection.execute(
                    collection_run_accounts.insert().values(
                        id=new_uuid7(),
                        run_id=run_id,
                        account_id=account_id,
                        job_id=job_id,
                        plan_position=position,
                        status=run_account_status,
                        reason_code=reason_code,
                    )
                )
                connection.execute(
                    collection_command_items.insert().values(
                        command_id=command_id,
                        account_id=account_id,
                        outcome=outcome,
                        job_id=job_id,
                        active_run_id=active_run_id,
                        reason_code=reason_code,
                    )
                )
            return SubmittedCommand(command_id, run_id, reused=False)

    def start_run(self, run_id: str) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(collection_runs)
                .where(collection_runs.c.id == run_id, collection_runs.c.status == "queued")
                .values(status="running", started_at=self._timestamp())
            )
            if result.rowcount != 1:
                raise RepositoryError("run is not queued")
            connection.execute(
                update(collection_commands)
                .where(collection_commands.c.run_id == run_id, collection_commands.c.status == "queued")
                .values(status="claimed", claimed_at=self._timestamp())
            )

    def claim_next_job(self, worker_id: str, lease_seconds: int = 900) -> AccountCollectionJob | None:
        """Claim one account job using a SQLite write lock as the queue mutex."""
        if lease_seconds < 1:
            raise RepositoryError("lease_seconds must be positive")
        now = self._timestamp()
        expires_at = self._timestamp(self._now() + timedelta(seconds=lease_seconds))
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    update(account_collection_jobs)
                    .where(
                        account_collection_jobs.c.status == "running",
                        account_collection_jobs.c.lease_expires_at.is_not(None),
                        account_collection_jobs.c.lease_expires_at < now,
                    )
                    .values(status="queued", lease_owner=None, lease_expires_at=None)
                )
                job = connection.execute(
                    select(account_collection_jobs, official_accounts.c.fakeid_ciphertext)
                    .join(official_accounts, official_accounts.c.id == account_collection_jobs.c.account_id)
                    .where(account_collection_jobs.c.status == "queued")
                    .order_by(account_collection_jobs.c.priority.desc(), account_collection_jobs.c.queued_at)
                    .limit(1)
                ).mappings().one_or_none()
                if job is None:
                    connection.commit()
                    return None
                connection.execute(
                    update(account_collection_jobs)
                    .where(account_collection_jobs.c.id == job["id"], account_collection_jobs.c.status == "queued")
                    .values(status="running", lease_owner=worker_id, lease_expires_at=expires_at, started_at=now)
                )
                connection.execute(
                    update(collection_run_accounts)
                    .where(collection_run_accounts.c.job_id == job["id"], collection_run_accounts.c.status == "queued")
                    .values(status="running", started_at=now)
                )
                run_ids = connection.execute(
                    select(collection_run_accounts.c.run_id).where(collection_run_accounts.c.job_id == job["id"])
                ).scalars().all()
                if run_ids:
                    connection.execute(
                        update(collection_runs)
                        .where(collection_runs.c.id.in_(run_ids), collection_runs.c.status == "queued")
                        .values(status="running", started_at=now)
                    )
                    connection.execute(
                        update(collection_commands)
                        .where(collection_commands.c.run_id.in_(run_ids), collection_commands.c.status == "queued")
                        .values(status="claimed", claimed_at=now)
                    )
                connection.commit()
                return AccountCollectionJob(
                    id=str(job["id"]),
                    account_id=str(job["account_id"]),
                    command_id=str(job["origin_command_id"]),
                    status="running",
                    priority=int(job["priority"]),
                    fakeid_ciphertext=bytes(job["fakeid_ciphertext"]),
                    stop_watermark_source_article_id=str(job["stop_watermark_source_article_id"]),
                )
            except Exception:
                connection.rollback()
                raise

    def finish_job(
        self,
        *,
        job_id: str,
        status: RunAccountStatus,
        page_count: int,
        request_count: int,
        fetched_count: int,
        new_count: int,
        updated_count: int,
        first_page_head_source_article_id: str = "",
        reason_code: str = "",
        message: str = "",
    ) -> list[str]:
        if status in {RunAccountStatus.QUEUED, RunAccountStatus.RUNNING, RunAccountStatus.NOT_STARTED}:
            raise RepositoryError("job must finish with a terminal account status")
        now = self._timestamp()
        with self.engine.begin() as connection:
            job = connection.execute(
                select(account_collection_jobs.c.account_id).where(account_collection_jobs.c.id == job_id)
            ).mappings().one_or_none()
            if job is None:
                raise RepositoryError("job does not exist")
            result = connection.execute(
                update(account_collection_jobs)
                .where(account_collection_jobs.c.id == job_id, account_collection_jobs.c.status == "running")
                .values(
                    status=status.value,
                    lease_owner=None,
                    lease_expires_at=None,
                    finished_at=now,
                    page_count=page_count,
                    request_count=request_count,
                    fetched_count=fetched_count,
                    new_count=new_count,
                    updated_count=updated_count,
                    first_page_head_source_article_id=first_page_head_source_article_id,
                    reason_code=reason_code,
                    message=message,
                )
            )
            if result.rowcount != 1:
                raise RepositoryError("job is not running")
            connection.execute(
                update(collection_run_accounts)
                .where(collection_run_accounts.c.job_id == job_id)
                .values(
                    status=status.value,
                    page_count=page_count,
                    request_count=request_count,
                    fetched_count=fetched_count,
                    new_count=new_count,
                    updated_count=updated_count,
                    finished_at=now,
                    last_source_article_id=first_page_head_source_article_id,
                    reason_code=reason_code,
                    message=message,
                )
            )
            if status in SUCCESSFUL_ACCOUNT_STATUSES:
                account_values: dict[str, object] = {"last_success_at": now, "consecutive_failure_count": 0, "updated_at": now}
                if first_page_head_source_article_id:
                    account_values["watermark_article_id"] = first_page_head_source_article_id
                connection.execute(
                    update(official_accounts).where(official_accounts.c.id == job["account_id"]).values(**account_values)
                )
            else:
                connection.execute(
                    update(official_accounts)
                    .where(official_accounts.c.id == job["account_id"])
                    .values(
                        consecutive_failure_count=official_accounts.c.consecutive_failure_count + 1,
                        updated_at=now,
                    )
                )
            return [
                str(row)
                for row in connection.execute(
                    select(collection_run_accounts.c.run_id).where(collection_run_accounts.c.job_id == job_id)
                ).scalars()
            ]

    def mark_queued_jobs_not_started(self, *, reason_code: str, message: str = "") -> list[str]:
        """Cancel work that has not reached the gateway after a global failure."""
        now = self._timestamp()
        with self.engine.begin() as connection:
            job_ids = connection.execute(
                select(account_collection_jobs.c.id).where(account_collection_jobs.c.status == "queued")
            ).scalars().all()
            if not job_ids:
                return []
            connection.execute(
                update(account_collection_jobs)
                .where(account_collection_jobs.c.id.in_(job_ids))
                .values(status="cancelled", finished_at=now, reason_code=reason_code, message=message)
            )
            connection.execute(
                update(collection_run_accounts)
                .where(collection_run_accounts.c.job_id.in_(job_ids), collection_run_accounts.c.status == "queued")
                .values(status="not_started", finished_at=now, reason_code=reason_code, message=message)
            )
            return [
                str(row)
                for row in connection.execute(
                    select(collection_run_accounts.c.run_id).where(collection_run_accounts.c.job_id.in_(job_ids))
                ).scalars()
            ]

    def command_items(self, command_id: str) -> list[CommandItemResult]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(collection_command_items)
                .where(collection_command_items.c.command_id == command_id)
                .order_by(collection_command_items.c.account_id)
            ).mappings()
            return [
                CommandItemResult(
                    account_id=str(row["account_id"]),
                    outcome=str(row["outcome"]),
                    job_id=str(row["job_id"]) if row["job_id"] else None,
                    active_run_id=str(row["active_run_id"]) if row["active_run_id"] else None,
                    reason_code=str(row["reason_code"]),
                )
                for row in rows
            ]

    def finalize_ready_runs(self) -> list[RunSummary]:
        with self.engine.connect() as connection:
            run_ids = [
                str(row)
                for row in connection.execute(
                    select(collection_runs.c.id)
                    .where(collection_runs.c.status.in_(("queued", "running")))
                    .where(
                        ~collection_runs.c.id.in_(
                            select(collection_run_accounts.c.run_id).where(
                                collection_run_accounts.c.status.in_(("queued", "running"))
                            )
                        )
                    )
                ).scalars()
            ]
        return [self.finalize_run(run_id) for run_id in run_ids]

    def set_run_account_status(
        self,
        *,
        run_id: str,
        account_id: str,
        status: RunAccountStatus,
        reason_code: str = "",
        message: str = "",
    ) -> None:
        values: dict[str, object] = {"status": status.value, "reason_code": reason_code, "message": message}
        if status == RunAccountStatus.RUNNING:
            values["started_at"] = self._timestamp()
        elif status not in {RunAccountStatus.QUEUED, RunAccountStatus.NOT_STARTED}:
            values["finished_at"] = self._timestamp()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(collection_run_accounts)
                .where(collection_run_accounts.c.run_id == run_id, collection_run_accounts.c.account_id == account_id)
                .values(**values)
            )
            if result.rowcount != 1:
                raise RepositoryError("run account does not exist")

    def existing_digests(self, account_id: str, source_article_ids: Sequence[str]) -> dict[str, str]:
        """Return the digests already stored for the given source articles.

        The summary enricher uses this to skip articles that already carry a
        digest, so a re-collected article never costs another detail request.
        """
        ids = [str(value) for value in source_article_ids if value]
        if not ids:
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(articles.c.source_article_id, articles.c.digest).where(
                    articles.c.account_id == account_id,
                    articles.c.source_article_id.in_(ids),
                )
            ).all()
        return {str(row[0]): str(row[1] or "") for row in rows}

    def latest_command_id(self) -> str | None:
        """Return the most recent collection command id.

        Standalone summary backfill has no job of its own, so its requests are
        attributed to the latest command for per-command budget accounting.
        """
        with self.engine.connect() as connection:
            command_id = connection.execute(
                select(collection_commands.c.id).order_by(collection_commands.c.created_at.desc()).limit(1)
            ).scalar_one_or_none()
        return str(command_id) if command_id else None

    def list_articles_missing_digest(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        limit: int = 60,
    ) -> list[dict[str, object]]:
        """Return in-window articles that still have no digest.

        The daily report only shows articles that carry a summary or body, so
        a stored article with an empty digest is invisible to the pipeline.
        This drives the standalone summary backfill pass.
        """
        if not 1 <= limit <= 500:
            raise RepositoryError("limit must be between 1 and 500")
        start = self._timestamp(window_start)
        end = self._timestamp(window_end)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    articles.c.id,
                    articles.c.account_id,
                    articles.c.source_article_id,
                    articles.c.title,
                    articles.c.published_at,
                )
                .where(
                    articles.c.source_deleted == 0,
                    articles.c.digest == "",
                    articles.c.published_at >= start,
                    articles.c.published_at < end,
                )
                .order_by(articles.c.published_at.desc(), articles.c.id.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def set_article_digest(self, *, account_id: str, source_article_id: str, digest: str) -> bool:
        """Fill in a previously empty digest without touching anything else.

        The digest is monotonic: an existing non-empty value is never
        overwritten, so a later list collection (which carries no digest) can
        never wipe a summary that was fetched at a cost of one request.
        """
        summary = str(digest or "").strip()
        if not summary:
            return False
        now = self._timestamp()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(articles).where(
                    articles.c.account_id == account_id,
                    articles.c.source_article_id == source_article_id,
                )
            ).mappings().one_or_none()
            if existing is None:
                return False
            if str(existing["digest"] or "").strip():
                return False
            # content_hash covers the digest, so it has to move with it —
            # otherwise the next list collection records a phantom revision.
            content_hash = self._content_hash(
                ArticleInput(
                    source_article_id=str(existing["source_article_id"]),
                    title=str(existing["title"]),
                    digest=summary,
                    url=str(existing["url"]),
                    cover_url=str(existing["cover_url"]),
                    published_at=self._parse_timestamp(str(existing["published_at"])),
                    source_payload={},
                ),
                str(existing["published_at"]),
            )
            connection.execute(
                update(articles)
                .where(
                    articles.c.account_id == account_id,
                    articles.c.source_article_id == source_article_id,
                )
                .values(digest=summary, content_hash=content_hash, updated_at=now)
            )
        return True

    def store_page(
        self,
        *,
        run_id: str,
        account_id: str,
        page_number: int,
        articles_to_store: Iterable[ArticleInput],
    ) -> PageStoreResult:
        if page_number < 1:
            raise RepositoryError("page_number must be positive")
        page = list(articles_to_store)
        now = self._timestamp()
        new_count = 0
        updated_count = 0
        observed_count = 0

        with self.engine.begin() as connection:
            run_account = connection.execute(
                select(collection_run_accounts.c.id).where(
                    collection_run_accounts.c.run_id == run_id,
                    collection_run_accounts.c.account_id == account_id,
                )
            ).scalar_one_or_none()
            if run_account is None:
                raise RepositoryError("page does not belong to this run account")

            for position, article in enumerate(page):
                existing = connection.execute(
                    select(articles).where(
                        articles.c.account_id == account_id,
                        articles.c.source_article_id == article.source_article_id,
                    )
                ).mappings().one_or_none()
                # 摘要单调更新：列表接口不带摘要，后续采集会带回空摘要。
                # 已有的非空摘要必须保留，否则补全结果会被下一轮空值覆盖。
                effective = article
                if existing is not None:
                    stored_digest = str(existing["digest"] or "")
                    if stored_digest.strip() and not article.digest.strip():
                        effective = replace(article, digest=stored_digest)
                values = self._article_values(effective, now)
                is_first_seen = existing is None
                if existing is None:
                    article_id = new_uuid7()
                    connection.execute(articles.insert().values(id=article_id, account_id=account_id, **values))
                    new_count += 1
                else:
                    article_id = str(existing["id"])
                    if existing["content_hash"] != values["content_hash"]:
                        revision_number = int(
                            connection.execute(
                                select(func.coalesce(func.max(article_revisions.c.revision_number), 0)).where(
                                    article_revisions.c.article_id == article_id
                                )
                            ).scalar_one()
                        ) + 1
                        connection.execute(
                            article_revisions.insert().values(
                                id=new_uuid7(),
                                article_id=article_id,
                                revision_number=revision_number,
                                content_hash=existing["content_hash"],
                                title=existing["title"],
                                digest=existing["digest"],
                                url=existing["url"],
                                cover_url=existing["cover_url"],
                                published_at=existing["published_at"],
                                source_payload_json=self._source_payload_json(effective),
                                observed_at=now,
                            )
                        )
                        updated_values = {
                            key: value
                            for key, value in values.items()
                            if key not in {"first_seen_at", "last_seen_at", "created_at", "updated_at"}
                        }
                        connection.execute(
                            update(articles)
                            .where(articles.c.id == article_id)
                            .values(**updated_values, last_seen_at=now, updated_at=now)
                        )
                        updated_count += 1
                    else:
                        connection.execute(
                            update(articles).where(articles.c.id == article_id).values(last_seen_at=now, updated_at=now)
                        )

                observation = connection.execute(
                    sqlite_insert(collection_run_articles)
                    .values(
                        run_id=run_id,
                        article_id=article_id,
                        account_id=account_id,
                        page_number=page_number,
                        position=position,
                        observed_at=now,
                        is_first_seen=int(is_first_seen),
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "article_id"])
                )
                observed_count += max(observation.rowcount, 0)

            connection.execute(
                update(collection_run_accounts)
                .where(collection_run_accounts.c.id == run_account)
                .values(
                    page_count=func.max(collection_run_accounts.c.page_count, page_number),
                    request_count=collection_run_accounts.c.request_count + 1,
                    fetched_count=collection_run_accounts.c.fetched_count + len(page),
                    new_count=collection_run_accounts.c.new_count + new_count,
                    updated_count=collection_run_accounts.c.updated_count + updated_count,
                )
            )
            connection.execute(
                update(collection_runs)
                .where(collection_runs.c.id == run_id)
                .values(
                    request_count=collection_runs.c.request_count + 1,
                    new_article_count=collection_runs.c.new_article_count + new_count,
                    updated_article_count=collection_runs.c.updated_article_count + updated_count,
                )
            )
        return PageStoreResult(len(page), new_count, updated_count, observed_count)

    def store_recovered_articles(
        self,
        *,
        account_id: str,
        articles_to_store: Iterable[ArticleInput],
    ) -> tuple[int, int]:
        """幂等写入补采到的文章，返回 ``(新增数, 更新数)``。

        与 :meth:`store_page` 的区别：补采不隶属于某次采集运行，因此不写
        ``collection_run_articles``，也不需要 run/account 关联。

        幂等性来自 ``uq_articles_account_source (account_id, source_article_id)``
        唯一约束：同一篇文章重复补采只会更新既有记录，不会新建重复行。

        此外还做一次**跨通道去重**：微信读书通道用的 ``source_article_id`` 是
        ``MP_WXS_{uin}_{reviewId}``，而补采用的是 ``MP_{biz}_{mid}_{idx}``，
        两套 ID 对同一篇文章取值不同，唯一约束拦不住，会写出重复行。
        因此写入前在同一账号内查重：标题规范化相同 **且发布时间相差不超过
        ``_RECOVERY_DEDUP_SPAN_HOURS`` 小时**、且两行 ``source_article_id``
        前缀不同（一行微信读书行、一行补采行）时，才判定为同一篇并合并，
        只补充它缺失的字段（正文链接、封面、摘要），不新建行。

        标题相同但发布时间相隔多天的**不算重复** —— 同一个公众号会在不同
        日子发同名文章（实测量子位「量子位编辑作者招聘」10 天发了 10 条），
        按标题无差别去重会误删历史文章。
        """
        page = list(articles_to_store)
        now = self._timestamp()
        new_count = 0
        updated_count = 0

        with self.engine.begin() as connection:
            for article in page:
                existing = connection.execute(
                    select(articles).where(
                        articles.c.account_id == account_id,
                        articles.c.source_article_id == article.source_article_id,
                    )
                ).mappings().one_or_none()
                if existing is None:
                    # 跨通道去重：标题相同 + 时间接近 + ID 前缀不同 = 同一篇。
                    existing = self._find_recovered_duplicate(
                        connection, account_id, article
                    )
                    if existing is not None:
                        # 合并：保留 WeRead 行的 source_article_id，只补空缺字段。
                        merged = self._merge_recovered_values(
                            existing, article, now
                        )
                        connection.execute(
                            update(articles)
                            .where(articles.c.id == str(existing["id"]))
                            .values(
                                **merged,
                                last_seen_at=now,
                                updated_at=now,
                            )
                        )
                        updated_count += 1
                        continue
                values = self._article_values(article, now)
                if existing is None:
                    connection.execute(
                        articles.insert().values(id=new_uuid7(), account_id=account_id, **values)
                    )
                    new_count += 1
                    continue
                # 摘要单调更新：补采的摘要可能为空，不能覆盖已有的非空摘要。
                stored_digest = str(existing["digest"] or "")
                if stored_digest.strip() and not article.digest.strip():
                    values["digest"] = stored_digest
                    values["content_hash"] = self._content_hash(article, str(values["published_at"]))
                updated_values = {
                    key: value
                    for key, value in values.items()
                    if key not in {"first_seen_at", "last_seen_at", "created_at", "updated_at"}
                }
                connection.execute(
                    update(articles)
                    .where(articles.c.id == str(existing["id"]))
                    .values(**updated_values, last_seen_at=now, updated_at=now)
                )
                updated_count += 1
        return new_count, updated_count

    def _find_recovered_duplicate(
        self,
        connection: Any,
        account_id: str,
        article: ArticleInput,
    ) -> Mapping[str, Any] | None:
        """找出与 ``article`` 同一篇的既有记录（跨通道），没有则返回 ``None``。

        判定条件（三条同时满足才算同一篇）：

        1. 同一账号；
        2. 标题规范化后相同；
        3. 发布时间相差不超过 ``_RECOVERY_DEDUP_SPAN_HOURS`` 小时。

        另外要求 ID 前缀不同 —— 微信读书行 ``MP_WXS_*`` 与补采行 ``MP_{biz}_*``
        才是需要合并的两套 ID；同前缀的行已被唯一约束处理过。

        第 3 条是必要的：同一个公众号会在不同日子发同名文章
        （实测量子位「量子位编辑作者招聘」10 天发了 10 条），
        只按标题去重会把历史文章误合并成一条。
        """
        if article.published_at is None:
            return None
        candidates = connection.execute(
            select(articles).where(
                articles.c.account_id == account_id,
                func.lower(articles.c.title) == func.lower(article.title),
            )
        ).mappings().all()

        incoming = self._timestamp(article.published_at)
        best: tuple[float, Mapping[str, Any]] | None = None
        for row in candidates:
            stored_sid = str(row["source_article_id"] or "")
            if stored_sid == article.source_article_id:
                continue
            same_family = stored_sid.startswith("MP_WXS_") == (
                article.source_article_id.startswith("MP_WXS_")
            )
            if same_family:
                continue
            try:
                stored = self._parse_timestamp(str(row["published_at"]))
            except Exception:  # noqa: BLE001 - 脏数据跳过即可
                continue
            span_h = abs((stored - self._parse_timestamp(incoming)).total_seconds()) / 3600.0
            if span_h > _RECOVERY_DEDUP_SPAN_HOURS:
                continue
            if best is None or span_h < best[0]:
                best = (span_h, row)
        return best[1] if best else None

    def _merge_recovered_values(
        self,
        existing: Mapping[str, Any],
        article: ArticleInput,
        now: str,
    ) -> dict[str, Any]:
        """把补采结果合并进一条已存在的记录。

        原则：**只补空缺，不覆盖**。微信读书行的 ``published_at`` 精度更高
        （带秒），补采行的链接更完整（带 ``chksm``），各有优势，所以：
        链接/封面/摘要取"更完整的一方"，发布时间保持原值。
        """
        merged: dict[str, Any] = {}

        existing_url = str(existing.get("url") or "")
        existing_cover = str(existing.get("cover_url") or "")
        existing_digest = str(existing.get("digest") or "")

        # 链接：优先带 chksm 的完整短链
        new_url = article.url or ""
        if "chksm=" in new_url and "chksm=" not in existing_url:
            merged["url"] = new_url
        elif not existing_url:
            merged["url"] = new_url

        if not existing_cover and article.cover_url:
            merged["cover_url"] = article.cover_url

        if not existing_digest.strip() and article.digest.strip():
            merged["digest"] = article.digest

        if merged:
            # content_hash 基于**合并后**的实际值重算，保证哈希与行内容一致。
            merged_article = replace(
                article,
                title=str(existing.get("title") or article.title),
                digest=str(merged.get("digest", existing_digest)),
                url=str(merged.get("url", existing_url)),
                cover_url=str(merged.get("cover_url", existing_cover)),
                published_at=self._parse_timestamp(str(existing["published_at"])),
            )
            merged["content_hash"] = self._content_hash(
                merged_article, str(existing["published_at"])
            )
        return merged

    def store_job_page(
        self,
        *,
        job_id: str,
        page_number: int,
        articles_to_store: Iterable[ArticleInput],
    ) -> PageStoreResult:
        """Persist one response for every run currently attached to a shared job."""
        article_list = list(articles_to_store)
        with self.engine.connect() as connection:
            targets = connection.execute(
                select(collection_run_accounts.c.run_id, collection_run_accounts.c.account_id)
                .where(collection_run_accounts.c.job_id == job_id)
                .order_by(collection_run_accounts.c.run_id)
            ).mappings().all()
        if not targets:
            raise RepositoryError("job is not linked to a run")
        primary: PageStoreResult | None = None
        for target in targets:
            result = self.store_page(
                run_id=str(target["run_id"]),
                account_id=str(target["account_id"]),
                page_number=page_number,
                articles_to_store=article_list,
            )
            if primary is None:
                primary = result
        assert primary is not None
        return primary

    def finalize_run(self, run_id: str, *, stop_reason_code: str = "", stop_message: str = "") -> RunSummary:
        now = self._timestamp()
        with self.engine.begin() as connection:
            run = connection.execute(select(collection_runs).where(collection_runs.c.id == run_id)).mappings().one_or_none()
            if run is None:
                raise RepositoryError("run does not exist")
            rows = connection.execute(
                select(collection_run_accounts.c.status).where(collection_run_accounts.c.run_id == run_id)
            ).mappings().all()
            statuses = [RunAccountStatus(row["status"]) for row in rows]
            planned_count = len(statuses)
            success_count = sum(status in SUCCESSFUL_ACCOUNT_STATUSES for status in statuses)
            not_started_count = sum(status == RunAccountStatus.NOT_STARTED for status in statuses)
            failed_count = sum(
                status
                in {
                    RunAccountStatus.FAILED,
                    RunAccountStatus.RATE_LIMITED,
                    RunAccountStatus.SESSION_EXPIRED,
                    RunAccountStatus.PROTOCOL_CHANGED,
                }
                for status in statuses
            )
            coverage_ratio = success_count / planned_count if planned_count else 0.0
            status = self._final_run_status(statuses)
            connection.execute(
                update(collection_runs)
                .where(collection_runs.c.id == run_id)
                .values(
                    status=status.value,
                    planned_count=planned_count,
                    success_count=success_count,
                    failed_count=failed_count,
                    not_started_count=not_started_count,
                    coverage_ratio=coverage_ratio,
                    finished_at=now,
                    stop_reason_code=stop_reason_code,
                    stop_message=stop_message,
                )
            )
            command_status = "completed"
            if status == CollectionRunStatus.REJECTED:
                command_status = "rejected"
            elif status in {
                CollectionRunStatus.FAILED,
                CollectionRunStatus.RATE_LIMITED,
                CollectionRunStatus.SESSION_EXPIRED,
                CollectionRunStatus.PROTOCOL_CHANGED,
            }:
                command_status = "failed"
            connection.execute(
                update(collection_commands)
                .where(collection_commands.c.id == run["command_id"])
                .values(status=command_status, finished_at=now, reason_code=stop_reason_code)
            )
            return RunSummary(run_id, status.value, planned_count, success_count, failed_count, not_started_count, coverage_ratio)

    def get_article(self, account_id: str, source_article_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(articles).where(
                    articles.c.account_id == account_id,
                    articles.c.source_article_id == source_article_id,
                )
            ).mappings().one_or_none()
            return dict(row) if row else None

    def list_revisions(self, article_id: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(article_revisions)
                .where(article_revisions.c.article_id == article_id)
                .order_by(article_revisions.c.revision_number)
            ).mappings()
            return [dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(collection_runs).where(collection_runs.c.id == run_id)).mappings().one_or_none()
            return dict(row) if row else None

    def enabled_account_ids(self) -> list[str]:
        with self.engine.connect() as connection:
            return [
                str(value)
                for value in connection.execute(
                    select(official_accounts.c.id)
                    .where(official_accounts.c.enabled == 1, official_accounts.c.archived_at.is_(None))
                    .order_by(official_accounts.c.display_order, official_accounts.c.id)
                ).scalars()
            ]

    def create_login_session(
        self,
        *,
        channel: str,
        created_by: str,
        expires_in_seconds: int,
    ) -> tuple[dict[str, object], bool]:
        if channel not in {"local_web", "feishu"}:
            raise RepositoryError("unsupported login channel")
        if expires_in_seconds < 30:
            raise RepositoryError("login expiry must be at least 30 seconds")
        now_value = self._now()
        now = self._timestamp(now_value)
        expires_at = self._timestamp(now_value + timedelta(seconds=expires_in_seconds))
        active_statuses = ("creating", "waiting_scan", "scanned", "confirming", "validating")
        with self.engine.begin() as connection:
            connection.execute(
                update(wechat_login_sessions)
                .where(
                    wechat_login_sessions.c.status.in_(active_statuses),
                    wechat_login_sessions.c.expires_at <= now,
                )
                .values(status="expired", completed_at=now, reason_code="QR_EXPIRED", message="二维码已过期")
            )
            existing = connection.execute(
                select(wechat_login_sessions)
                .where(wechat_login_sessions.c.status.in_(active_statuses))
                .order_by(wechat_login_sessions.c.created_at.desc())
                .limit(1)
            ).mappings().one_or_none()
            if existing is not None:
                return dict(existing), True
            session_id = new_uuid7()
            connection.execute(
                wechat_login_sessions.insert().values(
                    id=session_id,
                    channel=channel,
                    status="creating",
                    created_by=created_by,
                    created_at=now,
                    expires_at=expires_at,
                )
            )
            return {
                "id": session_id,
                "channel": channel,
                "status": "creating",
                "created_by": created_by,
                "created_at": now,
                "expires_at": expires_at,
                "completed_at": None,
                "reason_code": "",
                "message": "",
            }, False

    def get_login_session(self, session_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(wechat_login_sessions).where(wechat_login_sessions.c.id == session_id)
            ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def latest_login_session(self) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(wechat_login_sessions)
                .order_by(wechat_login_sessions.c.created_at.desc())
                .limit(1)
            ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def update_login_session(
        self,
        session_id: str,
        *,
        status: str,
        reason_code: str = "",
        message: str = "",
    ) -> dict[str, object]:
        terminal = {"succeeded", "expired", "failed", "cancelled"}
        values: dict[str, object] = {"status": status, "reason_code": reason_code, "message": message}
        if status in terminal:
            values["completed_at"] = self._timestamp()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(wechat_login_sessions).where(wechat_login_sessions.c.id == session_id).values(**values)
            )
            if result.rowcount != 1:
                raise RepositoryError("login session does not exist")
        session = self.get_login_session(session_id)
        assert session is not None
        return session

    def expire_login_sessions_after_restart(self) -> int:
        active_statuses = ("creating", "waiting_scan", "scanned", "confirming", "validating")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(wechat_login_sessions)
                .where(wechat_login_sessions.c.status.in_(active_statuses))
                .values(
                    status="expired",
                    completed_at=self._timestamp(),
                    reason_code="PROCESS_RESTARTED",
                    message="授权进程已重启，请重新生成二维码",
                )
            )
            return int(result.rowcount or 0)

    def has_running_collection_jobs(self) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(
                select(account_collection_jobs.c.id)
                .where(account_collection_jobs.c.status == "running")
                .limit(1)
            ).scalar_one_or_none() is not None

    def has_active_login_session(self) -> bool:
        active_statuses = ("creating", "waiting_scan", "scanned", "confirming", "validating")
        with self.engine.connect() as connection:
            return connection.execute(
                select(wechat_login_sessions.c.id)
                .where(wechat_login_sessions.c.status.in_(active_statuses))
                .limit(1)
            ).scalar_one_or_none() is not None

    def list_accounts(self, *, include_archived: bool = False) -> list[dict[str, object]]:
        statement = select(
            official_accounts.c.id,
            official_accounts.c.source_account_id,
            official_accounts.c.name,
            official_accounts.c.avatar_url,
            official_accounts.c.enabled,
            official_accounts.c.archived_at,
            official_accounts.c.display_order,
            official_accounts.c.watermark_article_id,
            official_accounts.c.last_success_at,
            official_accounts.c.consecutive_failure_count,
        )
        if not include_archived:
            statement = statement.where(official_accounts.c.archived_at.is_(None))
        with self.engine.connect() as connection:
            rows = connection.execute(statement.order_by(official_accounts.c.archived_at, official_accounts.c.display_order, official_accounts.c.name)).mappings()
            return [dict(row) for row in rows]

    def update_account(
        self,
        account_id: str,
        *,
        enabled: bool | None = None,
        name: str | None = None,
        display_order: int | None = None,
    ) -> dict[str, object]:
        values: dict[str, object] = {"updated_at": self._timestamp()}
        if enabled is not None:
            values["enabled"] = int(enabled)
        if name is not None:
            values["name"] = name
        if display_order is not None:
            values["display_order"] = display_order
        with self.engine.begin() as connection:
            result = connection.execute(update(official_accounts).where(official_accounts.c.id == account_id).values(**values))
            if result.rowcount != 1:
                raise RepositoryError("account does not exist")
        return next(item for item in self.list_accounts(include_archived=True) if item["id"] == account_id)

    def archive_account(self, account_id: str) -> dict[str, object]:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(official_accounts)
                .where(official_accounts.c.id == account_id, official_accounts.c.archived_at.is_(None))
                .values(archived_at=self._timestamp(), enabled=0, updated_at=self._timestamp())
            )
            if result.rowcount != 1:
                raise RepositoryError("active account does not exist")
        return next(item for item in self.list_accounts(include_archived=True) if item["id"] == account_id)

    def restore_account(self, account_id: str) -> dict[str, object]:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(official_accounts)
                .where(official_accounts.c.id == account_id, official_accounts.c.archived_at.is_not(None))
                .values(archived_at=None, enabled=0, updated_at=self._timestamp())
            )
            if result.rowcount != 1:
                raise RepositoryError("archived account does not exist")
        return next(item for item in self.list_accounts(include_archived=True) if item["id"] == account_id)

    def list_runs(self, limit: int = 50) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise RepositoryError("limit must be between 1 and 500")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(collection_runs).order_by(collection_runs.c.started_at.desc(), collection_runs.c.id.desc()).limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def list_run_accounts(self, run_id: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(collection_run_accounts, official_accounts.c.name.label("account_name"))
                .join(official_accounts, official_accounts.c.id == collection_run_accounts.c.account_id)
                .where(collection_run_accounts.c.run_id == run_id)
                .order_by(collection_run_accounts.c.plan_position)
            ).mappings()
            return [dict(row) for row in rows]

    def list_articles(self, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise RepositoryError("limit must be between 1 and 500")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(articles, official_accounts.c.name.label("account_name"))
                .join(official_accounts, official_accounts.c.id == articles.c.account_id)
                .order_by(articles.c.published_at.desc(), articles.c.id.desc())
                .limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def get_article_by_id(self, article_id: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            article = connection.execute(
                select(articles, official_accounts.c.name.label("account_name"))
                .join(official_accounts, official_accounts.c.id == articles.c.account_id)
                .where(articles.c.id == article_id)
            ).mappings().one_or_none()
            if article is None:
                return None
            result = dict(article)
            result["revisions"] = [dict(row) for row in connection.execute(
                select(article_revisions).where(article_revisions.c.article_id == article_id).order_by(article_revisions.c.revision_number)
            ).mappings()]
            return result

    def list_events(self, limit: int = 100) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(collection_events).order_by(collection_events.c.timestamp.desc()).limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def recent_requests(self, limit: int = 100) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(wechat_requests).order_by(wechat_requests.c.planned_at.desc()).limit(limit)
            ).mappings()
            return [dict(row) for row in rows]

    def _final_run_status(self, statuses: list[RunAccountStatus]) -> CollectionRunStatus:
        if not statuses:
            return CollectionRunStatus.REJECTED
        for account_status, run_status in GLOBAL_FAILURE_ACCOUNT_STATUSES:
            if account_status in statuses:
                return run_status
        if RunAccountStatus.FAILED in statuses:
            return CollectionRunStatus.FAILED
        if all(status in SUCCESSFUL_ACCOUNT_STATUSES for status in statuses):
            return CollectionRunStatus.COMPLETED
        return CollectionRunStatus.PARTIAL

    def _article_values(self, article: ArticleInput, now: str) -> dict[str, object]:
        published_at = self._timestamp(article.published_at)
        return {
            "source_article_id": article.source_article_id,
            "title": article.title,
            "digest": article.digest,
            "url": article.url,
            "cover_url": article.cover_url,
            "published_at": published_at,
            "first_seen_at": now,
            "last_seen_at": now,
            "content_hash": self._content_hash(article, published_at),
            "created_at": now,
            "updated_at": now,
        }

    def _content_hash(self, article: ArticleInput, published_at: str) -> str:
        canonical = {
            "cover_url": article.cover_url,
            "digest": article.digest,
            "published_at": published_at,
            "title": article.title,
            "url": article.url,
        }
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _source_payload_json(self, article: ArticleInput) -> str:
        allowed = {
            "source_article_id": article.source_article_id,
            "title": article.title,
            "digest": article.digest,
            "url": article.url,
            "cover_url": article.cover_url,
            "published_at": self._timestamp(article.published_at),
        }
        return json.dumps(allowed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        """Parse a stored UTC timestamp back into an aware datetime."""
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    def _timestamp(self, value: datetime | None = None) -> str:
        value = value or self._now()
        if value.tzinfo is None:
            raise RepositoryError("timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
