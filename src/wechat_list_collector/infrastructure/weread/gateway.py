"""文章抓取网关：公众号后台优先，权限被封时自动降级到微信读书。

背景
----
``/cgi-bin/appmsgpublish``（公众号后台“取他人文章列表”）对未认证订阅号恒定
返回 ``ret=200013``，静默 420 秒后仍复现，属于 entitlement/permission 封禁，
不是频率限制。

微信读书的推文聚合接口可以取到同样的公众号文章，因此这里做成降级链路：

    RefreshingRequestsWeChatGateway（公众号后台）
        └── 抛 WeChatAccessBlocked
                └── WeReadArticleGateway（微信读书）

``WeReadArticleGateway`` 实现的仍然是 ``ArticlePageGateway`` 协议
（``fetch_article_page`` -> ``ArticlePage``），因此对上层采集执行器完全透明。
"""

from __future__ import annotations

import time
from typing import Any

from ...domain.errors import (
    WeChatAccessBlocked,
    WeChatNetworkFailed,
    WeChatProtocolChanged,
    WeChatRateLimited,
    WeChatSessionExpired,
)
from ...models import Article, ArticlePage
from ..credential_vault import EncryptedFileCredentialVault
from ..weread.account_pool import PooledWeReadClient, WeReadAllAccountsCooling
from ..weread.weread_client import (
    WeReadAuthExpired,
    WeReadClient,
    WeReadRateLimited,
    WeReadSessionInactive,
)


class WeReadArticleGateway:
    """用微信读书推文接口实现 ArticlePageGateway 协议。"""

    def __init__(self, vault: EncryptedFileCredentialVault, timeout_seconds: int = 20):
        self.vault = vault
        self.timeout_seconds = timeout_seconds
        self._client: PooledWeReadClient | None = None

    def _get_client(self) -> PooledWeReadClient:
        if self._client is None:
            accounts = self.vault.load_weread_accounts()
            self._client = PooledWeReadClient.from_credentials(
                accounts,
                timeout_seconds=self.timeout_seconds,
                on_refresh=self._persist_refreshed,
            )
        return self._client

    def _persist_refreshed(self, refreshed: dict[str, Any]) -> None:
        """把静默续期后的凭据写回 vault，并保留账号池里的其它账号。

        微信读书的 accessToken 是短效的，不持久化的话每次采集都可能撞上 401。

        续期回调只带新凭据、不带账号标识，因此按 ``vid`` 定位账号槽位：
        直接 ``replace_weread_accounts([refreshed])`` 会把整个账号池覆盖成
        只有一个账号，另一个账号的凭据会被静默丢弃。
        """
        try:
            accounts = self.vault.load_weread_accounts()
            vid = str(refreshed.get("vid") or "")
            merged: list[dict[str, Any]] = []
            replaced = False
            for account in accounts:
                if vid and str(account.get("vid")) == vid:
                    merged.append({**account, **refreshed})
                    replaced = True
                else:
                    merged.append(account)
            if not replaced:
                merged.append(dict(refreshed))
            self.vault.replace_weread_accounts(merged)
        except Exception:  # noqa: BLE001 - 写回失败不应中断抓取
            pass

    def fetch_article_summary(self, review_id: str) -> str:
        """取回单篇微信文章摘要（``/review/single`` 的 ``review.mpInfo.content``）。

        ``/mp/chapters`` 的列表项不带摘要，只有单篇详情接口才有，因此摘要
        补全天然是"一篇一次请求"的线性成本，必须由调用方做预算控制。
        """
        client = self._get_client()
        try:
            return client.fetch_summary(review_id)
        except WeReadAuthExpired as error:
            raise WeChatSessionExpired("WEREAD_CREDENTIALS_EXPIRED") from error
        except WeReadRateLimited as error:
            # 账号级频控：账号池已经冷却该账号并换号，这里不触发通道级冷却，
            # 否则摘要补全会把列表采集一起锁死。
            raise WeChatNetworkFailed("WEREAD_RATE_LIMITED") from error
        except WeReadSessionInactive as error:
            raise WeChatNetworkFailed("WEREAD_SESSION_INACTIVE") from error
        except WeReadAllAccountsCooling as error:
            raise WeChatNetworkFailed("WEREAD_ALL_ACCOUNTS_COOLING") from error
        except RuntimeError as error:
            raise WeChatNetworkFailed("WEREAD_REQUEST_FAILED") from error

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        client = self._get_client()
        try:
            articles, _total = client.fetch_articles(fakeid, begin=begin, count=count)
        except WeReadAuthExpired as error:
            raise WeChatSessionExpired("WEREAD_CREDENTIALS_EXPIRED") from error
        except WeReadRateLimited as error:
            # 微信读书的频控是"按接口、分钟级"的，不能当成微信后台的 200013，
            # 否则会误触发全局 6 小时冷却，把公众号通道一起锁死。
            # 这里归类为网络类失败：只标记本次请求失败，不进入冷却。
            raise WeChatNetworkFailed("WEREAD_RATE_LIMITED") from error
        except WeReadSessionInactive as error:
            raise WeChatNetworkFailed("WEREAD_SESSION_INACTIVE") from error
        except WeReadAllAccountsCooling as error:
            # 账号池里所有账号都在冷却：属于可恢复的暂时状态，不进全局冷却。
            raise WeChatNetworkFailed("WEREAD_ALL_ACCOUNTS_COOLING") from error
        except ValueError as error:
            raise WeChatProtocolChanged("WEREAD_INVALID_FAKEID") from error
        except RuntimeError as error:
            raise WeChatNetworkFailed("WEREAD_REQUEST_FAILED") from error
        return ArticlePage(
            publish_record_count=len(articles),
            articles=tuple(
                Article(
                    id=article.review_id,
                    feed_id=account_id,
                    title=article.title,
                    url=article.url,
                    digest=article.digest,
                    cover_url=article.cover_url,
                    publish_time=article.publish_time,
                )
                for article in articles
            ),
        )


class FallbackArticleGateway:
    """先试公众号后台；被权限封禁时自动改走微信读书。

    其它错误（限流 / 会话过期 / 网络）一律原样上抛，不做降级，
    以免把真实的限流问题掩盖成"抓到了"。
    """

    def __init__(self, primary, fallback, *, retry_delay_seconds: float = 1.0):
        self.primary = primary
        self.fallback = fallback
        self.retry_delay_seconds = retry_delay_seconds

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        try:
            return self.primary.fetch_article_page(
                account_id=account_id, fakeid=fakeid, begin=begin, count=count
            )
        except WeChatAccessBlocked:
            time.sleep(self.retry_delay_seconds)
            return self.fallback.fetch_article_page(
                account_id=account_id, fakeid=fakeid, begin=begin, count=count
            )
