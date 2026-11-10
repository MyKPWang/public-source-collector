from __future__ import annotations

import unittest
from unittest.mock import patch

from wechat_list_collector.infrastructure.credential_vault import InMemoryCredentialVault
from wechat_list_collector.infrastructure.wechat.gateway import RefreshingRequestsWeChatGateway
from wechat_list_collector.models import ArticlePage, Credentials


class RefreshingRequestsWeChatGatewayTests(unittest.TestCase):
    def test_uses_replaced_credentials_for_the_next_request(self) -> None:
        vault = InMemoryCredentialVault(Credentials(token="old-token", cookie="old-cookie"))
        gateway = RefreshingRequestsWeChatGateway(vault, timeout_seconds=30)
        response = ArticlePage(publish_record_count=0, articles=())

        with patch("wechat_list_collector.infrastructure.wechat.gateway.RequestsWeChatGateway") as transport:
            transport.return_value.fetch_article_page.return_value = response
            gateway.fetch_article_page(account_id="account", fakeid="fakeid", begin=0, count=5)
            transport.assert_called_once_with(Credentials(token="old-token", cookie="old-cookie"), 30)

            vault.replace_wechat(Credentials(token="new-token", cookie="new-cookie"))
            gateway.fetch_article_page(account_id="account", fakeid="fakeid", begin=5, count=5)

        self.assertEqual(
            transport.call_args_list[-1].args,
            (Credentials(token="new-token", cookie="new-cookie"), 30),
        )

