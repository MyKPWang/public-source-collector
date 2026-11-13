"""公众号后台历史消息网关：把 ``mp/profile_ext`` 接进采集链路。

为什么需要这一层
----------------
微信读书的 ``/mp/chapters`` 对每个公众号只暴露**每条推送的头条**。一篇推送
（群发）里可以带多条次条，微信读书不会把次条拆成独立条目，于是次条整批漏采。
实测 APPSO 连续 40 天每天只拿到 3 条，而第三方列表显示它每天实际发 5～6 篇。

公众号后台的 ``mp/profile_ext`` 返回的是官方全量历史消息：每条推送记录里带
``article_list``，包含该推送的头条与全部次条，每篇都有官方永久链接
（``__biz/mid/idx/sn``）与精确发布时间。

降级链路
--------
    MpProfileExtGateway（官方全量，含次条）
        └── 抛 WeChatSessionExpired / WeChatAccessBlocked
                └── WeReadArticleGateway（微信读书，仅头条）
                    └── 再次失败
                            └── WeChatListClient（公众号后台 appmsgpublish）
"""

from __future__ import annotations

import logging
from typing import Any

from ...domain.errors import (
    WeChatAccessBlocked,
    WeChatNetworkFailed,
    WeChatProtocolChanged,
    WeChatSessionExpired,
)
from ...infrastructure.credential_vault import EncryptedFileCredentialVault
from ...models import ArticlePage
from .profile_ext_client import (
    MpProfileExtClient,
    ProfileExtAccessBlocked,
    ProfileExtSessionExpired,
)


class MpProfileExtGateway:
    """用公众号后台历史消息实现 ``ArticlePageGateway`` 协议。"""

    def __init__(
        self,
        vault: EncryptedFileCredentialVault,
        timeout_seconds: int = 20,
        *,
        page_size: int = 10,
    ):
        self.vault = vault
        self.timeout_seconds = timeout_seconds
        self.page_size = page_size
        self.logger = logging.getLogger(__name__)

    def _client(self) -> MpProfileExtClient:
        # 每次请求都重新读凭据：登录成功后 vault 会被替换，长驻 Worker 需要
        # 能立刻用上新会话，不能缓存旧 cookie。
        credentials = self.vault.load_wechat()
        return MpProfileExtClient(credentials.cookie, timeout_seconds=self.timeout_seconds)

    def fetch_article_page(
        self, *, account_id: str, fakeid: str, begin: int, count: int
    ) -> ArticlePage:
        """取历史消息。

        ``fakeid`` 在本通道里是公众号的 ``__biz``。它与微信读书的 fakeid
        同源——微信读书的 bookId ``MP_WXS_<bizuin>`` 里的数字就是 ``bizuin``，
        而 ``__biz = base64(bizuin)``。因此这里接受 ``__biz`` 形式的标识。
        """
        client = self._client()
        try:
            return client.fetch_article_page(
                account_id=account_id, biz=fakeid, begin=begin, count=count
            )
        except ProfileExtSessionExpired as error:
            raise WeChatSessionExpired("MP_SESSION_EXPIRED") from error
        except ProfileExtAccessBlocked as error:
            raise WeChatAccessBlocked("MP_ACCESS_BLOCKED") from error
        except ValueError as error:
            raise WeChatProtocolChanged("MP_SCHEMA_CHANGED") from error
        except RuntimeError as error:
            message = str(error)
            if "ret=" in message:
                raise WeChatProtocolChanged("MP_SCHEMA_CHANGED") from error
            raise WeChatNetworkFailed("MP_REQUEST_FAILED") from error


class MergedArticleGateway:
    """合并多个通道的文章列表，按 ``(mid, idx)`` 去重。

    用途：官方历史消息通道能补上次条，但微信读书对**推送头条**的覆盖更稳
    （有摘要、正文可用），且历史更深。合并后既保留次条，也不丢已有头条。

    去重键用官方链接里的 ``mid`` + ``idx``：同一篇文章在两个通道里的
    ``appmsgid`` / ``sn`` 表述可能不同，但 ``mid``/``idx`` 坐标一致。
    """

    def __init__(self, *gateways: Any):
        self.gateways = gateways
        self.logger = logging.getLogger(__name__)

    def fetch_article_page(
        self, *, account_id: str, fakeid: str, begin: int, count: int
    ) -> ArticlePage:
        merged: dict[tuple[str, int], Any] = {}
        order: list[tuple[str, int]] = []
        total_records = 0
        for gateway in self.gateways:
            try:
                page = gateway.fetch_article_page(
                    account_id=account_id, fakeid=fakeid, begin=begin, count=count
                )
            except (WeChatSessionExpired, WeChatAccessBlocked):
                # 某个通道不可用时继续用下一个；但如果全部不可用，异常会在
                # 循环结束后由最后抛出的错误体现——这里只跳过，继续尝试。
                self.logger.warning(
                    "合并通道不可用，跳过: %s", type(gateway).__name__, exc_info=True
                )
                continue
            total_records = max(total_records, page.publish_record_count)
            for article in page.articles:
                key = _article_key(article)
                if key in merged:
                    # 已有的优先保留（前面的通道更权威），后到的补齐空字段。
                    existing = merged[key]
                    merged[key] = _fill_missing(existing, article)
                    continue
                merged[key] = article
                order.append(key)
        if not merged and not order:
            # 所有通道都失败：让上层看到最后一次的真实错误。
            raise WeChatNetworkFailed("ALL_CHANNELS_FAILED")
        articles = tuple(merged[key] for key in order)
        return ArticlePage(publish_record_count=total_records or len(articles), articles=articles)


def _article_key(article: Any) -> tuple[str, int]:
    """从文章 URL 里解析 ``(mid, idx)``；解析不出时退化为按 URL 精确匹配。"""
    url = getattr(article, "url", "") or ""
    mid = ""
    idx = 1
    if "__biz=" in url:
        parts = url.split("?", 1)[1].split("&")
        for part in parts:
            if part.startswith("mid="):
                mid = part[4:]
            elif part.startswith("idx="):
                try:
                    idx = int(part[4:])
                except ValueError:
                    idx = 1
    if mid:
        return (mid, idx)
    return (url, 0)


def _fill_missing(existing: Any, incoming: Any) -> Any:
    """用后到文章补齐已有文章的空字段（摘要/封面），不覆盖已有值。"""
    from dataclasses import replace

    updates: dict[str, Any] = {}
    if not getattr(existing, "digest", "") and getattr(incoming, "digest", ""):
        updates["digest"] = incoming.digest
    if not getattr(existing, "cover_url", "") and getattr(incoming, "cover_url", ""):
        updates["cover_url"] = incoming.cover_url
    if not getattr(existing, "title", "") and getattr(incoming, "title", ""):
        updates["title"] = incoming.title
    if not updates:
        return existing
    return replace(existing, **updates)
