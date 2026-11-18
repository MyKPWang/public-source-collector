from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from wechat_list_collector.application.collection_runner import CollectionRunner
from wechat_list_collector.application.worker import CollectionWorker
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.domain.errors import WeChatNetworkFailed
from wechat_list_collector.infrastructure.request_gate import RequestGate
from wechat_list_collector.models import Article, ArticlePage


class PlaintextTestDecoder:
    def decrypt_text(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")


class CrashingDecoder:
    def decrypt_text(self, ciphertext: bytes) -> str:
        raise ValueError("ciphertext cannot be decrypted")


class ExplodingGateway:
    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        raise RuntimeError("gateway exploded")


class FailingGateway:
    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        raise WeChatNetworkFailed("wechat unreachable")


class SequencedGateway:
    def __init__(self, pages: list[ArticlePage]):
        self.pages = pages
        self.calls: list[tuple[int, int]] = []

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        self.calls.append((begin, count))
        return self.pages[len(self.calls) - 1]


def article(identifier: str) -> Article:
    return Article(
        id=identifier,
        feed_id="unused",
        title=f"Title {identifier}",
        url=f"https://example.invalid/{identifier}",
        digest="",
        cover_url="",
        publish_time=1_753_900_000,
    )


class CollectionRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.repository = CollectionRepository(self.engine, now=lambda: datetime(2026, 7, 30, 12, tzinfo=UTC))
        self.account_id = self.repository.add_account(
            source_account_id="source-account",
            fakeid_ciphertext=b"fakeid",
            name="Account",
            watermark_article_id="historical",
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_page_overlap_is_deduplicated_without_becoming_a_watermark(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="runner-overlap",
        )
        gateway = SequencedGateway(
            [
                ArticlePage(publish_record_count=5, articles=(article("new-a"), article("new-b"))),
                ArticlePage(publish_record_count=5, articles=(article("new-b"), article("new-c"), article("historical"))),
            ]
        )
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, gateway, PlaintextTestDecoder()),
            gate,
            worker_id="worker-test",
        )

        self.assertTrue(worker.run_once())

        run = self.repository.get_run(command.run_id)
        self.assertEqual(gateway.calls, [(0, 5), (5, 5)])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["request_count"], 2)
        self.assertEqual(run["new_article_count"], 4)

    def test_full_pages_without_frozen_watermark_finish_partial(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="runner-page-limit",
        )
        gateway = SequencedGateway(
            [
                ArticlePage(publish_record_count=5, articles=(article("a"),)),
                ArticlePage(publish_record_count=5, articles=(article("b"),)),
                ArticlePage(publish_record_count=5, articles=(article("c"),)),
            ]
        )
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, gateway, PlaintextTestDecoder()),
            gate,
            worker_id="worker-test",
        )

        worker.run_once()

        run = self.repository.get_run(command.run_id)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["stop_reason_code"], "")
        self.assertEqual(run["request_count"], 3)

    def test_account_without_fakeid_fails_without_crashing_the_worker(self) -> None:
        """池外公众号（空 fakeid）不能让整轮 worker 崩掉。"""
        pool_account_id = self.repository.add_account(
            source_account_id="pool-account",
            fakeid_ciphertext=b"",
            name="Pool Account",
        )
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[pool_account_id],
            idempotency_key="runner-empty-fakeid",
        )
        gateway = SequencedGateway([ArticlePage(publish_record_count=0, articles=())])
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, gateway, PlaintextTestDecoder()),
            gate,
            worker_id="worker-test",
        )

        self.assertTrue(worker.run_once())

        self.assertEqual(gateway.calls, [])
        run = self.repository.get_run(command.run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failed_count"], 1)
        self.assertEqual(run["request_count"], 0)
        job = self.repository.claim_next_job("worker-test-2")
        self.assertIsNone(job)

    def test_undecryptable_fakeid_fails_only_that_account(self) -> None:
        """密文解不开时，任务必须落到终态而不是把异常抛给 worker。"""
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="runner-bad-ciphertext",
        )
        gateway = SequencedGateway([ArticlePage(publish_record_count=0, articles=())])
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, gateway, CrashingDecoder()),
            gate,
            worker_id="worker-test",
        )

        self.assertTrue(worker.run_once())

        self.assertEqual(gateway.calls, [])
        run = self.repository.get_run(command.run_id)
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(self.repository.claim_next_job("worker-test-2"))

    def test_unexpected_job_error_is_contained_and_the_queue_continues(self) -> None:
        """单账号的意外异常必须收口成失败，并继续处理队列里的下一个账号。"""
        second_account_id = self.repository.add_account(
            source_account_id="second-account",
            fakeid_ciphertext=b"second-fakeid",
            name="Second Account",
        )
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id, second_account_id],
            idempotency_key="runner-crash-contains",
        )
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        crashing_worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, ExplodingGateway(), PlaintextTestDecoder()),
            gate,
            worker_id="worker-crash",
        )

        self.assertTrue(crashing_worker.run_once())

        # 崩溃的账号落到终态，并带上明确的原因码。
        failed = self._job_row(self.account_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason_code"], "COLLECTION_JOB_CRASHED")
        # 崩溃的账号没有污染队列：第二个账号仍然在排队，仍然可以被 claim。
        queued = self._job_row(second_account_id)
        self.assertEqual(queued["status"], "queued")
        claimed = self.repository.claim_next_job("worker-next")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.account_id, second_account_id)
        self.assertEqual(self.repository.get_run(command.run_id)["status"], "running")

    def test_channel_wide_failure_still_cancels_the_remaining_queue(self) -> None:
        """通道级失败（网络/冷却）仍然要取消整个待办队列。"""
        second_account_id = self.repository.add_account(
            source_account_id="second-account",
            fakeid_ciphertext=b"second-fakeid",
            name="Second Account",
        )
        self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id, second_account_id],
            idempotency_key="runner-global-failure",
        )
        gate = RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0)
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(self.repository, gate, FailingGateway(), PlaintextTestDecoder()),
            gate,
            worker_id="worker-global",
        )

        self.assertTrue(worker.run_once())

        self.assertEqual(self._job_row(self.account_id)["status"], "failed")
        self.assertEqual(self._job_row(self.account_id)["reason_code"], "WECHAT_REQUEST_FAILED")
        self.assertEqual(self._job_row(second_account_id)["status"], "cancelled")

    def test_enabled_accounts_exclude_accounts_without_fakeid(self) -> None:
        """没有 fakeid 的公众号不得进入 fakeid 采集队列，但仍保留在账号表里。"""
        pool_account_id = self.repository.add_account(
            source_account_id="pool-account",
            fakeid_ciphertext=b"",
            name="Pool Account",
        )

        enabled = self.repository.enabled_account_ids()

        self.assertIn(self.account_id, enabled)
        self.assertNotIn(pool_account_id, enabled)
        self.assertEqual(len(enabled), 1)

    def _job_row(self, account_id: str) -> dict[str, object]:
        from sqlalchemy import select

        from wechat_list_collector.infrastructure.db.schema import account_collection_jobs

        with self.engine.connect() as connection:
            row = connection.execute(
                select(account_collection_jobs).where(account_collection_jobs.c.account_id == account_id)
            ).mappings().one()
        return dict(row)
