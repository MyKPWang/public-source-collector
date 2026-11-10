from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from wechat_list_collector.application.read_models import ReadModel
from wechat_list_collector.domain.enums import RunAccountStatus
from wechat_list_collector.domain.models import ArticleInput
from wechat_list_collector.entrypoints.read_api import create_read_api
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database


class ReadApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        now = datetime(2026, 7, 30, 12, tzinfo=UTC)
        self.repository = CollectionRepository(self.engine, now=lambda: now)
        self.account_id = self.repository.add_account(
            source_account_id="source-account", fakeid_ciphertext=b"ciphertext", name="Account"
        )
        command = self.repository.submit_collection_command(
            command_type="collect_all",
            trigger="scheduled",
            account_ids=[self.account_id],
            idempotency_key="scheduled-read-api",
            business_date="2026-07-30",
        )
        self.repository.start_run(command.run_id)
        self.repository.store_page(
            run_id=command.run_id,
            account_id=self.account_id,
            page_number=1,
            articles_to_store=[
                self._article("a", "A"),
                self._article("b", "B"),
            ],
        )
        self.repository.set_run_account_status(
            run_id=command.run_id, account_id=self.account_id, status=RunAccountStatus.COMPLETED
        )
        self.repository.finalize_run(command.run_id)
        self.client = TestClient(
            create_read_api(ReadModel(self.engine, now=lambda: now), bearer_token="pipeline-token")
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_pipeline_article_cursor_and_batch_quality(self) -> None:
        response = self.client.get(
            "/api/v1/pipeline/articles",
            params={
                "published_from": "2026-07-29T00:00:00+08:00",
                "published_before": "2026-07-31T00:00:00+08:00",
                "limit": 1,
            },
            headers={"Authorization": "Bearer pipeline-token"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["batch"]["status"], "completed")
        self.assertEqual(payload["batch"]["coverage_ratio"], 1.0)
        self.assertTrue(payload["page"]["has_more"])
        second = self.client.get(
            "/api/v1/pipeline/articles",
            params={
                "published_from": "2026-07-29T00:00:00+08:00",
                "published_before": "2026-07-31T00:00:00+08:00",
                "limit": 1,
                "cursor": payload["page"]["next_cursor"],
            },
            headers={"Authorization": "Bearer pipeline-token"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.json()["articles"][0]["id"], payload["articles"][0]["id"])

    def test_read_api_requires_token_and_rejects_invalid_cursor(self) -> None:
        denied = self.client.get("/api/v1/pipeline/accounts")
        self.assertEqual(denied.status_code, 401)
        invalid = self.client.get(
            "/api/v1/pipeline/articles",
            params={
                "published_from": "2026-07-29T00:00:00+08:00",
                "published_before": "2026-07-31T00:00:00+08:00",
                "cursor": "not-a-cursor",
            },
            headers={"Authorization": "Bearer pipeline-token"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_CURSOR")

    @staticmethod
    def _article(source_article_id: str, title: str) -> ArticleInput:
        return ArticleInput(
            source_article_id=source_article_id,
            title=title,
            digest="",
            url=f"https://example.invalid/{source_article_id}",
            cover_url="",
            published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
            source_payload={},
        )
