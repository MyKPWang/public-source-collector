from __future__ import annotations

import logging
import time

from ..domain.enums import RunAccountStatus
from ..infrastructure.db.repositories import CollectionRepository
from ..infrastructure.request_gate import RequestGate
from .collection_runner import CollectionRunner


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
        result = self.runner.run(job)
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
        if result.status in {
            RunAccountStatus.RATE_LIMITED,
            RunAccountStatus.SESSION_EXPIRED,
            RunAccountStatus.PROTOCOL_CHANGED,
            RunAccountStatus.FAILED,
        }:
            self.repository.mark_queued_jobs_not_started(reason_code=result.reason_code, message=result.message)
        self.repository.finalize_ready_runs()
        self.logger.info(
            "account_job_finished",
            extra={"account_id": job.account_id, "reason_code": result.reason_code, "operation": "collect_account"},
        )
        return True

    def run_forever(self, poll_seconds: float = 5.0) -> None:
        self.recover()
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)
