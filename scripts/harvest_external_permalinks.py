#!/usr/bin/env python3
"""从外部转载/索引站采集原始微信永久链接，产出补采种子。

为什么需要
----------
微信读书的公众号索引会漏掉一部分文章（实测独立覆盖 5/7）。`recover_missing_articles.py`
能沿「🔗 相关阅读」做广度优先扩展，但扩展需要**新的种子**；库里已有的永久链接
只能覆盖已经抓到的文章，覆盖不到被完全漏掉的那几篇。

aipick.news 是一个 AI 科技文章转载/索引站，它保留了原文的微信永久链接：

* ``https://aipick.news/rss``                —— 标准 RSS 2.0，50 条，其中若干条带永久链接；
* ``https://aipick.news/daily/<YYYY-MM-DD>`` —— 按日期归档，每天 20~35 条；
* ``https://aipick.news/all?q=<关键词>``      —— 服务端关键词检索。

它的条目页（``/items/<slug>``）里含原始
``mp.weixin.qq.com/s?__biz=…&mid=…&idx=…&sn=…``。本脚本把这些永久链接抽出来，
交给 ``recover_from_external_seeds.py`` 去抓原文元数据并入库。

用法::

    python scripts/harvest_external_permalinks.py --out seeds.json --days 14
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger("harvest_permalinks")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 允许 & / &amp; / \\u0026 三种分隔符写法（aipick 的内联 JSON 会把 & 编码成 \\u0026）
PERMALINK_RE = re.compile(
    r"mp\.weixin\.qq\.com/s\?__biz=([^&\"'<>\\\s]+)"
    r"&(?:amp;|\\u0026|&#38;|%26)?mid=(\d+)"
    r"&(?:amp;|\\u0026|&#38;|%26)?idx=(\d+)"
    r"&(?:amp;|\\u0026|&#38;|%26)?sn=([a-f0-9]+)",
    re.I,
)
ITEM_LINK = re.compile(r'href="(/items/[a-z0-9]+)"')

# AI/科技领域常见检索词，用来覆盖 aipick 站点搜索
DEFAULT_QUERIES = [
    "AI", "人工智能", "大模型", "LLM", "智能体", "Agent", "开源",
    "英伟达", "苹果", "谷歌", "微软", "OpenAI", "Anthropic", "DeepSeek",
    "机器人", "自动驾驶", "芯片", "算力", "生成式", "多模态", "编程",
]


def http_get(url: str, *, timeout: int = 25, ua: str = DEFAULT_UA) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(1_500_000).decode("utf-8", errors="replace")


def extract_permalinks(text: str) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for match in PERMALINK_RE.finditer(text or ""):
        biz = urllib.parse.unquote(match.group(1))
        key = f"{biz}|{match.group(2)}|{match.group(3)}|{match.group(4)}"
        found.setdefault(
            key,
            {
                "biz": biz,
                "mid": match.group(2),
                "idx": match.group(3),
                "sn": match.group(4),
            },
        )
    return list(found.values())


def aipick_item_urls() -> list[str]:
    """RSS + 站点检索能直接看到的条目页。"""
    urls: set[str] = set()
    try:
        rss = http_get("https://aipick.news/rss")
        urls.update("https://aipick.news" + s for s in ITEM_LINK.findall(rss))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("aipick RSS 抓取失败：%s", exc)
    return sorted(urls)


def aipick_daily_urls(days: int, today: dt.date | None = None) -> list[str]:
    end = today or dt.date.today()
    urls: set[str] = set()
    for offset in range(days):
        day = (end - dt.timedelta(days=offset)).isoformat()
        try:
            body = http_get(f"https://aipick.news/daily/{day}")
        except Exception:  # noqa: BLE001
            continue
        found = ITEM_LINK.findall(body)
        LOGGER.info("aipick %s：%d 条", day, len(found))
        urls.update("https://aipick.news" + s for s in found)
        time.sleep(0.4)
    return sorted(urls)


def aipick_query_urls(queries: list[str]) -> list[str]:
    urls: set[str] = set()
    for query in queries:
        try:
            body = http_get(
                "https://aipick.news/all?q=" + urllib.parse.quote(query)
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("aipick 检索 '%s' 失败：%s", query, exc)
            continue
        found = ITEM_LINK.findall(body)
        LOGGER.info("aipick 检索 '%s'：%d 条", query, len(found))
        urls.update("https://aipick.news" + s for s in found)
        time.sleep(0.4)
    return sorted(urls)


def scan_url(url: str) -> list[dict[str, str]]:
    try:
        page = http_get(url)
    except Exception:  # noqa: BLE001
        return []
    return extract_permalinks(page)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="采集外部转载站的微信永久链接作为补采种子")
    parser.add_argument("--out", type=Path, required=True, help="输出的种子 JSON")
    parser.add_argument("--days", type=int, default=14, help="回溯多少天的日期归档")
    parser.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES, help="站点检索关键词")
    parser.add_argument("--no-query", action="store_true", help="跳过站点检索")
    parser.add_argument("--max-pages", type=int, default=600, help="最多抓多少个条目页")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pages: set[str] = set(aipick_item_urls())
    LOGGER.info("RSS 条目页 %d 个", len(pages))
    pages.update(aipick_daily_urls(args.days))
    LOGGER.info("加日期归档后 %d 个", len(pages))
    if not args.no_query:
        pages.update(aipick_query_urls(args.queries))
        LOGGER.info("加站点检索后 %d 个", len(pages))

    selected = sorted(pages)[: args.max_pages]
    LOGGER.info("开始逐页抽取永久链接，共 %d 页", len(selected))

    seeds: dict[str, dict[str, str]] = {}
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for found in pool.map(scan_url, selected):
            for seed in found:
                seeds[f"{seed['biz']}|{seed['mid']}|{seed['idx']}|{seed['sn']}"] = seed

    payload = list(seeds.values())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    LOGGER.info("写出 %d 条永久链接 → %s", len(payload), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
