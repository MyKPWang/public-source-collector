#!/usr/bin/env python3
"""补采脚本：从生产库已有文章派生官方短链，恢复被微信读书漏掉的文章。

用法::

    .venv/bin/python scripts/recover_missing_articles.py --dry-run
    .venv/bin/python scripts/recover_missing_articles.py --apply

原理
----
微信读书对单个公众号账号有服务端展示上限（实测 APPSO 每天 3 条），会稳定
漏掉同一公众号当天发布的其余文章。本脚本不依赖微信读书接口：

1. 从生产库已有的 ``source_article_id``（形如 ``MP_WXS_{uin}_{shortid}``）
   派生官方短链 ``https://mp.weixin.qq.com/s/{shortid}``；
2. 短链无需鉴权，页面内直接给出 ``var mid/idx/sn/biz`` 与正文里的
   「🔗 相关阅读」永久链接；
3. 带账号级 ``chksm`` 请求永久链接，取回漏采文章的标题/摘要/封面/时间；
4. 幂等写回生产库（``uq_articles_account_source`` 唯一约束保证不重复）。

``chksm`` 由脚本自动学习并加密落盘到 ``data/wechat_tokens.enc``，
不写进源码、不写进日志。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wechat_list_collector.infrastructure.credential_vault import (  # noqa: E402
    EncryptedFileCredentialVault,
)
from wechat_list_collector.infrastructure.wechat.permalink_recovery import (  # noqa: E402
    PermalinkRecoveryClient,
    PermalinkRecoveryRunner,
    PermalinkTokenStore,
    to_article_inputs,
)

LOGGER = logging.getLogger("recover_missing_articles")

_SHORT_ID_RE = re.compile(r"^MP_WXS_[^_]+_(.+)$")


def derive_short_links(source_article_ids: list[str]) -> list[str]:
    """从 ``MP_WXS_{uin}_{shortid}`` 派生官方短链。

    短 id 里的 ``~`` 必须换成 ``_``：微信短链字符集只有 ``[A-Za-z0-9_-]``，
    带 ``~`` 的形式访问会直接返回「参数错误」。
    """
    links: list[str] = []
    seen: set[str] = set()
    for source_id in source_article_ids:
        match = _SHORT_ID_RE.match(str(source_id or ""))
        if not match:
            continue
        short_id = match.group(1).replace("~", "_")
        if not short_id or short_id in seen:
            continue
        seen.add(short_id)
        links.append(f"https://mp.weixin.qq.com/s/{short_id}")
    return links


def load_accounts(database: Path) -> list[dict[str, str]]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT a.id AS account_id, a.name AS name, a.source_account_id AS source_account_id
            FROM official_accounts a
            WHERE a.enabled = 1 AND a.archived_at IS NULL
            ORDER BY a.display_order, a.name
            """
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def load_seed_source_ids(database: Path, account_id: str, limit: int) -> list[str]:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT source_article_id FROM articles
            WHERE account_id = ? AND source_deleted = 0
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def load_permalink_seeds(database: Path, account_id: str, limit: int) -> list:
    """从库里已有的补采记录取出永久链接坐标（``biz/mid/idx/sn``）当种子。

    只用短链做种子时扩展会在某一层停住；库里已存的补采记录自带完整永久链接
    （含 ``sn``），把它们也当种子，广度优先就能继续往下走。
    """
    import urllib.parse

    from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
        PermalinkCoords,
    )

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT url FROM articles
            WHERE account_id = ? AND source_deleted = 0
              AND url LIKE 'https://mp.weixin.qq.com/s?%'
            ORDER BY published_at DESC
            LIMIT ?
            """,
            (account_id, limit),
        ).fetchall()
    finally:
        connection.close()

    seeds = []
    for (url,) in rows:
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(str(url)).query)
        except ValueError:
            continue
        biz = (params.get("__biz") or [""])[0]
        mid = (params.get("mid") or [""])[0]
        idx = (params.get("idx") or ["1"])[0]
        sn = (params.get("sn") or [""])[0]
        if not (biz and mid and sn):
            continue
        seeds.append(
            PermalinkCoords(
                biz=urllib.parse.unquote(biz),
                mid=mid,
                idx=idx,
                sn=sn,
            )
        )
    return seeds


def apply_to_production(
    database: Path,
    account_id: str,
    articles,
) -> tuple[int, int]:
    """幂等写回生产库。"""
    from wechat_list_collector.infrastructure.db import (
        CollectionRepository,
        create_sqlite_engine,
    )

    engine = create_sqlite_engine(database)
    repository = CollectionRepository(engine)
    return repository.store_recovered_articles(
        account_id=account_id,
        articles_to_store=list(to_article_inputs(articles, account_id=account_id)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补采被微信读书漏掉的公众号文章")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "collector.toml")
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--account-id", default=None, help="只处理指定账号")
    parser.add_argument("--seed-limit", type=int, default=60, help="每个账号用多少条已有文章做种子")
    parser.add_argument("--max-articles", type=int, default=120)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省只做演练")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from wechat_list_collector.config import load_settings

    settings = load_settings(args.config)
    database = args.database or Path(settings.database)
    credentials = args.credentials or (database.parent / "credentials.enc")

    vault = EncryptedFileCredentialVault(credentials)
    tokens = PermalinkTokenStore(vault)
    client = PermalinkRecoveryClient(tokens)
    runner = PermalinkRecoveryRunner(client, max_articles=args.max_articles, max_depth=args.max_depth)

    accounts = load_accounts(database)
    if args.account_id:
        accounts = [a for a in accounts if a["account_id"] == args.account_id]
    if not accounts:
        LOGGER.error("没有匹配到账号")
        return 2

    total_new = 0
    total_updated = 0
    for account in accounts:
        account_id = account["account_id"]
        source_ids = load_seed_source_ids(database, account_id, args.seed_limit)
        short_links = derive_short_links(source_ids)
        if not short_links:
            LOGGER.info("[%s] 没有可用种子（source_article_id 不是 MP_WXS_* 形态）", account["name"])
            continue
        LOGGER.info("[%s] 种子 %d 条，开始补采", account["name"], len(short_links))
        permalink_seeds = load_permalink_seeds(database, account_id, args.seed_limit)
        if permalink_seeds:
            LOGGER.info(
                "[%s] 另有 %d 条已存永久链接可作种子", account["name"], len(permalink_seeds)
            )
        report = runner.run(
            [(account["source_account_id"], link) for link in short_links],
            permalink_seeds=permalink_seeds,
        )
        LOGGER.info(
            "[%s] 抓到 %d 篇，失败 %d 篇", account["name"], report.fetched, report.failed
        )
        if not report.new_articles:
            continue
        if args.apply:
            new_count, updated_count = apply_to_production(database, account_id, report.new_articles)
            total_new += new_count
            total_updated += updated_count
            LOGGER.info("[%s] 写库：新增 %d，更新 %d", account["name"], new_count, updated_count)
        else:
            LOGGER.info("[%s] 演练：%d 篇待写入", account["name"], len(report.new_articles))
            for article in report.new_articles[:5]:
                LOGGER.info("    %s | %s", article.published_at, article.title[:44])
    LOGGER.info("合计：新增 %d，更新 %d", total_new, total_updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
