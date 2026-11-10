from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from wechat_list_collector.application.account_search import FakeAccountSearchCoordinator
from wechat_list_collector.application.login_coordinator import FakeLoginCoordinator
from wechat_list_collector.application.read_models import ReadModel
from wechat_list_collector.entrypoints.admin_app import create_admin_app
from wechat_list_collector.infrastructure.credential_vault import InMemoryCredentialVault
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.repository = CollectionRepository(self.engine, now=lambda: datetime(2026, 7, 30, 12, tzinfo=UTC))
        self.existing_account = self.repository.add_account(
            source_account_id="existing", fakeid_ciphertext=b"fakeid", name="Existing"
        )
        self.search_coordinator = FakeAccountSearchCoordinator()
        self.search_coordinator.account_id = self.existing_account
        self.client = TestClient(
            create_admin_app(
                self.repository,
                ReadModel(self.engine),
                InMemoryCredentialVault(),
                FakeLoginCoordinator(),
                self.search_coordinator,
                admin_password="admin-password",
                session_secret="test-session-secret",
            )
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_admin_session_csrf_and_manual_collection_command(self) -> None:
        denied = self.client.get("/api/v1/admin/accounts")
        self.assertEqual(denied.status_code, 401)
        login = self.client.post("/api/v1/admin/session", json={"password": "admin-password"})
        self.assertEqual(login.status_code, 204)
        csrf = login.headers["X-CSRF-Token"]
        accounts = self.client.get("/api/v1/admin/accounts")
        self.assertEqual(accounts.status_code, 200)
        submitted = self.client.post(
            "/api/v1/admin/collection-commands",
            json={"account_ids": [self.existing_account]},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "admin-select-1"},
        )
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(submitted.json()["items"][0]["outcome"], "enqueued")

    def test_admin_can_add_then_archive_account_without_returning_fakeid(self) -> None:
        login = self.client.post("/api/v1/admin/session", json={"password": "admin-password"})
        csrf = login.headers["X-CSRF-Token"]
        created = self.client.post(
            "/api/v1/admin/accounts",
            json={"source_account_id": "new", "fakeid": "sensitive", "name": "New account"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("fakeid", created.text.lower())
        archive = self.client.post(
            f"/api/v1/admin/accounts/{created.json()['id']}/archive",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(archive.status_code, 200)
        self.assertIsNotNone(archive.json()["archived_at"])

    def test_admin_starts_and_cancels_local_qr_session_without_exposing_secrets(self) -> None:
        login = self.client.post("/api/v1/admin/session", json={"password": "admin-password"})
        csrf = login.headers["X-CSRF-Token"]
        started = self.client.post(
            "/api/v1/admin/login-sessions",
            json={"channel": "local_web"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["status"], "waiting_scan")
        self.assertNotIn("token", started.text.lower())
        image = self.client.get(f"/api/v1/admin/login-sessions/{started.json()['id']}/qr")
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["cache-control"], "no-store, max-age=0")
        cancelled = self.client.post(
            f"/api/v1/admin/login-sessions/{started.json()['id']}/cancel",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    def test_admin_searches_by_name_then_subscribes_without_exposing_fakeid(self) -> None:
        login = self.client.post("/api/v1/admin/session", json={"password": "admin-password"})
        csrf = login.headers["X-CSRF-Token"]
        search = self.client.post(
            "/api/v1/admin/account-searches",
            json={"query": "测试"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(search.status_code, 202)
        self.assertEqual(search.json()["candidates"][0]["name"], "测试公众号")
        self.assertNotIn("fakeid", search.text.lower())
        subscribed = self.client.post(
            "/api/v1/admin/account-searches/test-account-search/subscribe",
            json={"candidate_id": "candidate-1"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(subscribed.status_code, 201)
