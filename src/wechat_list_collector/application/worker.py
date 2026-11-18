from __future__ import annotations

import logging
import time

from ..domain.enums import RunAccountStatus
from ..infrastructure.db.repositories import CollectionRepository
from ..infrastructure.request_gate import RequestGate
from .collection_runner import AccountJobResult, CollectionRunner

# 这些原因码代表"整条通道都不通"：继续跑后面的账号只会继续撞同一堵墙，
# 因此要取消整个待办队列。账号自身的问题（没有 fakeid、密文解不开、
# 单个任务意外异常）不在此列——它们必须只结束当前账号，不能带走整轮 worker。
GLOBAL_FAILURE_REASON_CODES = frozenset(
    {
        "WECHAT_REQUEST_FAILED",
        "COLLECTION_COOLDOWN_ACTIVE",
        "WECHAT_200013",
        "WECHAT_200003",
        "WECHAT_SCHEMA_CHANGED",
        "WECHAT_ARTICLE_LIST_NOT_GRANTED",
    }
)


class CollectionWorker:
    """Single-process consumer for the durable account-job queue."""

    def __init__(self, repository: CollectionRepository, runner: CollectionRunner, gate: RequestGate, *, worker_id: str):
        self.repository = repository
        self.runner = runner
        self.gate = gate
        self.worker_id = worker_id
        self.logger = logging.getLogger(__name__)

    def recover(self) -> int:
        return self.gate.recover_unknown_requests()

    def run_once(self) -> bool:
        if self.repository.has_active_login_session():
            self.logger.info("collection_waiting_for_login", extra={"operation": "collect_account"})
            return False
        job = self.repository.claim_next_job(self.worker_id)
        if job is None:
            self.repository.finalize_ready_runs()
            return False
        try:
            result = self.runner.run(job)
        except Exception as error:  # noqa: BLE001 - 单个账号的意外异常不得终止整轮 worker
            # 任务必须在这里收口：否则 job 会一直停在 running，直到租约过期
            # 才被重新排队，而那正是"队头毒丸"卡死整个队列的机制。
            self.logger.warning(
                "account_job_crashed",
                extra={"operation": "collect_account", "account_id": job.account_id},
            )
            result = AccountJobResult(
                status=RunAccountStatus.FAILED,
                page_count=0,
                request_count=0,
                fetched_count=0,
                new_count=0,
                updated_count=0,
                reason_code="COLLECTION_JOB_CRASHED",
                message=f"{type(error).__name__}: {error}"[:500],
            )
        self.repository.finish_job(
            job_id=job.id,
            status=result.status,
            page_count=result.page_count,
            request_count=result.request_count,
            fetched_count=result.fetched_count,
            new_count=result.new_count,
            updated_count=result.updated_count,
            first_page_head_source_article_id=result.first_page_head_source_article_id,
            reason_code=result.reason_code,
            message=result.message,
        )
        if self._is_global_failure(result):
            self.repository.mark_queued_jobs_not_started(reason_code=result.reason_code, message=result.message)
        self.repository.finalize_ready_runs()
        self.logger.info(
            "account_job_finished",
            extra={"account_id": job.account_id, "reason_code": result.reason_code, "operation": "collect_account"},
        )
        return True

    @staticmethod
    def _is_global_failure(result: AccountJobResult) -> bool:
        """Only channel-wide failures cancel the rest of the queue."""
        if result.status in {
            RunAccountStatus.RATE_LIMITED,
            RunAccountStatus.SESSION_EXPIRED,
            RunAccountStatus.PROTOCOL_CHANGED,
        }:
            return True
        return result.status == RunAccountStatus.FAILED and result.reason_code in GLOBAL_FAILURE_REASON_CODES

    def run_forever(self, poll_seconds: float = 5.0) -> None:
        self.recover()
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)
