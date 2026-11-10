from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from wechat_list_collector.domain.enums import RunAccountStatus
from wechat_list_collector.domain.models import ArticleInput
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database


class CollectionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(self.database)
        self.current_time = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        self.repository = CollectionRepository(
            create_sqlite_engine(self.database),
            now=lambda: self.current_time,
        )
        self.account_id = self.repository.add_account(
            source_account_id="source-account-1",
            fakeid_ciphertext=b"test-only-ciphertext",
            name="Test Account",
        )

    def tearDown(self) -> None:
        self.repository.engine.dispose()
        self.temp_dir.cleanup()

    def test_upgrade_creates_alembic_managed_core_tables(self) -> None:
        connection = sqlite3.connect(self.database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(revision, "20260810_10")
        self.assertTrue(
            {
                "official_accounts",
                "articles",
                "article_revisions",
                "collection_commands",
                "collection_runs",
                "collection_run_accounts",
                "collection_run_articles",
                "wechat_login_sessions",
            }.issubset(tables)
        )

    def test_login_session_is_metadata_only_and_expires_on_restart(self) -> None:
        session, reused = self.repository.create_login_session(
            channel="local_web",
            created_by="admin",
            expires_in_seconds=300,
        )
        self.assertFalse(reused)
        self.assertEqual(session["status"], "creating")
        self.assertNotIn("token", session)
        self.assertNotIn("cookie", session)
        self.assertTrue(self.repository.has_active_login_session())
        self.assertEqual(self.repository.expire_login_sessions_after_restart(), 1)
        expired = self.repository.get_login_session(str(session["id"]))
        self.assertEqual(expired["status"], "expired")
        self.assertEqual(expired["reason_code"], "PROCESS_RESTARTED")

    def test_idempotent_command_creates_one_run_and_immutable_plan(self) -> None:
        first = self.repository.submit_collection_command(
            command_type="collect_selected",
            trigger="manual_web",
            account_ids=[self.account_id],
            idempotency_key="manual-selection-1",
            submitted_by="admin",
        )
        repeated = self.repository.submit_collection_command(
            command_type="collect_selected",
            trigger="manual_web",
            account_ids=[self.account_id],
            idempotency_key="manual-selection-1",
            submitted_by="admin",
        )
        run = self.repository.get_run(first.run_id)
        self.assertFalse(first.reused)
        self.assertTrue(repeated.reused)
        self.assertEqual(first.command_id, repeated.command_id)
        self.assertEqual(first.run_id, repeated.run_id)
        self.assertEqual(run["planned_count"], 1)
        self.assertEqual(run["status"], "queued")

    def test_article_upsert_revisions_and_observations(self) -> None:
        command = self._new_run()
        original = self._article("A", digest="first digest")
        first = self.repository.store_page(
            run_id=command.run_id,
            account_id=self.account_id,
            page_number=1,
            articles_to_store=[original],
        )
        duplicate = self.repository.store_page(
            run_id=command.run_id,
            account_id=self.account_id,
            page_number=2,
            articles_to_store=[original],
        )
        article_before_change = self.repository.get_article(self.account_id, "A")
        self.current_time = datetime(2026, 7, 30, 13, 0, tzinfo=UTC)
        changed = self.repository.store_page(
            run_id=command.run_id,
            account_id=self.account_id,
            page_number=3,
            articles_to_store=[self._article("A", digest="changed digest")],
        )
        article = self.repository.get_article(self.account_id, "A")
        revisions = self.repository.list_revisions(str(article["id"]))

        self.assertEqual((first.new_count, first.updated_count, first.observed_count), (1, 0, 1))
        self.assertEqual((duplicate.new_count, duplicate.updated_count, duplicate.observed_count), (0, 0, 0))
        self.assertEqual((changed.new_count, changed.updated_count, changed.observed_count), (0, 1, 0))
        self.assertEqual(article["digest"], "changed digest")
        self.assertEqual(article["first_seen_at"], article_before_change["first_seen_at"])
        self.assertNotEqual(article["last_seen_at"], article_before_change["last_seen_at"])
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["digest"], "first digest")
        self.assertEqual(revisions[0]["revision_number"], 1)

    def test_final_summary_is_calculated_from_run_accounts(self) -> None:
        second_account = self.repository.add_account(
            source_account_id="source-account-2",
            fakeid_ciphertext=b"test-only-ciphertext-2",
            name="Second Account",
        )
        command = self.repository.submit_collection_command(
            command_type="collect_all",
            trigger="scheduled",
            account_ids=[self.account_id, second_account],
            idempotency_key="scheduled-2026-07-30",
            business_date="2026-07-30",
        )
        self.repository.start_run(command.run_id)
        self.repository.set_run_account_status(
            run_id=command.run_id,
            account_id=self.account_id,
            status=RunAccountStatus.COMPLETED,
        )
        self.repository.set_run_account_status(
            run_id=command.run_id,
            account_id=second_account,
            status=RunAccountStatus.PAGE_LIMIT_REACHED,
            reason_code="PAGE_LIMIT_REACHED",
        )
        summary = self.repository.finalize_run(command.run_id)
        self.assertEqual(summary.status, "partial")
        self.assertEqual(summary.success_count, 1)
        self.assertEqual(summary.failed_count, 0)
        self.assertEqual(summary.not_started_count, 0)
        self.assertEqual(summary.coverage_ratio, 0.5)
        self.assertEqual(self.repository.get_run(command.run_id)["status"], "partial")

    def test_failed_page_transaction_does_not_store_a_partial_page(self) -> None:
        command = self._new_run()
        invalid = ArticleInput(
            source_article_id="bad",
            title="Bad timestamp",
            digest="",
            url="https://example.invalid/bad",
            cover_url="",
            published_at=datetime(2026, 7, 30, 12, 0),
            source_payload={},
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.repository.store_page(
                run_id=command.run_id,
                account_id=self.account_id,
                page_number=1,
                articles_to_store=[self._article("good"), invalid],
            )
        self.assertIsNone(self.repository.get_article(self.account_id, "good"))

    def _new_run(self):
        command = self.repository.submit_collection_command(
            command_type="collect_account",
            trigger="manual_cli",
            account_ids=[self.account_id],
            idempotency_key=f"command-{self._testMethodName}",
        )
        self.repository.start_run(command.run_id)
        self.repository.set_run_account_status(
            run_id=command.run_id,
            account_id=self.account_id,
            status=RunAccountStatus.RUNNING,
        )
        return command

    @staticmethod
    def _article(source_article_id: str, *, digest: str = "digest") -> ArticleInput:
        return ArticleInput(
            source_article_id=source_article_id,
            title="Article title",
            digest=digest,
            url=f"https://example.invalid/{source_article_id}",
            cover_url="https://example.invalid/cover.jpg",
            published_at=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
            source_payload={"ignored_untrusted_field": "ignored"},
        )
