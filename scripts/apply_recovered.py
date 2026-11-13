"""把已恢复的文章写进生产库，并核对入库结果。

用法：.venv/bin/python scripts/apply_recovered.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wechat_list_collector.infrastructure.db import (  # noqa: E402
    CollectionRepository,
    create_sqlite_engine,
)
from wechat_list_collector.infrastructure.wechat.permalink_recovery import (  # noqa: E402
    RecoveredArticle,
    to_article_inputs,
)

SCRATCH = Path("/Users/wangkp/.hermes/cache/scratch")
ACCT = "019fb40f-112c-7fc4-9c9a-bf844188e0f5"
DB = Path("/Users/wangkp/Developer/public-source-collector/data/collector.db")


def main() -> int:
    data = json.loads((SCRATCH / "prod_recovery_titles.json").read_text(encoding="utf-8"))
    articles = [
        RecoveredArticle(
            title=d["title"],
            url=d["url"],
            digest=d.get("digest", ""),
            cover_url=d.get("cover", ""),
            published_at=d.get("published_at", ""),
            biz=d.get("biz", ""),
            mid=d.get("mid", ""),
            idx=d.get("idx", "1"),
            source=d.get("source", "short"),
        )
        for d in data
    ]
    inputs = list(to_article_inputs(articles, account_id=ACCT))
    print("待写入:", len(inputs))

    engine = create_sqlite_engine(DB)
    repository = CollectionRepository(engine)
    new_count, updated_count = repository.store_recovered_articles(
        account_id=ACCT, articles_to_store=inputs
    )
    print(f"首次写入：新增 {new_count}，更新 {updated_count}")

    # 幂等验证：再跑一次，新增必须是 0
    new2, updated2 = repository.store_recovered_articles(
        account_id=ACCT, articles_to_store=inputs
    )
    print(f"二次写入：新增 {new2}，更新 {updated2}（幂等要求 new2 == 0）")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    total = con.execute(
        "SELECT COUNT(*) FROM articles WHERE account_id=?", (ACCT,)).fetchone()[0]
    recent = con.execute(
        """SELECT published_at, title FROM articles WHERE account_id=?
           ORDER BY published_at DESC LIMIT 12""", (ACCT,)).fetchall()
    con.close()
    print(f"\n生产库 APPSO 文章总数: {total}")
    print("最新 12 条:")
    for t, title in recent:
        print(f"  {t} | {title[:66]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
