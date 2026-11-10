from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_list_collector.operations import backup_database, integrity_check, restore_database


class DatabaseOperationsTests(unittest.TestCase):
    def test_backup_restore_and_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.db"
            backup = root / "backup.db"
            restored = root / "restored.db"
            connection = sqlite3.connect(original)
            try:
                connection.execute("CREATE TABLE facts(value TEXT)")
                connection.execute("INSERT INTO facts(value) VALUES ('preserved')")
                connection.commit()
            finally:
                connection.close()
            backup_database(original, backup)
            restore_database(backup, restored)
            connection = sqlite3.connect(restored)
            try:
                value = connection.execute("SELECT value FROM facts").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(value, "preserved")
            self.assertEqual(integrity_check(restored), "ok")
