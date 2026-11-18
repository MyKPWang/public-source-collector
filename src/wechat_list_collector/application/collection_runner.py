from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..domain.errors import (
    CooldownActive,
    RequestBudgetExhausted,
    WeChatAccessBlocked,
    WeChatNetworkFailed,
    WeChatProtocolChanged,
    WeChatRateLimited,
    WeChatSessionExpired,
)
from ..domain.enums import RunAccountStatus
from ..domain.models import AccountCollectionJob, ArticleInput, RequestIntent
from ..infrastructure.db.repositories import CollectionRepository
from ..infrastructure.request_gate import RequestGate
from ..models import Article, ArticlePage
from .summary_enricher import SummaryEnricher


class ArticlePageGateway(Protocol):
    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage: ...


class FakeIdDecoder(Protocol):
    def decrypt_text(self, ciphertext: bytes) -> str: ...


@dataclass(frozen=True)
class AccountJobResult:
    status: RunAccountStatus
    page_count: int
    request_count: int
    fetched_count: int
    new_count: int
    updated_count: int
    first_page_head_source_article_id: str = ""
    reason_code: str = ""
    message: str = ""


class CollectionRunner:
    """Runs one claimed account job, with every page separately gated and stored."""

    def __init__(
        self,
        repository: CollectionRepository,
        gate: RequestGate,
        gateway: ArticlePageGateway,
        fakeid_decoder: FakeIdDecoder,
        *,
        publish_record_page_size: int = 5,
        max_pages: int = 3,
        summary_enricher: SummaryEnricher | None = None,
    ):
        self.repository = repository
        self.gate = gate
        self.gateway = gateway
        self.fakeid_decoder = fakeid_decoder
        self.publish_record_page_size = publish_record_page_size
        self.max_pages = max_pages
        self.logger = logging.getLogger(__name__)
        # 摘要补全是可选的：失败只影响摘要，不影响列表结果的入库。
        self.summary_enricher = summary_enricher

    def run(self, job: AccountCollectionJob) -> AccountJobResult:
        # 池外公众号没有 fakeid：它们只能走永久链接恢复通道，永远进不了
        # fakeid 采集通道。必须在解密之前判空——否则空密文会在解密里抛
        # VaultError，越过任务边界把整轮 worker 一起带走。
        if not job.fakeid_ciphertext:
            self.logger.warning(
                "account_fakeid_unavailable",
                extra={"operation": "collect_account", "account_id": job.account_id},
            )
            return self._result(
                RunAccountStatus.FAILED,
                0,
                0,
                0,
                0,
                0,
                "",
                "FAKEID_UNAVAILABLE",
            )
        try:
            fakeid = self.fakeid_decoder.decrypt_text(job.fakeid_ciphertext)
        except Exception as error:  # noqa: BLE001 - 单账号解密失败不得越过任务边界
            self.logger.warning(
                "account_fakeid_decrypt_failed",
                extra={"operation": "collect_account", "account_id": job.account_id},
            )
            return self._result(
                RunAccountStatus.FAILED,
                0,
                0,
                0,
                0,
                0,
                "",
                "FAKEID_DECRYPT_FAILED",
            )
        seen_article_ids: set[str] = set()
        first_head = ""
        page_count = request_count = fetched_count = new_count = updated_count = 0

        for page_number in range(1, self.max_pages + 1):
            begin = (page_number - 1) * self.publish_record_page_size
            permit = None
            started = time.monotonic()
            try:
                permit = self.gate.acquire(
                    RequestIntent(
                        command_id=job.command_id,
                        job_id=job.id,
                        account_id=job.account_id,
                        operation="article_page",
                        page_number=page_number,
                        begin=begin,
                        count=self.publish_record_page_size,
                    )
                )
                page = self.gateway.fetch_article_page(
                    account_id=job.account_id,
                    fakeid=fakeid,
                    begin=begin,
                    count=self.publish_record_page_size,
                )
                articles = self._enrich_summaries(job, page.articles)
                inputs = [self._to_article_input(article) for article in articles]
                self.gate.succeed(
                    permit,
                    publish_record_count=page.publish_record_count,
                    article_count=len(page.articles),
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
            except WeChatAccessBlocked:
                # Permission/entitlement refusal, not a rate limit: record it as a
                # terminal condition and deliberately do NOT start a cooldown,
                # because no amount of waiting clears it.
                if permit:
                    self.gate.fail(permit, status="access_blocked", reason_code="WECHAT_ARTICLE_LIST_NOT_GRANTED", wechat_ret=200013)
                return self._result(RunAccountStatus.ACCESS_BLOCKED, page_count, request_count + int(permit is not None), fetched_count, new_count, updated_count, first_head, "WECHAT_ARTICLE_LIST_NOT_GRANTED")
            except WeChatRateLimited:
                if permit:
                    self.gate.fail(permit, status="rate_limited", reason_code="WECHAT_200013", wechat_ret=200013)
                return self._result(RunAccountStatus.RATE_LIMITED, page_count, request_count + int(permit is not None), fetched_count, new_count, updated_count, first_head, "WECHAT_200013")
            except WeChatSessionExpired:
                if permit:
                    self.gate.fail(permit, status="session_expired", reason_code="WECHAT_200003", wechat_ret=200003)
                return self._result(RunAccountStatus.SESSION_EXPIRED, page_count, request_count + int(permit is not None), fetched_count, new_count, updated_count, first_head, "WECHAT_200003")
            except WeChatProtocolChanged:
                if permit:
                    self.gate.fail(permit, status="protocol_changed", reason_code="WECHAT_SCHEMA_CHANGED")
                return self._result(RunAccountStatus.PROTOCOL_CHANGED, page_count, request_count + int(permit is not None), fetched_count, new_count, updated_count, first_head, "WECHAT_SCHEMA_CHANGED")
            except (WeChatNetworkFailed, CooldownActive, RequestBudgetExhausted) as error:
                if permit:
                    self.gate.fail(permit, status="network_failed", reason_code="WECHAT_REQUEST_FAILED")
                code = "COLLECTION_COOLDOWN_ACTIVE" if isinstance(error, CooldownActive) else "WECHAT_REQUEST_FAILED"
                return self._result(RunAccountStatus.FAILED, page_count, request_count + int(permit is not None), fetched_count, new_count, updated_count, first_head, code)

            request_count += 1
            page_count += 1
            if page_number == 1 and inputs:
                first_head = inputs[0].source_article_id
            result = self.repository.store_job_page(job_id=job.id, page_number=page_number, articles_to_store=inputs)
            fetched_count += result.fetched_count
            new_count += result.new_count
            updated_count += result.updated_count

            hit_historical_watermark = False
            for article in inputs:
                if article.source_article_id in seen_article_ids:
                    continue
                seen_article_ids.add(article.source_article_id)
                if job.stop_watermark_source_article_id and article.source_article_id == job.stop_watermark_source_article_id:
                    hit_historical_watermark = True
            if hit_historical_watermark or page.publish_record_count < self.publish_record_page_size:
                status = RunAccountStatus.EMPTY if page_number == 1 and page.publish_record_count == 0 else RunAccountStatus.COMPLETED
                return self._result(status, page_count, request_count, fetched_count, new_count, updated_count, first_head)

        return self._result(
            RunAccountStatus.PAGE_LIMIT_REACHED,
            page_count,
            request_count,
            fetched_count,
            new_count,
            updated_count,
            first_head,
            "PAGE_LIMIT_REACHED",
        )

    def _enrich_summaries(self, job: AccountCollectionJob, articles: tuple[Article, ...]) -> tuple[Article, ...]:
        """在入库前补全微信摘要。

        ``/mp/chapters`` 的列表项不带摘要，只有 ``/review/single`` 的
        ``review.mpInfo.content`` 才是真正的微信摘要。补全是"一篇一次请求"的
        线性成本，因此由 ``SummaryEnricher`` 负责预算、冷却与停止条件。

        补全失败绝不丢弃已经取回的列表文章：这里捕获所有异常，原样返回。
        """
        if self.summary_enricher is None or not articles:
            return articles
        try:
            return self.summary_enricher.enrich(job, articles)
        except Exception:  # noqa: BLE001 - 摘要补全不能影响列表入库
            self.logger.warning(
                "summary_enrichment_failed",
                extra={"operation": "article_summary", "account_id": job.account_id},
            )
            return articles

    @staticmethod
    def _to_article_input(article) -> ArticleInput:
        if article.publish_time <= 0:
            raise WeChatProtocolChanged("WECHAT_MISSING_PUBLISH_TIME")
        return ArticleInput(
            source_article_id=article.id,
            title=article.title,
            digest=article.digest,
            url=article.url,
            cover_url=article.cover_url,
            published_at=datetime.fromtimestamp(article.publish_time, UTC),
            source_payload={},
        )

    @staticmethod
    def _result(
        status: RunAccountStatus,
        page_count: int,
        request_count: int,
        fetched_count: int,
        new_count: int,
        updated_count: int,
        first_head: str,
        reason_code: str = "",
    ) -> AccountJobResult:
        return AccountJobResult(
            status=status,
            page_count=page_count,
            request_count=request_count,
            fetched_count=fetched_count,
            new_count=new_count,
            updated_count=updated_count,
            first_page_head_source_article_id=first_head,
            reason_code=reason_code,
        )
