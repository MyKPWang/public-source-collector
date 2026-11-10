from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_list_collector.cli import main


class DatabaseCommandTests(unittest.TestCase):
    def test_db_upgrade_creates_new_schema_without_initializing_legacy_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "collector.toml"
            database = root / "fresh.db"
            config.write_text(f"[storage]\ndatabase = '{database}'\n", encoding="utf-8")

            result = main(["--config", str(config), "db", "upgrade"])

            self.assertEqual(result, 0)
            connection = sqlite3.connect(database)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            finally:
                connection.close()
            self.assertIn("official_accounts", tables)
            self.assertNotIn("feeds", tables)
