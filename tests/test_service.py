import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.config import Settings
from wechat_list_collector.errors import WeChatRateLimited
from wechat_list_collector.models import Credentials
from wechat_list_collector.service import CollectorService
from wechat_list_collector.store import Store


class RateLimitedClient:
    def __init__(self, *_args, **_kwargs):
        self.calls = 0

    def fetch_articles(self, **_kwargs):
        self.calls += 1
        raise WeChatRateLimited("limited")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "collector.db")
        self.store.initialize()
        self.store.save_credentials(Credentials("token", "a=b"))
        self.store.add_feed("one", "One", "fake-one")
        self.store.add_feed("two", "Two", "fake-two")
        self.settings = Settings(
            database=Path(self.tempdir.name) / "collector.db",
            pages_per_feed=1,
            page_size=5,
            minimum_interval_seconds=0,
            cooldown_seconds=600,
            request_timeout_seconds=30,
        )

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_rate_limit_stops_remaining_feeds_and_persists_cooldown(self):
        service = CollectorService(self.store, self.settings, client_factory=RateLimitedClient)
        results = service.fetch_all()
        self.assertEqual(1, len(results))
        self.assertEqual("rate_limited", results[0].status)
        self.assertGreater(self.store.get_state_int("cooldown_until"), 0)
        runs = self.store.list_runs()
        self.assertEqual(1, len(runs))
        self.assertEqual("fetch_all", runs[0]["command"])
        self.assertEqual("rate_limited", runs[0]["status"])
        self.assertEqual(0, runs[0]["article_count"])
