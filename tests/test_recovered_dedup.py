"""``store_recovered_articles`` 的跨通道去重行为测试。

回归重点：同名但不同天发布的文章**不能**被合并。
（曾发生过：量子位「量子位编辑作者招聘」10 天发了 10 条，
按标题去重被误删 9 条。）
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.domain.models import ArticleInput
from wechat_list_collector.infrastructure.db import (
    CollectionRepository,
    create_sqlite_engine,
    upgrade_database,
)

ACCOUNT = "acct-1"
WEREAD = "MP_WXS_2392024520_abc123"
RECOVERED = "MP_MjM5MjAyNDUyMA==_2651116120_1"


def _input(sid: str, title: str, when: datetime, url: str = "https://x/y") -> ArticleInput:
    return ArticleInput(
        source_article_id=sid,
        title=title,
        digest="摘要",
        url=url,
        cover_url="",
        published_at=when,
        source_payload={},
    )


class RecoveredDeduplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "t.db"
        upgrade_database(db)
        self.engine = create_sqlite_engine(db)
        self.repo = CollectionRepository(self.engine)
        self.account_id = self.repo.add_account(
            source_account_id="source-account-1",
            fakeid_ciphertext=b"test-only-ciphertext",
            name="Test Account",
        )
        self.base = datetime(2026, 11, 12, 4, 0, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.engine.dispose()
        self._tmp.cleanup()

    def _count(self) -> int:
        from sqlalchemy import func, select

        from wechat_list_collector.infrastructure.db.schema import articles

        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(articles)).scalar_one()

    def test_same_title_same_time_merges(self) -> None:
        """同一篇文章被两条通道各记一次 → 合并成一行。"""
        self.repo.store_recovered_articles(
            account_id=self.account_id,
            articles_to_store=[_input(WEREAD, "同一篇", self.base)],
        )
        new, upd = self.repo.store_recovered_articles(
            account_id=self.account_id,
            articles_to_store=[
                _input(RECOVERED, "同一篇", self.base + timedelta(seconds=17),
                       url="https://x/y?chksm=abc")
            ],
        )
        self.assertEqual((new, upd), (0, 1))
        self.assertEqual(self._count(), 1)

    def test_same_title_different_day_does_not_merge(self) -> None:
        """同名但隔天的文章是**不同文章**，不能合并。"""
        self.repo.store_recovered_articles(
            account_id=self.account_id,
            articles_to_store=[_input(WEREAD, "量子位编辑作者招聘", self.base)],
        )
        # 三天后另一条同名文章
        new, upd = self.repo.store_recovered_articles(
            account_id=self.account_id,
            articles_to_store=[
                _input(RECOVERED, "量子位编辑作者招聘", self.base + timedelta(days=3))
            ],
        )
        self.assertEqual((new, upd), (1, 0))

    def test_repeated_same_title_across_days_all_kept(self) -> None:
        """连续多天发同名文章：每一条都要保留。"""
        self.repo.store_recovered_articles(
            account_id=self.account_id,
            articles_to_store=[_input(WEREAD, "同名公告", self.base)],
        )
        for day in range(1, 5):
            new, _ = self.repo.store_recovered_articles(
                account_id=self.account_id,
                articles_to_store=[
                    _input(
                        f"MP_MjM5MjAyNDUyMA==_26511161{20 + day}_1",
                        "同名公告",
                        self.base + timedelta(days=day),
                    )
                ],
            )
            self.assertEqual(new, 1)

    def test_idempotent_reapply(self) -> None:
        """同一条补采记录重复写入不产生新行。"""
        payload = [_input(RECOVERED, "幂等", self.base)]
        n1, _ = self.repo.store_recovered_articles(
            account_id=self.account_id, articles_to_store=payload)
        n2, _ = self.repo.store_recovered_articles(
            account_id=self.account_id, articles_to_store=payload)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)


if __name__ == "__main__":
    unittest.main()
