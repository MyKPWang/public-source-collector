"""``recover_missing_articles.py`` 的 ``--apply`` 写库路径回归测试。

回归重点：``to_article_inputs`` 需要 ``account_id`` 关键字参数。
曾因调用处漏传导致 ``--apply`` 抛 ``TypeError``，结果是恢复任务
「跑完了但一篇文章都没写进库」，而且异常发生在抓取之后（浪费一轮请求）。

另外验证：默认**不带** ``--apply`` 时是演练模式，不碰生产库。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import recover_missing_articles as rma  # noqa: E402

from wechat_list_collector.infrastructure.db import (  # noqa: E402
    CollectionRepository,
    create_sqlite_engine,
    upgrade_database,
)
from wechat_list_collector.infrastructure.wechat.permalink_recovery import (  # noqa: E402
    RecoveredArticle,
    to_article_inputs,
)


class ApplyPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "collector.db"
        upgrade_database(self.db)
        engine = create_sqlite_engine(self.db)
        self.repo = CollectionRepository(engine)
        self.account_id = self.repo.add_account(
            source_account_id="acct-src",
            fakeid_ciphertext=b"test-only-ciphertext",
            name="Test Account",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _article(self, title: str = "补回的文章") -> RecoveredArticle:
        return RecoveredArticle(
            title=title,
            url="https://mp.weixin.qq.com/s/abc123",
            digest="摘要",
            cover_url="https://cdn.example/cover.jpg",
            published_at="2026-11-12 08:30:00",
            biz="MjM5",
            mid="2651116120",
            idx="1",
            source="related",
        )

    def test_to_article_inputs_requires_account_id(self) -> None:
        """漏传 account_id 必须立刻报 TypeError（而不是静默写错账号）。"""
        with self.assertRaises(TypeError):
            list(to_article_inputs([self._article()]))  # type: ignore[call-arg]

    def test_apply_to_production_writes_rows(self) -> None:
        """``apply_to_production`` 必须真的写进库，且返回 (新增, 更新)。"""
        new_count, updated_count = rma.apply_to_production(
            self.db, self.account_id, [self._article()]
        )
        self.assertEqual(new_count, 1)
        self.assertEqual(updated_count, 0)

        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT title, account_id FROM articles"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "补回的文章")
        self.assertEqual(rows[0][1], self.account_id)

    def test_apply_to_production_is_idempotent(self) -> None:
        """重复写入同一篇 → 新增 0，不再产生重复行。"""
        article = self._article()
        first = rma.apply_to_production(self.db, self.account_id, [article])
        second = rma.apply_to_production(self.db, self.account_id, [article])
        self.assertEqual(first, (1, 0))
        self.assertEqual(second[0], 0)

        connection = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
