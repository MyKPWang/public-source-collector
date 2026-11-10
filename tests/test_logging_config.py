import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.config import Settings
from wechat_list_collector.logging_config import configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_writes_structured_run_event_to_log_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                database=root / "collector.db",
                pages_per_feed=1,
                page_size=5,
                minimum_interval_seconds=0,
                cooldown_seconds=600,
                request_timeout_seconds=30,
                log_file=root / "collector.log",
            )
            logger = configure_logging(settings)
            logger.info("run_started", extra={"run_id": "run-1", "operation": "fetch_all", "count": 2})
            for handler in logger.handlers:
                handler.flush()
            contents = settings.log_file.read_text(encoding="utf-8")
        self.assertIn('"event":"run_started"', contents)
        self.assertIn('"run_id":"run-1"', contents)
        self.assertNotIn("token", contents)

    def tearDown(self):
        logger = logging.getLogger("wechat_list_collector")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
