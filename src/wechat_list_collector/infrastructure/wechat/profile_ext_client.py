"""公众号后台历史消息接口（``mp/profile_ext``）客户端。

背景
----
微信读书的 ``/mp/chapters`` 每次只返回**一条群发推送**（publish record），
一篇推送里可以包含头条 + 多条次条。但微信读书只把头条暴露成独立条目，
次条不会出现在章节列表里。实测 APPSO 连续 40 天每天只有 3 条，而它实际每天
发布 5～6 篇——缺的正是次条。

公众号后台的 ``mp/profile_ext`` 才是官方全量历史消息接口：每条 publish
record 的 ``app_msg_list`` 里带着该次推送的全部文章，每篇都有
``appmsgid`` / ``title`` / ``link``（永久链接）/ ``create_time``。

接口契约
--------
``GET https://mp.weixin.qq.com/mp/profile_ext``

* ``action=home``  —— 返回 ``publish_count`` 等账号概况
* ``action=getmsg`` —— 按 ``offset`` / ``count`` 翻页返回 ``app_msg_list``

两者都需要有效的公众号后台会话 cookie；无会话时返回 ``ret=-3``。

永久链接
--------
``app_msg_list[].link`` 形如::

    https://mp.weixin.qq.com/s?__biz=MjM5MjAyNDUyMA==&mid=2651116031&idx=1&sn=2a8423e6...

其中 ``idx`` 区分同一推送内的头条（1）与次条（2、3……），``sn`` 是文章自身的
签名参数。次条与头条共享 ``mid``，但 ``sn`` 不同——所以 ``sn`` 必须来自官方
接口，不能由 ``mid`` 推导。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ...models import Article, ArticlePage

_PROFILE_EXT_URL = "https://mp.weixin.qq.com/mp/profile_ext"

# ``app_msg_list[].link`` 里的 HTML 实体形式（``&amp;``）需要还原。
_LINK_RE = re.compile(
    r"__biz=(?P<biz>[A-Za-z0-9+/=_-]+)&(?:amp;)?mid=(?P<mid>\d+)&(?:amp;)?idx=(?P<idx>\d+)&(?:amp;)?sn=(?P<sn>[0-9a-fA-F]+)"
)


class ProfileExtSessionExpired(RuntimeError):
    """公众号后台会话失效（``ret=-3`` / 跳转登录页）。"""


class ProfileExtAccessBlocked(RuntimeError):
    """公众号后台拒绝访问（权限封禁 / 频控）。"""


@dataclass(frozen=True)
class ProfileExtArticle:
    """一条官方历史消息里的单篇文章。"""

    appmsgid: int
    title: str
    url: str
    digest: str
    cover_url: str
    create_time: int
    item_idx: int

    @property
    def publish_time(self) -> datetime:
        return datetime.fromtimestamp(self.create_time, tz=UTC)

    @property
    def biz(self) -> str:
        match = _LINK_RE.search(self.url)
        return match.group("biz") if match else ""

    @property
    def mid(self) -> str:
        match = _LINK_RE.search(self.url)
        return match.group("mid") if match else ""

    @property
    def sn(self) -> str:
        match = _LINK_RE.search(self.url)
        return match.group("sn") if match else ""

    @property
    def idx(self) -> int:
        match = _LINK_RE.search(self.url)
        return int(match.group("idx")) if match else 0


class MpProfileExtClient:
    """``mp/profile_ext`` 的最小 HTTP 客户端。

    刻意不引入新依赖：复用标准库 ``urllib``，与 ``WeChatListClient`` 的
    错误语义保持一致，便于网关层统一翻译。
    """

    def __init__(self, cookie: str, *, timeout_seconds: int = 20):
        self.cookie = cookie
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ HTTP

    def _request(self, params: dict[str, str]) -> str:
        import urllib.error
        import urllib.parse
        import urllib.request

        url = _PROFILE_EXT_URL + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "Cookie": self.cookie,
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": "https://mp.weixin.qq.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:  # noqa: PERF203 - 逐个错误语义不同
            if error.code in (401, 403):
                raise ProfileExtSessionExpired(f"HTTP {error.code}") from error
            raise RuntimeError(f"profile_ext HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"profile_ext network failed: {error.reason}") from error

        # 命中登录页 = 会话失效（后台把未登录请求重定向到 /cgi-bin/loginpage）。
        if "<title>" in body and "login" in body[:4000].lower():
            raise ProfileExtSessionExpired("redirected to login page")
        return body

    # --------------------------------------------------------------- 解析

    @staticmethod
    def _load_json(body: str) -> dict[str, Any]:
        text = body.strip()
        if not text:
            raise ProfileExtSessionExpired("empty response")
        if not text.startswith("{"):
            # 少数情况下后台返回 JSONP 包裹。
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                raise ProfileExtSessionExpired("unexpected response body")
            text = text[start : end + 1]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProfileExtSessionExpired("response is not JSON") from error
        if not isinstance(payload, dict):
            raise ProfileExtSessionExpired("response is not an object")
        return payload

    @staticmethod
    def _check_ret(payload: dict[str, Any]) -> None:
        ret = payload.get("ret")
        if ret is None:
            return
        try:
            code = int(ret)
        except (TypeError, ValueError):
            return
        if code in (0, 1):
            return
        if code == -3:
            raise ProfileExtSessionExpired("ret=-3 no session")
        if code in (200003, 200013):
            raise ProfileExtAccessBlocked(f"ret={code}")
        raise RuntimeError(f"profile_ext ret={code}")

    # --------------------------------------------------------------- 接口

    def fetch_publish_count(self, biz: str) -> int:
        """账号历史消息总条数（``action=home``）。"""
        payload = self._load_json(
            self._request(
                {
                    "action": "home",
                    "__biz": biz,
                    "scene": "124",
                    "f": "json",
                    "t": "weixin",
                }
            )
        )
        self._check_ret(payload)
        try:
            return int(payload.get("publish_count") or 0)
        except (TypeError, ValueError):
            return 0

    def fetch_history(self, biz: str, *, offset: int = 0, count: int = 10) -> list[ProfileExtArticle]:
        """按 ``offset``/``count`` 翻页取历史消息，展开成单篇列表。

        ``app_msg_list`` 每项是一条推送，内含 ``app_msg_ext_info`` 与
        ``article_list``（旧版字段名）/`app_msg_list`（新版字段名）——两者
        结构一致，都是文章数组。
        """
        payload = self._load_json(
            self._request(
                {
                    "action": "getmsg",
                    "__biz": biz,
                    "offset": str(offset),
                    "count": str(count),
                    "is_ok": "1",
                    "scene": "124",
                    "f": "json",
                    "t": "weixin",
                }
            )
        )
        self._check_ret(payload)
        return self._parse_articles(payload)

    @staticmethod
    def _parse_articles(payload: dict[str, Any]) -> list[ProfileExtArticle]:
        articles: list[ProfileExtArticle] = []
        for record in payload.get("app_msg_list") or []:
            if not isinstance(record, dict):
                continue
            for item in _iter_article_items(record):
                parsed = _parse_article_item(item)
                if parsed is not None:
                    articles.append(parsed)
        return articles

    def fetch_article_page(
        self, *, account_id: str, biz: str, begin: int, count: int
    ) -> ArticlePage:
        """实现网关协议：返回 ``ArticlePage``。

        ``begin`` / ``count`` 的语义与其它网关一致——按**推送条数**分页，
        而不是按文章条数。但返回的 ``articles`` 展开了每次推送里的所有文章
        （头条 + 次条），因此实际文章数会大于 ``count``。
        """
        articles = self.fetch_history(biz, offset=begin, count=count)
        return ArticlePage(
            publish_record_count=count,
            articles=tuple(
                Article(
                    id=_source_article_id(article),
                    feed_id=account_id,
                    title=article.title,
                    url=article.url,
                    digest=article.digest,
                    cover_url=article.cover_url,
                    publish_time=int(article.publish_time.timestamp()),
                )
                for article in articles
            ),
        )


def _iter_article_items(record: dict[str, Any]):
    """兼容不同版本后台的字段名。"""
    for key in ("article_list", "app_msg_list", "app_msg_ext_info"):
        value = record.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield item


def _parse_article_item(item: dict[str, Any]) -> ProfileExtArticle | None:
    title = str(item.get("title") or "").strip()
    link = str(item.get("link") or item.get("content_url") or "").strip()
    if not title or not link:
        return None
    link = link.replace("&amp;", "&")
    try:
        appmsgid = int(item.get("appmsgid") or item.get("appmsg_id") or 0)
    except (TypeError, ValueError):
        appmsgid = 0
    try:
        create_time = int(item.get("create_time") or item.get("update_time") or 0)
    except (TypeError, ValueError):
        create_time = 0
    match = _LINK_RE.search(link)
    item_idx = int(match.group("idx")) if match else 1
    return ProfileExtArticle(
        appmsgid=appmsgid,
        title=title,
        url=link,
        digest=str(item.get("digest") or "").strip(),
        cover_url=str(item.get("cover") or item.get("cover_url") or "").strip(),
        create_time=create_time,
        item_idx=item_idx,
    )


def _source_article_id(article: ProfileExtArticle) -> str:
    """构造稳定的来源文章 ID。

    用 ``biz`` + ``mid`` + ``idx`` 而不是 ``appmsgid``：``appmsgid`` 在
    后台改文中会变，而 ``mid``/``idx`` 是文章在推送中的固定坐标。这样
    补采到的次条与微信读书已有的头条之间也能按 ID 稳定对齐。
    """
    parts = [article.biz, article.mid, str(article.idx)]
    return "MPX_" + "_".join(part for part in parts if part)
