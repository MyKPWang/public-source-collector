from __future__ import annotations

import json
from typing import Any

import requests

from .errors import WeChatAccessBlocked, WeChatRateLimited, WeChatResponseError, WeChatSessionExpired
from .models import Article, ArticlePage, Credentials


class WeChatListClient:
    """Small adapter around the same list endpoint used by we-mp-rss."""

    base_url = "https://mp.weixin.qq.com"
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

    def __init__(self, credentials: Credentials, timeout_seconds: int, session: requests.Session | None = None):
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def search(self, keyword: str, limit: int = 5) -> list[dict[str, Any]]:
        payload = self._request_json(
            "/cgi-bin/searchbiz",
            {
                "action": "search_biz",
                "begin": 0,
                "count": limit,
                "query": keyword,
                "token": self.credentials.token,
                "lang": "zh_CN",
                "f": "json",
                "ajax": "1",
            },
        )
        return list(payload.get("list", []))

    def fetch_articles(self, feed_id: str, fakeid: str, pages: int, page_size: int) -> list[Article]:
        articles: list[Article] = []
        for page in range(pages):
            result = self.fetch_article_page(feed_id, fakeid, begin=page * page_size, count=page_size)
            articles.extend(result.articles)
            if result.publish_record_count < page_size:
                break
        return articles

    def fetch_article_page(self, feed_id: str, fakeid: str, *, begin: int, count: int) -> ArticlePage:
        """Fetch and parse exactly one publish-list request with no retry loop."""
        payload = self._request_json(
                "/cgi-bin/appmsgpublish",
                {
                    "sub": "list",
                    "sub_action": "list_ex",
                    "begin": begin,
                    "count": count,
                    "fakeid": fakeid,
                    "token": self.credentials.token,
                    "lang": "zh_CN",
                    "f": "json",
                    "ajax": "1",
                },
            )
        publish_page = payload.get("publish_page")
        if not publish_page:
            return ArticlePage(publish_record_count=0, articles=())
        try:
            page_data = json.loads(publish_page) if isinstance(publish_page, str) else publish_page
            publish_list = page_data.get("publish_list", [])
            if not isinstance(publish_list, list):
                raise TypeError("publish_list must be a list")
            articles: list[Article] = []
            for publish_item in publish_list:
                publish_info = publish_item.get("publish_info")
                if not publish_info:
                    continue
                info = json.loads(publish_info) if isinstance(publish_info, str) else publish_info
                if not isinstance(info, dict):
                    raise TypeError("publish_info must be an object")
                for item in info.get("appmsgex", []):
                    aid = str(item.get("aid", "")).strip()
                    if not aid:
                        continue
                    articles.append(
                        Article(
                            id=aid,
                            feed_id=feed_id,
                            title=str(item.get("title", "")),
                            url=str(item.get("link", "")),
                            digest=str(item.get("digest", "")),
                            cover_url=str(item.get("cover", "")),
                            publish_time=int(item.get("update_time") or item.get("create_time") or 0),
                        )
                    )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WeChatResponseError("WeChat article-list schema changed") from error
        return ArticlePage(publish_record_count=len(publish_list), articles=tuple(articles))

    def _request_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(url, params=params, headers=self._headers(url), timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as error:
            raise WeChatResponseError("WeChat request failed") from error
        except (TypeError, ValueError) as error:
            raise WeChatResponseError("WeChat returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise WeChatResponseError("WeChat returned an invalid response")
        base_response = payload.get("base_resp", {})
        try:
            ret = int(base_response.get("ret", -1))
        except (TypeError, ValueError) as error:
            raise WeChatResponseError("WeChat returned an invalid response code") from error
        if ret == 0:
            return payload
        if ret == 200013:
            # WeChat overloads 200013 for two different situations:
            #   * appmsgpublish -> entitlement/permission block (permanent)
            #   * searchbiz     -> genuine request-frequency limit (transient)
            # Treating them alike made the request gate enter a 6h cooldown for a
            # block that no wait can clear, so they are now separated.
            if path == "/cgi-bin/appmsgpublish":
                raise WeChatAccessBlocked(
                    "WeChat refused /cgi-bin/appmsgpublish with ret=200013 "
                    "(account lacks the article-list capability; not a rate limit)"
                )
            raise WeChatRateLimited("WeChat returned base_resp.ret=200013")
        if ret == 200003:
            raise WeChatSessionExpired("WeChat returned base_resp.ret=200003")
        raise WeChatResponseError(f"WeChat returned response code {ret}")

    def _headers(self, referer: str) -> dict[str, str]:
        return {
            "Cookie": self.credentials.cookie,
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": referer,
        }
