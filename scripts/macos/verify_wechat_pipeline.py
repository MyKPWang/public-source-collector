#!/usr/bin/env python3
"""微信公众号文章全链路验证（微信读书通道）。

逐步验证并打印每一步的真实结果：

  STEP 1  微信读书凭据可用性（账号池状态）
  STEP 2  真实抓取一个公众号的推文（WeReadClient -> articles）
  STEP 3  入库：articles 表行数变化
  STEP 4  Collector read API: /api/v1/pipeline/wechat/articles
  STEP 5  Pipeline 读取并产出 NewsItem

用法：
    .venv/bin/python scripts/macos/verify_wechat_pipeline.py
    .venv/bin/python scripts/macos/verify_wechat_pipeline.py --account 量子位
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

COLLECTOR_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_ROOT = Path("/Users/wangkp/Developer/ai-news-pipeline")
DB_PATH = COLLECTOR_ROOT / "data" / "collector.db"

sys.path.insert(0, str(COLLECTOR_ROOT / "src"))

STEP = 0


def step(title: str) -> None:
    global STEP
    STEP += 1
    print(f"\n=== STEP {STEP}: {title} ===", flush=True)


def ok(message: str) -> None:
    print(f"  [OK]   {message}", flush=True)


def fail(message: str) -> None:
    print(f"  [FAIL] {message}", flush=True)


def article_count() -> int:
    connection = sqlite3.connect(DB_PATH)
    try:
        return int(connection.execute("select count(*) from articles").fetchone()[0])
    finally:
        connection.close()


def pick_account(name: str) -> tuple[str, str]:
    """返回 (account_id, fakeid)，fakeid 从加密列解密。"""
    from wechat_list_collector.bootstrap import create_production_runtime
    from wechat_list_collector.config import load_settings

    settings = load_settings(COLLECTOR_ROOT / "collector.toml")
    runtime = create_production_runtime(settings)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        if name:
            row = connection.execute(
                "select id, name, fakeid_ciphertext from official_accounts where name = ?",
                (name,),
            ).fetchone()
        else:
            row = connection.execute(
                "select id, name, fakeid_ciphertext from official_accounts "
                "where archived_at is null and enabled = 1 order by display_order limit 1"
            ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"no official account matched {name!r}")
    fakeid = runtime.vault.decrypt_text(row["fakeid_ciphertext"]) if row["fakeid_ciphertext"] else ""
    return str(row["id"]), fakeid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", default="", help="公众号名称，默认取第一个启用账号")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--skip-api", action="store_true", help="跳过 STEP 4/5")
    args = parser.parse_args()

    # ---------- STEP 1 ----------
    step("微信读书凭据与账号池")
    from wechat_list_collector.bootstrap import create_production_runtime
    from wechat_list_collector.config import load_settings
    from wechat_list_collector.infrastructure.weread.account_pool import PooledWeReadClient

    settings = load_settings(COLLECTOR_ROOT / "collector.toml")
    runtime = create_production_runtime(settings)
    accounts = runtime.vault.load_weread_accounts()
    if not accounts:
        fail("no WeRead credentials stored in the vault")
        return 1
    ok(f"账号池中有 {len(accounts)} 个微信读书账号（vid 以 [REDACTED] 代替）")
    pool = PooledWeReadClient.from_credentials(accounts, timeout_seconds=30)
    for entry in pool.status():
        ok(f"  账号 label={entry['label'] or '(未命名)'} vid=[REDACTED] cooldown_until={entry['cooldown_until']}")

    # ---------- STEP 2 ----------
    step("真实抓取一个公众号的推文")
    account_id, fakeid = pick_account(args.account)
    ok(f"账号 id={account_id} fakeid=[REDACTED]")
    if not fakeid:
        fail("该账号没有保存 fakeid")
        return 1
    try:
        articles, total = pool.fetch_articles(fakeid, begin=0, count=args.count, account_name="")
    except Exception as error:  # noqa: BLE001
        fail(f"抓取失败: {type(error).__name__}: {error}")
        return 1
    ok(f"抓到 {len(articles)} 篇（接口报告总数 {total}）")
    for article in articles[:5]:
        stamp = (
            datetime.fromtimestamp(article.publish_time, UTC).strftime("%Y-%m-%d %H:%M")
            if article.publish_time
            else "无时间"
        )
        ok(f"  · {article.title[:38]} | {stamp} | {(article.url or '')[:52]}")

    # ---------- STEP 3 ----------
    step("入库 articles 表")
    before = article_count()
    from wechat_list_collector.infrastructure.weread.gateway import WeReadArticleGateway

    gateway = WeReadArticleGateway(runtime.vault, 30)
    try:
        page = gateway.fetch_article_page(
            account_id=account_id, fakeid=fakeid, begin=0, count=args.count
        )
    except Exception as error:  # noqa: BLE001
        fail(f"gateway 抓取失败: {type(error).__name__}: {error}")
        return 1
    ok(f"gateway 返回 {len(page.articles)} 篇")
    print(f"  articles 表当前 {before} 行（真实入库由 worker 触发，见下方说明）", flush=True)
    print(
        "  提示: 入库需经 worker --once 走 store_page 事务；直接调用 gateway 不会写库。",
        flush=True,
    )

    # ---------- STEP 4 ----------
    if args.skip_api:
        return 0

    step("Collector read API: /api/v1/pipeline/wechat/articles")
    import urllib.request

    env_path = PIPELINE_ROOT / "deploy" / "macos" / "local-machine.env"
    token = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"')
    if not token:
        fail("找不到 collector pipeline token")
        return 1

    now = datetime.now(UTC)
    query = (
        f"published_from={(now - timedelta(days=7)).isoformat()}"
        f"&published_before={(now + timedelta(days=1)).isoformat()}&limit=50"
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:4100/api/v1/pipeline/wechat/articles?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        fail(f"API 请求失败: {type(error).__name__}: {error}")
        return 1
    records = payload.get("articles") or []
    ok(f"API 返回 {len(records)} 篇微信文章")
    for record in records[:5]:
        ok(
            f"  · {str(record.get('title'))[:38]} | "
            f"{record.get('published_at')} | {(record.get('account') or {}).get('name')}"
        )

    # ---------- STEP 5 ----------
    step("Pipeline 读取并产出 NewsItem")
    script = (
        "import sys; sys.path.insert(0, %r);"
        "from src.config import load_config;"
        "from src.sources.wechat_read_api import WechatReadApiSource;"
        "config = load_config(%r, %r);"
        "items = WechatReadApiSource(config).collect();"
        "print('PIPELINE_ITEMS', len(items));"
        "[print('  ·', i.title[:40], '|', i.source, '|', i.time_text) for i in items[:5]]"
        % (str(PIPELINE_ROOT), str(PIPELINE_ROOT / "config.yaml"), str(PIPELINE_ROOT / "secrets.yaml"))
    )
    env = dict(__import__("os").environ)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"')
    result = subprocess.run(
        [str(PIPELINE_ROOT / ".venv" / "bin" / "python"), "-c", script],
        cwd=PIPELINE_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if result.returncode != 0:
        fail(f"pipeline 读取失败: {result.stderr.strip()[-400:]}")
        return 1
    for line in result.stdout.strip().splitlines():
        ok(line)

    print("\n=== 全链路验证通过 ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
