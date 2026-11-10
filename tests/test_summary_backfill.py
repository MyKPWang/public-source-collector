from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from wechat_list_collector.application.summary_backfill import SummaryBackfiller
from wechat_list_collector.application.summary_service import SummaryFetchService
from wechat_list_collector.domain.errors import WeChatNetworkFailed
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import (
    OperationBudget,
    RequestGate,
    WEREAD_SUMMARY_OPERATION,
)


class RecordingGateway:
    """Gateway that records summary fetches and serves fixed summaries."""

    def __init__(self, summary: str = "摘要内容", empty_ids: set[str] | None = None):
        self.summary = summary
        self.empty_ids = empty_ids or set()
        self.summary_calls: list[str] = []

    def fetch_article_summary(self, review_id: str) -> str:
        self.summary_calls.append(review_id)
        if review_id in self.empty_ids:
            return ""
        return self.summary


class FailingGateway:
    def __init__(self) -> None:
        self.summary_calls: list[str] = []

    def fetch_article_summary(self, review_id: str) -> str:
        self.summary_calls.append(review_id)
        raise WeChatNetworkFailed("WEREAD_RATE_LIMITED")


class SummaryBackfillTests(unittest.TestCase):
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
        )
        # 2026-07-30 12:00 UTC -> inside the report window.
        self.now = datetime(2026, 7, 30, 12, tzinfo=UTC)
        self.command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key="backfill-command",
        )

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

    def _store_article(self, source_article_id: str, *, digest: str = "", published_at: datetime | None = None) -> None:
        from wechat_list_collector.domain.models import ArticleInput

        self.repository.store_page(
            run_id=self.command.run_id,
            account_id=self.account_id,
            page_number=1,
            articles_to_store=[
                ArticleInput(
                    source_article_id=source_article_id,
                    title=f"Title {source_article_id}",
                    digest=digest,
                    url=f"https://mp.weixin.qq.com/s/{source_article_id}",
                    cover_url="",
                    published_at=published_at or self.now,
                    source_payload={},
                )
            ],
        )

    def test_missing_digest_is_filled(self) -> None:
        self._store_article("needs-summary")
        gateway = RecordingGateway()
        gate = self._gate()
        result = SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.filled, 1)
        self.assertEqual(gateway.summary_calls, ["needs-summary"])

        stored = self.repository.list_articles()
        self.assertEqual(stored[0]["digest"], "摘要内容")

    def test_existing_digest_is_left_alone(self) -> None:
        self._store_article("has-summary", digest="已有摘要")
        gateway = RecordingGateway()
        gate = self._gate()
        result = SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(result.scanned, 0)
        self.assertEqual(result.filled, 0)
        self.assertEqual(gateway.summary_calls, [])

        stored = self.repository.list_articles()
        self.assertEqual(stored[0]["digest"], "已有摘要")

    def test_empty_summary_does_not_overwrite(self) -> None:
        self._store_article("empty-summary")
        gateway = RecordingGateway(empty_ids={"empty-summary"})
        gate = self._gate()
        result = SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(result.empty, 1)
        self.assertEqual(result.filled, 0)
        stored = self.repository.list_articles()
        self.assertEqual(stored[0]["digest"], "")

    def test_out_of_window_articles_are_not_scanned(self) -> None:
        self._store_article(
            "old-article",
            published_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
        )
        gateway = RecordingGateway()
        gate = self._gate()
        result = SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(result.scanned, 0)
        self.assertEqual(gateway.summary_calls, [])

    def test_failure_stops_the_pass_but_keeps_earlier_fills(self) -> None:
        self._store_article("first")
        self._store_article("second")
        gateway = FailingGateway()
        gate = self._gate()
        result = SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(result.filled, 0)
        self.assertTrue(result.stopped_reason)
        self.assertEqual(len(gateway.summary_calls), 1)

    def test_backfill_does_not_break_the_next_collection(self) -> None:
        """A digest written by the backfill must survive the next list pass."""
        self._store_article("stable")
        gateway = RecordingGateway()
        gate = self._gate()
        SummaryBackfiller(
            self.repository,
            SummaryFetchService(gate, gateway, minimum_interval_seconds=0),
            limit=10,
            now=self.now,
        ).run(command_id=self.command.command_id)

        self.assertEqual(self.repository.list_articles()[0]["digest"], "摘要内容")

        # A second collection of the same article carries no digest and must
        # not wipe the stored summary.
        from wechat_list_collector.domain.models import ArticleInput

        self.repository.store_page(
            run_id=self.command.run_id,
            account_id=self.account_id,
            page_number=2,
            articles_to_store=[
                ArticleInput(
                    source_article_id="stable",
                    title="Title stable",
                    digest="",
                    url="https://mp.weixin.qq.com/s/stable",
                    cover_url="",
                    published_at=self.now,
                    source_payload={},
                )
            ],
        )

        stored = self.repository.list_articles()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["digest"], "摘要内容")


if __name__ == "__main__":
    unittest.main()
