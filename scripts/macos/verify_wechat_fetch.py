#!/usr/bin/env python3
"""验证「微信公众号文章能不能抓到」—— 走真实生产命令，不做假设。

STEP 1  用当前凭据调微信后台搜索接口（searchbiz），验证凭据是否有效。
STEP 2  凭据有效时，走生产链路真实抓取：
            enqueue collect-all  ->  worker --once
        采集器会真的调用 /cgi-bin/appmsgpublish，并把文章写进 articles 表。
STEP 3  读回 articles 表，确认真的入库了。

用法：
    python verify_wechat_fetch.py                  # 仅 STEP 1（凭据体检）
    python verify_wechat_fetch.py --fetch          # STEP 1+2+3（真实抓取入库）
    python verify_wechat_fetch.py --fetch --account <account_id>
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

COLLECTOR_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(COLLECTOR_ROOT / "src"))

from wechat_list_collector.bootstrap import create_production_runtime  # noqa: E402
from wechat_list_collector.config import load_settings  # noqa: E402
from wechat_list_collector.wechat_client import WeChatListClient  # noqa: E402

CLI = COLLECTOR_ROOT / ".venv" / "bin" / "public-source-collector"


def db_rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(COLLECTOR_ROOT / "data" / "collector.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CLI), "--config", "collector.toml", *args],
        cwd=COLLECTOR_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="执行真实抓取（STEP 2/3）")
    parser.add_argument("--account", default="", help="只抓取指定 account_id（默认为全部）")
    args = parser.parse_args()

    settings = load_settings(COLLECTOR_ROOT / "collector.toml")
    runtime = create_production_runtime(settings)

    # ---------- STEP 1: 凭据有效性 ----------
    print("STEP 1  调用微信后台搜索接口 (searchbiz)")
    credentials = runtime.vault.load_wechat()
    client = WeChatListClient(credentials, timeout_seconds=30)
    try:
        results = client.search("DeepSeek", limit=5)
    except Exception as exc:  # noqa: BLE001
        print(f"  SEARCH_ERR  {type(exc).__name__}: {str(exc)[:300]}")
        print("\n结论: 凭据无效 —— 微信文章抓不到。需要重新扫码登录：")
        print("      bash ~/Developer/public-source-collector/scripts/macos/wechat_login.sh")
        return 1
    print(f"  SEARCH_OK   返回 {len(results)} 个公众号")
    for item in results[:5]:
        print(f"    - {item.get('nickname')}   fakeid={str(item.get('fakeid'))[:20]}")

    if not args.fetch:
        print("\n结论: 凭据有效。加 --fetch 可继续验证「真的能抓到文章并入库」。")
        return 0

    # ---------- STEP 2: 真实抓取（生产链路）----------
    print("\nSTEP 2  通过生产命令提交采集任务")
    if args.account:
        enqueue = run_cli("enqueue", "collect-account", "--account-id", args.account,
                          "--idempotency-key", f"verify-{args.account}")
    else:
        enqueue = run_cli("enqueue", "collect-all", "--idempotency-key",
                          f"verify-all-{int(__import__('time').time())}")
    print("  enqueue:", enqueue.stdout.strip()[:200] or enqueue.stderr.strip()[:300])
    if enqueue.returncode != 0:
        print("  ENQUEUE_FAILED")
        return 1

    before = len(db_rows("SELECT id FROM articles"))
    worker = run_cli("worker", "--once")
    print("  worker:", worker.stdout.strip()[:200] or worker.stderr.strip()[:300])

    # ---------- STEP 3: 确认入库 ----------
    after_rows = db_rows("SELECT id FROM articles")
    print(f"\nSTEP 3  articles 表: {before} -> {len(after_rows)}（新增 {len(after_rows) - before}）")
    if len(after_rows) > before:
        for row in db_rows(
            "SELECT a.title, o.name AS account, a.published_at FROM articles a "
            "JOIN official_accounts o ON o.id = a.account_id "
            "ORDER BY a.published_at DESC LIMIT 5"
        ):
            print(f"    - [{row['account']}] {str(row['title'])[:56]}")
        print("\n结论: 微信文章抓取 + 入库链路可用。")
        return 0
    print("\n结论: 凭据有效但没有文章入库，需进一步排查（账号权限或接口字段变化）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
