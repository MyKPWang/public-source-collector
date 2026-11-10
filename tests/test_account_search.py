from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from wechat_list_collector.application.account_search import AccountSearchCoordinator
from wechat_list_collector.infrastructure.db import CollectionRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.request_gate import RequestGate
from wechat_list_collector.models import Credentials


class TestVault:
    def load_wechat(self) -> Credentials:
        return Credentials("token", "cookie=value")

    @staticmethod
    def encrypt_text(value: str) -> bytes:
        return value.encode("utf-8")

    @staticmethod
    def decrypt_text(value: bytes) -> str:
        return value.decode("utf-8")


class FakeSearchGateway:
    def search_accounts(self, *, keyword: str, limit: int = 5) -> list[dict[str, object]]:
        self.keyword = keyword
        self.limit = limit
        return [
            {"fakeid": "sensitive-fakeid", "nickname": "量子位", "alias": "QbitAI", "round_head_img": "https://example.test/avatar.png"},
            {"fakeid": "sensitive-fakeid", "nickname": "重复结果"},
        ]


class AccountSearchCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.repository = CollectionRepository(self.engine)
        self.gateway = FakeSearchGateway()
        self.coordinator = AccountSearchCoordinator(
            self.repository,
            TestVault(),
            RequestGate(self.engine, minimum_interval_seconds=0, jitter_seconds=lambda: 0),
            lambda: self.gateway,
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_search_encrypts_fakeid_and_subscribes_selected_candidate(self) -> None:
        started = self.coordinator.start(query="量子位", created_by="admin")
        for _ in range(50):
            current = self.coordinator.status(str(started["id"]))
            if current and current["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        self.assertIsNotNone(current)
        self.assertEqual("succeeded", current["status"])
        self.assertEqual("量子位", current["candidates"][0]["name"])
        self.assertNotIn("fakeid", str(current).lower())
        subscribed = self.coordinator.subscribe(session_id=str(started["id"]), candidate_id="candidate-1")
        accounts = self.repository.list_accounts()
        self.assertEqual(subscribed["account_id"], accounts[0]["id"])
        self.assertEqual("QbitAI", accounts[0]["source_account_id"])
