#!/usr/bin/env python3
"""从第三方转载/索引站（当前是 aipick.news）回收原始微信永久链接并补采入库。

背景
----
微信读书索引会漏掉部分公众号文章（独立覆盖 5/7）。`recover_missing_articles.py`
可以从库里的永久链接做广度优先扩展，但它**没有新种子**——扩展到某一层就停了。

aipick.news 是一个带原文永久链接的 AI 文章转载/索引站：

* ``/items/<slug>`` 页面里有原始 ``mp.weixin.qq.com/s?__biz=…&mid=…&idx=…&sn=…``；
* ``/all?q=<关键词>`` 和 ``/daily/<YYYY-MM-DD>`` 都能列举条目。

本脚本把这些外部发现的永久链接当作**外部种子**喂给现有的
``PermalinkRecoveryRunner``，走正式的 repository 写库路径（幂等），从而：

* 把原文标题/摘要/封面/发布时间抓回来（比转载站元数据准）；
* 把原始永久链接写进 ``articles.url``，后续就能作为 BFS 种子继续扩展。

用法::

    python scripts/recover_from_external_seeds.py --seeds seeds.json --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

LOGGER = logging.getLogger("recover_external")

PERMALINK_RE = re.compile(
    r"https?://mp\.weixin\.qq\.com/s\?__biz=([^&\"'<>\\\s]+)&amp;?mid=(\d+)&amp;?idx=(\d+)&amp;?sn=([a-f0-9]+)",
    re.I,
)
PERMALINK_RE_AMP = re.compile(
    r"https?://mp\.weixin\.qq\.com/s\?__biz=([^&\"'<>\\\s]+)&mid=(\d+)&idx=(\d+)&sn=([a-f0-9]+)",
    re.I,
)


def parse_permalinks(text: str) -> list[dict[str, str]]:
    """从任意文本（HTML / JSON / 日志）里抽永久链接坐标。"""
    found: dict[str, dict[str, str]] = {}
    for pattern in (PERMALINK_RE_AMP, PERMALINK_RE):
        for match in pattern.finditer(text or ""):
            biz = urllib.parse.unquote(match.group(1))
            mid, idx, sn = match.group(2), match.group(3), match.group(4)
            key = f"{biz}|{mid}|{idx}|{sn}"
            found.setdefault(key, {"biz": biz, "mid": mid, "idx": idx, "sn": sn})
    return list(found.values())


def _normalize_name(name: str) -> str:
    """公众号显示名归一化，用于跨通道对齐账号。

    微信页面上的名字常带空格和全角差异，微信读书侧则可能多出后缀。
    去掉空白并转小写，便于两边对上。
    """
    text = (name or "").strip()
    text = re.sub(r"[\s　]+", "", text)
    return text.lower()


def _coords_from_mapping(item: dict) -> dict[str, str] | None:
    """接受 ``{"biz":…, "mid":…, "idx":…, "sn":…}`` 这种结构化种子。

    ``chksm`` 可选：外部索引站给的链接常常自带它，有了它就能抓到
    不在本地账号池里的公众号。
    """
    biz = str(item.get("biz") or item.get("__biz") or "").strip()
    mid = str(item.get("mid") or "").strip()
    sn = str(item.get("sn") or "").strip()
    idx = str(item.get("idx") or "1").strip()
    if biz and mid and sn:
        coords = {"biz": urllib.parse.unquote(biz), "mid": mid, "idx": idx, "sn": sn}
        chksm = str(item.get("chksm") or "").strip()
        if chksm:
            coords["chksm"] = chksm
        return coords
    return None


def load_seeds(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return parse_permalinks(raw)
    seeds: list[dict[str, str]] = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            seeds.extend(parse_permalinks(node))
        elif isinstance(node, dict):
            direct = _coords_from_mapping(node)
            if direct:
                seeds.append(direct)
            else:
                for value in node.values():
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    unique: dict[str, dict[str, str]] = {}
    for seed in seeds:
        unique[f"{seed['biz']}|{seed['mid']}|{seed['idx']}|{seed['sn']}"] = seed
    return list(unique.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用外部发现的永久链接补采公众号文章")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "collector.toml")
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--credentials", type=Path, default=None)
    parser.add_argument("--seeds", type=Path, required=True, help="永久链接 JSON / 纯文本")
    parser.add_argument("--account-id", default=None, help="写回哪个账号（默认按 biz 自动匹配）")
    parser.add_argument("--max-articles", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=1, help="从外部种子继续扩展的层数")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省只做演练")
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="抓取结果缓存 JSON；存在则直接复用，不重复请求微信",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from wechat_list_collector.config import load_settings
    from wechat_list_collector.infrastructure.db import (
        CollectionRepository,
        create_sqlite_engine,
    )
    from wechat_list_collector.infrastructure.credential_vault import (
        EncryptedFileCredentialVault,
    )
    from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
        PermalinkCoords,
        PermalinkRecoveryClient,
        PermalinkRecoveryRunner,
        PermalinkTokenStore,
        RecoveredArticle,
        RecoveryReport,
        to_article_inputs,
    )

    settings = load_settings(args.config)
    database = args.database or Path(settings.database)
    credentials = args.credentials or (database.parent / "credentials.enc")

    seeds = load_seeds(args.seeds)
    if not seeds:
        LOGGER.error("没有从 %s 解析出永久链接", args.seeds)
        return 2
    LOGGER.info("外部种子 %d 条", len(seeds))

    vault = EncryptedFileCredentialVault(credentials)
    tokens = PermalinkTokenStore(vault)
    client = PermalinkRecoveryClient(tokens)
    runner = PermalinkRecoveryRunner(
        client, max_articles=args.max_articles, max_depth=args.max_depth
    )

    cached = None
    if args.cache and args.cache.exists():
        payload = json.loads(args.cache.read_text(encoding="utf-8"))
        cached = [RecoveredArticle(**item) for item in payload]
        LOGGER.info("复用缓存 %d 篇（%s）", len(cached), args.cache)
        report = RecoveryReport()
        report.new_articles = cached
        report.fetched = len(cached)
        report.seeds = len(cached)
    else:
        coords = [
            PermalinkCoords(
                biz=s["biz"],
                mid=s["mid"],
                idx=s["idx"],
                sn=s["sn"],
                chksm=s.get("chksm", ""),
            )
            for s in seeds
        ]
        report = runner.run([], permalink_seeds=coords)
        if args.cache:
            args.cache.parent.mkdir(parents=True, exist_ok=True)
            args.cache.write_text(
                json.dumps(
                    [
                        {
                            "title": a.title,
                            "url": a.url,
                            "digest": a.digest,
                            "cover_url": a.cover_url,
                            "published_at": a.published_at,
                            "biz": a.biz,
                            "mid": a.mid,
                            "idx": a.idx,
                            "source": a.source,
                            "account_name": a.account_name,
                        }
                        for a in report.new_articles
                    ],
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            LOGGER.info("抓取结果已缓存 → %s", args.cache)

    LOGGER.info(
        "抓取 %d，失败 %d，得到文章 %d", report.fetched, report.failed, len(report.new_articles)
    )
    for article in report.new_articles:
        LOGGER.info("  %s | %s", article.published_at[:16], article.title[:60])

    if not args.apply:
        LOGGER.info("演练模式，未写库")
        return 0

    engine = create_sqlite_engine(database)
    repository = CollectionRepository(engine)

    # 把恢复出来的文章放回**正确的账号**。
    #
    # 这里有两套互不相通的标识空间：
    # * ``official_accounts.source_account_id`` 是**公众号后台 fakeid**
    #   （QbitAI / AI_era / appsolution …），只对走过后台会话的账号有意义；
    # * 微信文章页里的 ``__biz``（MzI3MTA0MTk1MA== 之类）是另一套编号。
    #
    # 所以按 biz 反查 official_accounts 是查不到的。真正两边共有的只有
    # **公众号显示名**：页面上的 ``id="js_name"``。于是匹配顺序是
    # ①已有恢复记录的 biz → ②按显示名 → ③都没有就按显示名新建账号。
    import sqlite3

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, name FROM official_accounts"
        ).fetchall()
        # articles.source_article_id 形如 MP_<biz>_<mid>_<idx>，据此建立 biz → account 映射
        biz_rows = connection.execute(
            "SELECT account_id, source_article_id FROM articles"
            " WHERE source_article_id LIKE 'MP_%'"
        ).fetchall()
    finally:
        connection.close()
    by_biz: dict[str, str] = {}
    for account_id, source_article_id in biz_rows:
        parts = str(source_article_id).split("_")
        if len(parts) >= 2 and parts[1]:
            by_biz.setdefault(parts[1], account_id)
    by_name: dict[str, str] = {}
    for account_id, name in rows:
        key = _normalize_name(name)
        if key:
            by_name.setdefault(key, account_id)
    LOGGER.info(
        "已知 biz→账号 映射 %d 个，名称→账号 映射 %d 个", len(by_biz), len(by_name)
    )

    buckets: dict[str, list] = {}
    unmapped: list[str] = []
    created: dict[str, str] = {}
    for article in report.new_articles:
        target = (
            args.account_id
            or by_biz.get(article.biz or "")
            or by_name.get(_normalize_name(article.account_name))
        )
        if not target:
            # 账号池里没有这个公众号：按显示名新建一个，让文章有地方落。
            name = (article.account_name or "").strip()
            biz = (article.biz or "").strip()
            if not name or not biz:
                unmapped.append(biz or "?")
                continue
            if biz in created:
                target = created[biz]
            else:
                target = repository.add_account(
                    source_account_id=biz,
                    fakeid_ciphertext=b"",
                    name=name,
                    enabled=True,
                    display_order=len(created) + 100,
                )
                created[biz] = target
                by_name.setdefault(_normalize_name(name), target)
                LOGGER.info("新建账号 %s（biz=%s）→ %s", name, biz, target)
        buckets.setdefault(target, []).append(article)
    if unmapped:
        LOGGER.warning("有 %d 篇文章找不到对应账号，跳过：%s", len(unmapped), sorted(set(unmapped)))

    total_new = total_updated = 0
    for account_id, articles in buckets.items():
        new_count, updated_count = repository.store_recovered_articles(
            account_id=account_id,
            articles_to_store=list(to_article_inputs(articles, account_id=account_id)),
        )
        total_new += new_count
        total_updated += updated_count
        LOGGER.info("[%s] 新增 %d，更新 %d", account_id, new_count, updated_count)

    LOGGER.info("合计：新增 %d，更新 %d", total_new, total_updated)
    time.sleep(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
