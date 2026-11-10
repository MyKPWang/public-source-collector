from __future__ import annotations

from ...domain.errors import (
    WeChatAccessBlocked,
    WeChatNetworkFailed,
    WeChatProtocolChanged,
    WeChatRateLimited,
    WeChatSessionExpired,
)
from ...infrastructure.credential_vault import EncryptedFileCredentialVault
from ...models import ArticlePage, Credentials
from ...errors import WeChatAccessBlocked as LegacyAccessBlocked
from ...errors import WeChatRateLimited as LegacyRateLimited
from ...errors import WeChatResponseError, WeChatSessionExpired as LegacySessionExpired
from ...wechat_client import WeChatListClient


class RequestsWeChatGateway:
    """One-request adapter that translates legacy transport errors to domain errors."""

    def __init__(self, credentials: Credentials, timeout_seconds: int):
        self.client = WeChatListClient(credentials, timeout_seconds)

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        try:
            return self.client.fetch_article_page(account_id, fakeid, begin=begin, count=count)
        except LegacyAccessBlocked as error:
            raise WeChatAccessBlocked("WECHAT_ARTICLE_LIST_NOT_GRANTED") from error
        except LegacyRateLimited as error:
            raise WeChatRateLimited("WECHAT_200013") from error
        except LegacySessionExpired as error:
            raise WeChatSessionExpired("WECHAT_200003") from error
        except WeChatResponseError as error:
            if "schema changed" in str(error):
                raise WeChatProtocolChanged("WECHAT_SCHEMA_CHANGED") from error
            raise WeChatNetworkFailed("WECHAT_REQUEST_FAILED") from error

    def search_accounts(self, *, keyword: str, limit: int = 5) -> list[dict[str, object]]:
        try:
            return self.client.search(keyword, limit)
        except LegacyRateLimited as error:
            raise WeChatRateLimited("WECHAT_200013") from error
        except LegacySessionExpired as error:
            raise WeChatSessionExpired("WECHAT_200003") from error
        except WeChatResponseError as error:
            raise WeChatNetworkFailed("WECHAT_REQUEST_FAILED") from error


class RefreshingRequestsWeChatGateway:
    """Loads the latest authorized credentials for each collection request.

    The admin site can replace credentials after a successful account switch while
    a long-running Worker remains alive. Constructing the transport lazily keeps
    the next request bound to the newly authorized account.
    """

    def __init__(self, vault: EncryptedFileCredentialVault, timeout_seconds: int):
        self._vault = vault
        self._timeout_seconds = timeout_seconds

    def fetch_article_page(self, *, account_id: str, fakeid: str, begin: int, count: int) -> ArticlePage:
        gateway = RequestsWeChatGateway(self._vault.load_wechat(), self._timeout_seconds)
        return gateway.fetch_article_page(account_id=account_id, fakeid=fakeid, begin=begin, count=count)
