"""微信摘要补全：把 ``/review/single`` 的 ``mpInfo.content`` 写进 ``articles.digest``。

背景
----
``/mp/chapters`` 的列表项只有标题和封面，``mpInfo`` 里没有摘要字段，所以入库
的微信文章 ``digest`` 恒为空。pipeline 的 ``quality_filter`` 会把"既没有摘要
也没有正文"的条目打进 review，今天的日报里就看不到任何微信内容。

微信摘要的真实来源是 ``/review/single`` 的 ``review.mpInfo.content``（注意不是
``mpInfo.digest``——那个字段并不存在）。该接口只支持单篇查询，因此补全是
"一篇一次请求"的线性成本，必须受控。

节流纪律
--------
摘要请求与列表请求共用 ``weread_reader`` 通道的节奏（12 秒最小间隔 + 3~8 秒
jitter），因此不会两套独立全局节流叠加成双倍速率。区别只在于：

* 计数维度独立：``operation="article_summary"`` 有自己的 rolling window 和
  per-command 上限，不与列表请求互相挤占；
* 仍受账号池 300 秒冷却和 ``-2014`` 熔断约束。

停止条件
--------
达到 per-command 上限、账号池全部冷却、或触发频控时，只停止摘要补全——已经
取回的列表文章照常入库，不会因为补摘要失败而丢掉整页数据。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Callable, Sequence

from ..domain.models import AccountCollectionJob
from ..models import Article
from .report_window import report_window
from .summary_service import SummaryFetchService

logger = logging.getLogger(__name__)


class SummaryEnricher:
    """为缺摘要的微信文章补全摘要。

    只处理日报窗口内、数据库里还没有摘要的条目，并按页限量；任何失败都只
    中止补全本身，不影响列表结果的入库。
    """

    def __init__(
        self,
        repository,
        service: SummaryFetchService,
        *,
        max_items_per_page: int = 3,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.service = service
        self.max_items_per_page = max_items_per_page
        self._now = now or (lambda: datetime.now(UTC))

    # ---------- 对外入口 ----------

    def enrich(self, job: AccountCollectionJob, articles: Sequence[Article]) -> tuple[Article, ...]:
        page = tuple(articles)
        candidates = self._candidates(job, page)
        if not candidates:
            return page
        enriched: dict[str, str] = {}
        used = 0
        for article in candidates:
            if used >= self.max_items_per_page:
                break
            summary = self._fetch_one(job, article)
            if summary is None:
                # 停止条件：预算耗尽 / 账号全冷却 / 频控。列表文章照常入库。
                break
            used += 1
            if summary:
                enriched[article.id] = summary
        if not enriched:
            return page
        return tuple(replace(article, digest=enriched[article.id]) if article.id in enriched else article for article in page)

    # ---------- 内部实现 ----------

    def _candidates(self, job: AccountCollectionJob, page: Sequence[Article]) -> list[Article]:
        window_start, window_end = self._report_window()
        wanted = [article for article in page if not article.digest.strip() and self._in_window(article, window_start, window_end)]
        if not wanted:
            return []
        known = self.repository.existing_digests(job.account_id, [article.id for article in wanted])
        pending = [article for article in wanted if not known.get(article.id, "").strip()]
        # 新的优先：日报窗口内越新的文章越可能进入当天日报。
        pending.sort(key=lambda article: article.publish_time, reverse=True)
        return pending

    def _fetch_one(self, job: AccountCollectionJob, article: Article) -> str | None:
        return self.service.fetch(
            command_id=job.command_id,
            review_id=article.id,
            account_id=job.account_id,
            job_id=job.id,
        )

    def _in_window(self, article: Article, window_start: datetime, window_end: datetime) -> bool:
        published_at = datetime.fromtimestamp(article.publish_time, UTC)
        return window_start <= published_at < window_end

    def _report_window(self) -> tuple[datetime, datetime]:
        """与 pipeline 的日报窗口保持一致：昨天 00:00 ~ 明天 00:00（上海）。"""
        return report_window(self._now())
