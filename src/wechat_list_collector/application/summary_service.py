"""受控的单篇微信摘要取回。

``/review/single`` 的 ``review.mpInfo.content`` 才是真正的微信摘要，而
``/mp/chapters`` 的列表项不带摘要，因此摘要补全是"一篇一次请求"的线性成本。

这个服务把一次摘要请求的完整生命周期收敛到一处：

* 向 ``RequestGate`` 申请许可，计入 ``article_summary`` operation 的独立预算；
* 成功时把请求标记为 ``succeeded``；
* 失败时把请求标记为对应终态，**不触发通道级冷却**——摘要失败不应把列表
  采集一起锁死。

调用方只需要处理返回值：拿到摘要字符串就用，拿不到（``None``）就停止补全。
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from ..domain.errors import CooldownActive, RequestBudgetExhausted, WeChatNetworkFailed, WeChatSessionExpired
from ..domain.models import RequestIntent
from ..infrastructure.request_gate import WEREAD_SUMMARY_OPERATION, RequestGate

logger = logging.getLogger(__name__)


class SummaryFetcher(Protocol):
    """可以按 review_id 取回微信摘要的抓取方。"""

    def fetch_article_summary(self, review_id: str) -> str: ...


class SummaryFetchService:
    """在受控预算下取回单篇微信摘要。"""

    def __init__(
        self,
        gate: RequestGate,
        fetcher: SummaryFetcher,
        *,
        minimum_interval_seconds: int = 12,
    ):
        self.gate = gate
        self.fetcher = fetcher
        self.minimum_interval_seconds = minimum_interval_seconds

    def fetch(
        self,
        *,
        command_id: str,
        review_id: str,
        account_id: str | None = None,
        job_id: str | None = None,
    ) -> str | None:
        """取回一篇摘要；任何停止条件都返回 ``None`` 而不是抛异常。"""
        permit = None
        try:
            permit = self.gate.acquire(
                RequestIntent(
                    command_id=command_id,
                    job_id=job_id,
                    account_id=account_id,
                    operation=WEREAD_SUMMARY_OPERATION,
                    count=1,
                ),
                operation_minimum_interval_seconds=self.minimum_interval_seconds,
            )
        except (CooldownActive, RequestBudgetExhausted):
            logger.info(
                "summary_budget_stopped",
                extra={"operation": WEREAD_SUMMARY_OPERATION, "account_id": account_id},
            )
            return None

        started = time.monotonic()
        try:
            summary = self.fetcher.fetch_article_summary(review_id)
        except WeChatSessionExpired:
            self.gate.fail(permit, status="session_expired", reason_code="WEREAD_CREDENTIALS_EXPIRED")
            return None
        except WeChatNetworkFailed:
            # 频控（-2014）由账号池冷却 300 秒并换号处理，这里不触发通道级冷却。
            self.gate.fail(permit, status="network_failed", reason_code="WEREAD_SUMMARY_FAILED")
            return None
        except Exception:  # noqa: BLE001 - 补全失败不能影响列表入库
            self.gate.fail(permit, status="unknown", reason_code="WEREAD_SUMMARY_ERROR")
            return None

        self.gate.succeed(
            permit,
            publish_record_count=0,
            article_count=1,
            duration_ms=round((time.monotonic() - started) * 1000),
            response_schema="weread-review-single-v1",
        )
        return summary
