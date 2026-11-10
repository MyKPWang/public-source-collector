"""为已入库但仍缺摘要的微信文章补全摘要。

采集期的摘要补全受 ``max_items_per_page`` 限制（一页最多补几篇），一篇
账号的三个页面全部补完后，日报窗口内仍可能有文章没有摘要——它们会被
pipeline 的 ``quality_filter`` 以 ``missing_summary_and_content`` 打进 review，
于是今天的日报里看不到这些内容。

这个补全器单独跑一遍，遍历"日报窗口内 + 摘要为空"的存量文章，逐篇取回
``/review/single`` 的 ``review.mpInfo.content`` 并写回 ``articles.digest``。

预算纪律
========

摘要请求仍然走 ``SummaryFetchService``：与列表请求共用 ``weread_reader``
通道的 12 秒最小间隔和 3~8 秒 jitter，计入 ``article_summary`` operation 的
独立 per-command 上限和 24 小时滚动预算，并受账号池 300 秒冷却与 ``-2014``
熔断约束。达到上限时停止补全，已补的摘要照常保留。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .report_window import report_window
from .summary_service import SummaryFetchService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummaryBackfillResult:
    """一次补全的结果统计。"""

    scanned: int
    filled: int
    empty: int
    skipped: int
    stopped_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "filled": self.filled,
            "empty": self.empty,
            "skipped": self.skipped,
            "stopped_reason": self.stopped_reason,
        }


class SummaryBackfiller:
    """把日报窗口内缺摘要的存量文章补齐。"""

    def __init__(
        self,
        repository,
        service: SummaryFetchService,
        *,
        limit: int = 60,
        now: datetime | None = None,
    ):
        self.repository = repository
        self.service = service
        self.limit = limit
        self._now = now

    def run(self, *, command_id: str) -> SummaryBackfillResult:
        window_start, window_end = report_window(self._now)
        pending = self.repository.list_articles_missing_digest(
            window_start=window_start,
            window_end=window_end,
            limit=self.limit,
        )
        scanned = len(pending)
        filled = 0
        empty = 0
        skipped = 0
        stopped_reason = ""

        for row in pending:
            review_id = str(row["source_article_id"] or "")
            if not review_id:
                skipped += 1
                continue
            summary = self.service.fetch(
                command_id=command_id,
                review_id=review_id,
                account_id=str(row["account_id"] or "") or None,
            )
            if summary is None:
                # 停止条件：预算耗尽 / 账号全冷却 / 频控。已补的摘要保留。
                stopped_reason = "STOPPED_ON_LIMIT_OR_ERROR"
                break
            if not summary.strip():
                empty += 1
                continue
            self.repository.set_article_digest(
                account_id=str(row["account_id"]),
                source_article_id=review_id,
                digest=summary,
            )
            filled += 1

        result = SummaryBackfillResult(
            scanned=scanned,
            filled=filled,
            empty=empty,
            skipped=skipped,
            stopped_reason=stopped_reason,
        )
        logger.info("summary_backfill_finished", extra=result.as_dict())
        return result
