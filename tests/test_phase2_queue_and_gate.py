from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wechat_list_collector.domain.errors import CooldownActive, RequestBudgetExhausted
from wechat_list_collector.domain.models import RequestIntent
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import RequestGate


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class QueueAndRequestGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.repository = CollectionRepository(self.engine)
        self.first_account = self.repository.add_account(
            source_account_id="account-1", fakeid_ciphertext=b"encrypted-1", name="One"
        )
        self.second_account = self.repository.add_account(
            source_account_id="account-2", fakeid_ciphertext=b"encrypted-2", name="Two"
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_manual_command_promotes_queued_job_and_ignores_running_job(self) -> None:
        scheduled = self.repository.submit_collection_command(
            command_type="collect_all",
            trigger="scheduled",
            account_ids=[self.first_account],
            idempotency_key="scheduled-1",
        )
        manual = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_web",
            account_ids=[self.first_account],
            idempotency_key="manual-1",
        )
        promoted = self.repository.command_items(manual.command_id)
        self.assertEqual(promoted[0].outcome, "promoted_existing_job")
        claimed = self.repository.claim_next_job("worker-a")
        self.assertIsNotNone(claimed)
        ignored = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_web",
            account_ids=[self.first_account],
            idempotency_key="manual-2",
        )
        ignored_item = self.repository.command_items(ignored.command_id)[0]
        self.assertEqual(ignored_item.outcome, "ignored_already_running")
        self.assertIsNotNone(ignored_item.active_run_id)
        self.assertNotEqual(scheduled.run_id, ignored.run_id)

    def test_gate_persists_a_page_by_page_interval(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.first_account],
            idempotency_key="gate-command",
        )
        job = self.repository.claim_next_job("worker-a")
        clock = FakeClock()
        first_gate = RequestGate(
            self.engine,
            minimum_interval_seconds=240,
            jitter_seconds=lambda: 30,
            now=clock.now,
            sleep=clock.sleep,
        )
        first = first_gate.acquire(
            RequestIntent(command_id=command.command_id, job_id=job.id, run_id=command.run_id, account_id=job.account_id, operation="article_page", page_number=1, begin=0, count=5)
        )
        first_gate.succeed(first, publish_record_count=5, article_count=5, duration_ms=10)
        second_gate = RequestGate(
            self.engine,
            minimum_interval_seconds=240,
            jitter_seconds=lambda: 30,
            now=clock.now,
            sleep=clock.sleep,
        )
        second = second_gate.acquire(
            RequestIntent(command_id=command.command_id, job_id=job.id, run_id=command.run_id, account_id=job.account_id, operation="article_page", page_number=2, begin=5, count=5)
        )
        self.assertEqual(first.gate_wait_ms, 30_000)
        self.assertEqual(second.gate_wait_ms, 270_000)
        self.assertEqual(clock.sleeps, [30.0, 270.0])

    def test_rate_limit_opens_durable_global_cooldown(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.first_account],
            idempotency_key="cooldown-command",
        )
        job = self.repository.claim_next_job("worker-a")
        clock = FakeClock()
        gate = RequestGate(self.engine, now=clock.now, sleep=clock.sleep)
        permit = gate.acquire(
            RequestIntent(command_id=command.command_id, job_id=job.id, run_id=command.run_id, account_id=job.account_id, operation="article_page", page_number=1, begin=0, count=5)
        )
        gate.fail(permit, status="rate_limited", reason_code="WECHAT_200013", wechat_ret=200013)
        with self.assertRaises(CooldownActive):
            gate.acquire(
                RequestIntent(command_id=command.command_id, job_id=job.id, run_id=command.run_id, account_id=job.account_id, operation="article_page", page_number=2, begin=5, count=5)
            )

    def test_batch_request_limit_is_enforced_before_a_new_network_request(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.first_account],
            idempotency_key="batch-limit-command",
        )
        job = self.repository.claim_next_job("worker-a")
        clock = FakeClock()
        gate = RequestGate(self.engine, batch_limit=1, minimum_interval_seconds=0, jitter_seconds=lambda: 0, now=clock.now, sleep=clock.sleep)
        first = gate.acquire(
            RequestIntent(command_id=command.command_id, job_id=job.id, account_id=job.account_id, operation="article_page", page_number=1, begin=0, count=5)
        )
        gate.succeed(first, publish_record_count=5, article_count=1, duration_ms=1)
        with self.assertRaises(RequestBudgetExhausted):
            gate.acquire(
                RequestIntent(command_id=command.command_id, job_id=job.id, account_id=job.account_id, operation="article_page", page_number=2, begin=5, count=5)
            )

    def test_account_search_has_its_own_interval_without_delaying_collection(self) -> None:
        clock = FakeClock()
        gate = RequestGate(
            self.engine,
            minimum_interval_seconds=240,
            jitter_seconds=lambda: 30,
            now=clock.now,
            sleep=clock.sleep,
        )
        search, _ = self.repository.create_account_search_session(query="量子位", created_by="admin")
        first_search = gate.acquire(
            RequestIntent(command_id=str(search["command_id"]), operation="account_search", page_number=1, begin=0, count=5),
            operation_minimum_interval_seconds=15,
            updates_global_interval=False,
        )
        gate.succeed(first_search, publish_record_count=0, article_count=5, duration_ms=1, response_schema="searchbiz-v1")
        self.assertEqual(0, first_search.gate_wait_ms)
        self.repository.update_account_search_session(str(search["id"]), status="succeeded", message="done")

        second_search, _ = self.repository.create_account_search_session(query="新智元", created_by="admin")
        delayed_search = gate.acquire(
            RequestIntent(command_id=str(second_search["command_id"]), operation="account_search", page_number=1, begin=0, count=5),
            operation_minimum_interval_seconds=15,
            updates_global_interval=False,
        )
        self.assertEqual(15_000, delayed_search.gate_wait_ms)
        gate.succeed(delayed_search, publish_record_count=0, article_count=5, duration_ms=1, response_schema="searchbiz-v1")

        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.first_account],
            idempotency_key="search-does-not-delay-collection",
        )
        job = self.repository.claim_next_job("worker-a")
        article = gate.acquire(
            RequestIntent(command_id=command.command_id, job_id=job.id, account_id=job.account_id, operation="article_page", page_number=1, begin=0, count=5)
        )
        self.assertEqual(30_000, article.gate_wait_ms)
