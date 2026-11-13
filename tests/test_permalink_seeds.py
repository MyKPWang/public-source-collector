"""回归测试：永久链接种子。

背景：恢复链路原先只用 ``MP_WXS_*`` 短链做种子，广度优先扩展到某一层就停住，
因为短链页面里的「🔗 相关阅读」只覆盖作者手动挑过的文章。库里已有的补采记录
自带完整永久链接（含 ``sn``），把它们也当种子，扩展就能继续往下走。

新增后 APPSO 一次补采多出 22 篇文章，因此这个行为需要锁死。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
    PermalinkCoords,
    PermalinkRecoveryRunner,
)

ACCOUNT_ID = "acct-permalink-seeds"


class _FakeClient:
    """记录 runner 实际抓过的坐标。"""

    def __init__(self) -> None:
        self.fetched_permalinks: list[str] = []

    def fetch_permalink(self, coords: PermalinkCoords):
        self.fetched_permalinks.append(coords.key())
        return {"title": f"标题-{coords.mid}"}, [], None


class _FakeRunner(PermalinkRecoveryRunner):
    def __init__(self, client) -> None:  # noqa: D107 - 测试替身
        self._client = client
        self.visited: set = set()
        self.expanded: list = []

    def _expand(self, queue, report):  # noqa: D107 - 简化扩展
        self.expanded = list(queue)


class PermalinkSeedTests(unittest.TestCase):
    def test_permalink_seeds_are_queued(self):
        client = _FakeClient()
        runner = _FakeRunner(client)
        seed = PermalinkCoords(biz="MjM5", mid="2652396877", idx="2", sn="sn-a")
        report = runner.run([], permalink_seeds=[seed])
        self.assertEqual(1, report.seeds)
        self.assertEqual(1, len(runner.expanded))
        self.assertEqual(("MjM5", "2652396877", "2", "sn-a"), runner.expanded[0][1].key())

    def test_duplicate_permalink_seed_is_skipped(self):
        runner = _FakeRunner(_FakeClient())
        seed = PermalinkCoords(biz="MjM5", mid="1", idx="1", sn="sn")
        runner.run([], permalink_seeds=[seed, seed])
        self.assertEqual(1, len(runner.expanded))

    def test_short_link_path_still_works(self):
        class _ShortClient(_FakeClient):
            def fetch_short_link(self, url):
                return {"title": "短链标题"}, [], None

        runner = _FakeRunner(_ShortClient())
        report = runner.run([("MjM5", "https://mp.weixin.qq.com/s/abc")])
        self.assertEqual(1, report.seeds)
        self.assertEqual(1, report.fetched)


class LoadPermalinkSeedDbTests(unittest.TestCase):
    """``load_permalink_seeds`` 从库里已存的永久链接取种子。"""

    def _make_db(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "collector.db"
        connection = sqlite3.connect(db)
        connection.execute(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                account_id TEXT,
                title TEXT,
                url TEXT,
                published_at TEXT,
                source_article_id TEXT,
                source_deleted INTEGER DEFAULT 0
            )
            """
        )
        connection.executemany(
            "INSERT INTO articles (account_id,title,url,published_at,source_article_id)"
            " VALUES (?,?,?,?,?)",
            [
                (
                    ACCOUNT_ID,
                    "有 sn 的文章",
                    "https://mp.weixin.qq.com/s?__biz=MjM5&mid=2652396877&idx=2&sn=sn-a",
                    "2026-11-12T01:00:00+00:00",
                    "MP_MjM5_2652396877_2",
                ),
                (
                    ACCOUNT_ID,
                    "没有 sn 的短链文章",
                    "https://mp.weixin.qq.com/s/shortonly",
                    "2026-11-12T02:00:00+00:00",
                    "MP_WXS_MjM5_abc",
                ),
                (
                    "acct-other",
                    "别的账号的文章",
                    "https://mp.weixin.qq.com/s?__biz=Other&mid=1&idx=1&sn=sn-b",
                    "2026-11-12T03:00:00+00:00",
                    "MP_Other_1_1",
                ),
            ],
        )
        connection.commit()
        connection.close()
        return db

    def test_load_permalink_seeds_filters(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        from recover_missing_articles import load_permalink_seeds

        db = self._make_db()
        seeds = load_permalink_seeds(db, ACCOUNT_ID, 10)
        keys = {s.key() for s in seeds}
        self.assertIn(("MjM5", "2652396877", "2", "sn-a"), keys)
        self.assertNotIn(("Other", "1", "1", "sn-b"), keys)
        self.assertEqual(1, len(seeds))


if __name__ == "__main__":
    unittest.main()
