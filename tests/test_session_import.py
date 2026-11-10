import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.session_import import import_werss_license


class SessionImportTests(unittest.TestCase):
    def test_imports_token_and_cookie_from_werss_license(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wx.lic"
            path.write_text(json.dumps({"token_data": {"token": "abc", "cookie": "a=b"}}), encoding="utf-8")
            credentials = import_werss_license(path)
        self.assertEqual("abc", credentials.token)
        self.assertEqual("a=b", credentials.cookie)
