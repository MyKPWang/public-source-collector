from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from wechat_list_collector.application.collection_runner import CollectionRunner
from wechat_list_collector.application.worker import CollectionWorker
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import RequestGate
from wechat_list_collector.models import Article, ArticlePage


class PlaintextTestDecoder:
    def decrypt_text(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")


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
