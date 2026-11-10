import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.errors import CooldownActive
from wechat_list_collector.store import Store
from wechat_list_collector.throttle import RequestGate


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "collector.db")
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_waits_until_persistent_minimum_interval_has_elapsed(self):
        slept = []
        self.store.set_state_int("last_request_at", 100)
        gate = RequestGate(self.store, minimum_interval_seconds=30, cooldown_seconds=600, clock=lambda: 110, sleep=slept.append)
        gate.wait_for_turn()
        self.assertEqual([20], slept)
        self.assertEqual(110, self.store.get_state_int("last_request_at"))

    def test_active_cooldown_stops_before_any_request(self):
        self.store.set_state_int("cooldown_until", 500)
        gate = RequestGate(self.store, minimum_interval_seconds=30, cooldown_seconds=600, clock=lambda: 400)
        with self.assertRaises(CooldownActive):
            gate.wait_for_turn()
