from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from wechat_list_collector.application.collection_runner import CollectionRunner
from wechat_list_collector.application.summary_enricher import SummaryEnricher
from wechat_list_collector.application.summary_service import SummaryFetchService
from wechat_list_collector.application.worker import CollectionWorker
from wechat_list_collector.domain.errors import WeChatNetworkFailed
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import (
    OperationBudget,
    RequestGate,
    WEREAD_SUMMARY_OPERATION,
)
from wechat_list_collector.models import Article, ArticlePage


class PlaintextTestDecoder:
    def decrypt_text(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")


class RecordingGateway:
    """Gateway that serves one page and records summary fetches."""

    def __init__(self, page: ArticlePage):
        self.page = page
        self.summary_calls: list[str] = []

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        return self.page

    def fetch_article_summary(self, review_id: str) -> str:
        self.summary_calls.append(review_id)
        return f"摘要-{review_id}"


class FailingSummaryGateway(RecordingGateway):
    def fetch_article_summary(self, review_id: str) -> str:
        self.summary_calls.append(review_id)
        raise WeChatNetworkFailed("WEREAD_RATE_LIMITED")


def make_article(identifier: str, *, publish_time: int, digest: str = "") -> Article:
    return Article(
        id=identifier,
        feed_id="unused",
        title=f"Title {identifier}",
        url=f"https://mp.weixin.qq.com/s/{identifier}",
        digest=digest,
        cover_url="",
        publish_time=publish_time,
    )


class SummaryEnricherTests(unittest.TestCase):
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
        # 2026-07-30 12:00 UTC -> inside the report window (2026-07-29 00:00 .. 2026-07-31 00:00 Shanghai)
        self.now = datetime(2026, 7, 30, 12, tzinfo=UTC)
        self.in_window = int(self.now.timestamp())
        self.out_of_window = int(datetime(2026, 6, 1, 12, tzinfo=UTC).timestamp())

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _gate(self) -> RequestGate:
        return RequestGate(
            self.engine,
            channel="weread_reader",
            minimum_interval_seconds=0,
            jitter_seconds=lambda: 0,
            operation_budgets={
                WEREAD_SUMMARY_OPERATION: OperationBudget(rolling_window_limit=200, batch_limit=60)
            },
        )

    def test_missing_summary_is_filled_and_persisted(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="enrich-success",
        )
        gateway = RecordingGateway(
            ArticlePage(
                publish_record_count=1,
                articles=(make_article("in-window", publish_time=self.in_window),),
            )
        )
        gate = self._gate()
        enricher = SummaryEnricher(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            max_items_per_page=3,
            now=lambda: self.now,
        )
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(
                self.repository,
                gate,
                gateway,
                PlaintextTestDecoder(),
                publish_record_page_size=5,
                max_pages=1,
                summary_enricher=enricher,
            ),
            gate,
            worker_id="worker-test",
        )

        self.assertTrue(worker.run_once())

        stored = self.repository.list_articles()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["digest"], "摘要-in-window")
        self.assertEqual(gateway.summary_calls, ["in-window"])

        run = self.repository.get_run(command.run_id)
        self.assertEqual(run["status"], "completed")

    def test_summary_failure_still_stores_the_article(self) -> None:
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="enrich-failure",
        )
        gateway = FailingSummaryGateway(
            ArticlePage(
                publish_record_count=1,
                articles=(make_article("in-window", publish_time=self.in_window),),
            )
        )
        gate = self._gate()
        enricher = SummaryEnricher(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            max_items_per_page=3,
            now=lambda: self.now,
        )
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(
                self.repository,
                gate,
                gateway,
                PlaintextTestDecoder(),
                publish_record_page_size=5,
                max_pages=1,
                summary_enricher=enricher,
            ),
            gate,
            worker_id="worker-test",
        )

        self.assertTrue(worker.run_once())

        stored = self.repository.list_articles()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["digest"], "")
        self.assertEqual(gateway.summary_calls, ["in-window"])

        run = self.repository.get_run(command.run_id)
        self.assertEqual(run["status"], "completed")

    def test_out_of_window_articles_are_not_enriched(self) -> None:
        self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="enrich-window",
        )
        gateway = RecordingGateway(
            ArticlePage(
                publish_record_count=1,
                articles=(make_article("old", publish_time=self.out_of_window),),
            )
        )
        gate = self._gate()
        enricher = SummaryEnricher(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            max_items_per_page=3,
            now=lambda: self.now,
        )
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(
                self.repository,
                gate,
                gateway,
                PlaintextTestDecoder(),
                publish_record_page_size=5,
                max_pages=1,
                summary_enricher=enricher,
            ),
            gate,
            worker_id="worker-test",
        )

        worker.run_once()

        self.assertEqual(gateway.summary_calls, [])
        stored = self.repository.list_articles()
        self.assertEqual(len(stored), 1)

    def test_stored_summary_is_not_refetched(self) -> None:
        self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="enrich-dedup",
        )
        gateway = RecordingGateway(
            ArticlePage(
                publish_record_count=1,
                articles=(make_article("in-window", publish_time=self.in_window),),
            )
        )
        gate = self._gate()
        enricher = SummaryEnricher(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            max_items_per_page=3,
            now=lambda: self.now,
        )
        worker = CollectionWorker(
            self.repository,
            CollectionRunner(
                self.repository,
                gate,
                gateway,
                PlaintextTestDecoder(),
                publish_record_page_size=5,
                max_pages=1,
                summary_enricher=enricher,
            ),
            gate,
            worker_id="worker-test",
        )

        worker.run_once()
        self.assertEqual(gateway.summary_calls, ["in-window"])

        # Second collection of the same article must not spend another request.
        self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="enrich-dedup-2",
        )
        worker.run_once()
        self.assertEqual(gateway.summary_calls, ["in-window"])

        stored = self.repository.list_articles()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["digest"], "摘要-in-window")


if __name__ == "__main__":
    unittest.main()
