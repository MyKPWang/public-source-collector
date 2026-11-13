"""清理 articles 表里的**真重复**行。

重复的判定
----------
微信读书通道的 ``source_article_id`` 是 ``MP_WXS_{uin}_{reviewId}``，
补采通道的是 ``MP_{biz}_{mid}_{idx}``。同一篇文章在两套 ID 下取值不同，
唯一约束 ``uq_articles_account_source`` 拦不住，会写出重复行。

但**不能用标题当去重键**：同一个公众号会在不同日子发同名文章
（实测量子位「量子位编辑作者招聘」10 天发了 10 条），按标题去重会误删。

安全判定：同一账号内，标题规范化相同 **且发布时间相差在 6 小时内**
（跨零点也允许），并且两行的 ``source_article_id`` 前缀不同
（一行是微信读书行、一行是补采行）—— 只有这种组合才是
「同一篇文章被两条通道各记了一次」。

保留规则：优先保留微信读书行（发布时间精度更高、ID 是官方 reviewId），
并把补采行里更完整的 url（带 chksm）/cover_url/digest 合并进去，
补采行随后删除。
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DB = Path("/Users/wangkp/Developer/public-source-collector/data/collector.db")
# 同一篇文章在两条通道里的发布时间差容忍上限（小时）。
# 微信读书时间是整分、补采时间是秒级，正常差 < 1 分钟；留足余量防跨零点。
MAX_SPAN_HOURS = 6.0


def norm(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def is_weread(sid: str | None) -> bool:
    return str(sid or "").startswith("MP_WXS_")


def parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    account_ids = [
        r[0] for r in con.execute("SELECT DISTINCT account_id FROM articles").fetchall()
    ]

    merged = 0
    removed = 0
    for acct in account_ids:
        rows = con.execute(
            "SELECT * FROM articles WHERE account_id=?", (acct,)
        ).fetchall()

        groups = defaultdict(list)
        for r in rows:
            groups[norm(r["title"])].append(r)

        for _, group in groups.items():
            if len(group) < 2:
                continue
            # 只在「微信读书行」与「补采行」之间配对，且时间接近才算重复
            weread = [r for r in group if is_weread(r["source_article_id"])]
            recovered = [r for r in group if not is_weread(r["source_article_id"])]
            if not weread or not recovered:
                continue

            pairs = []
            for w in weread:
                wt = parse_ts(w["published_at"])
                if wt is None:
                    continue
                for rec in recovered:
                    rt = parse_ts(rec["published_at"])
                    if rt is None:
                        continue
                    span_h = abs((wt - rt).total_seconds()) / 3600.0
                    if span_h <= MAX_SPAN_HOURS:
                        pairs.append((span_h, w, rec))
            if not pairs:
                continue
            # 一行最多参与一次配对：按时间差从小到大贪心匹配
            pairs.sort(key=lambda p: p[0])
            used_w: set[str] = set()
            used_r: set[str] = set()
            for _, w, rec in pairs:
                wid, rid = str(w["id"]), str(rec["id"])
                if wid in used_w or rid in used_r:
                    continue
                used_w.add(wid)
                used_r.add(rid)

                url = str(w["url"] or "")
                cover = str(w["cover_url"] or "")
                digest = str(w["digest"] or "")
                rou = str(rec["url"] or "")
                if "chksm=" in rou and "chksm=" not in url:
                    url = rou
                if not cover and rec["cover_url"]:
                    cover = str(rec["cover_url"])
                if not digest.strip() and str(rec["digest"] or "").strip():
                    digest = str(rec["digest"])
                con.execute(
                    "UPDATE articles SET url=?, cover_url=?, digest=? WHERE id=?",
                    (url, cover, digest, wid),
                )
                con.execute("DELETE FROM articles WHERE id=?", (rid,))
                removed += 1
                merged += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"合并真重复 {merged} 组，删除多余行 {removed}")
    print(f"清理后全库文章数: {total}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
