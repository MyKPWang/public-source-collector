"""微信读书公众号文章抓取客户端。

背景
----
mp.weixin.qq.com 的 ``/cgi-bin/appmsgpublish``（公众号后台“取他人文章列表”）
对本机未认证订阅号恒定返回 ``ret=200013``，静默 420 秒后仍复现，属于权限封禁。

微信读书提供等价的公众号推文聚合接口，且不受该限制：

    POST https://i.weread.qq.com/mobileSync          # 激活终端会话（必需）
    GET  https://i.weread.qq.com/mp/chapters?...      # 公众号推文分页列表
    GET  https://i.weread.qq.com/mp/cover?...         # 置顶/最新一篇
    GET  https://i.weread.qq.com/review/single?...    # 单篇详情（含原始微信链接）

关键约束（均经实测确认）：
  * ``wr_vid`` / ``wr_skey`` 必须放在 **Cookie** 头，不能只当普通 header；
  * ``/mp/chapters`` 在会话未激活时返回 HTTP 499 + ``errcode=-2041``，
    必须先 POST ``/mobileSync`` 激活，且该激活是**短效**的，
    因此每次抓取前都要重新激活；
  * ``bookId`` = ``MP_WXS_{uin}``，由公众号 ``fakeid``（base64 的 __biz）解码得到。

凭据来源：微信读书扫码登录（见 scripts/macos/weread_login.py）。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from .weread_auth import WeReadRefreshFailed, refresh_credentials

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# AppService.mobileSync 的报活载荷：全部为 0 表示“不上报任何增量，仅刷新在线态”。
_MOBILE_SYNC_PAYLOAD = {
    "shelf": 0, "bookInventory": 0, "shelfLecture": 0, "notifications": 0,
    "config": 0, "gift": 0, "payItem": 0, "balance": 0.0, "reviewTimeline": 0,
    "follower": 0, "readingExchange": 0, "chat": 0, "chatRemovedSynckey": 0,
    "wechatFriend": 0, "inBackground": 0, "refluxSynckey": 0, "following": 0,
    "applyList": 0, "mcard": 0, "discoverFeedSynckey": 0, "rateSynckey": 0,
    "browse": 0, "medal": 0, "medalUrl": 0, "regionResource": 0,
    "discoverColumnSynckey": 0, "friendReviewSynckey": 0, "wehearSyncKey": 0,
    "preferTab": 1, "localBrowseTab": "", "localCommunityTab": "",
}


class WeReadSessionInactive(RuntimeError):
    """终端会话休眠（HTTP 499 / errcode -2041），且自动激活后仍未恢复。"""


class WeReadRateLimited(RuntimeError):
    """微信读书频控（errcode -2014 请求频率过高），应退避后重试。"""


class WeReadAuthExpired(RuntimeError):
    """微信读书凭据失效（HTTP 401），需要重新扫码。"""


@dataclass
class WeReadArticle:
    review_id: str
    title: str
    url: str
    digest: str = ""
    cover_url: str = ""
    author: str = ""
    publish_time: int = 0
    is_original: bool = False
    account_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "title": self.title,
            "url": self.url,
            "digest": self.digest,
            "cover_url": self.cover_url,
            "author": self.author,
            "publish_time": self.publish_time,
            "is_original": self.is_original,
            "account_name": self.account_name,
        }


def biz_to_book_id(fakeid: str) -> str:
    """base64 的 __biz / fakeid -> 微信读书 bookId (MP_WXS_{uin})。"""
    raw = urllib.parse.unquote(str(fakeid or "").strip())
    if raw.startswith("MP_"):
        return raw
    if len(raw) % 4:
        raw += "=" * (4 - len(raw) % 4)
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore").strip()
        if decoded.isdigit():
            return f"MP_WXS_{decoded}"
    except Exception:
        pass
    if raw.isdigit():
        return f"MP_WXS_{raw}"
    return raw


def short_id_from_review_id(review_id: str) -> str:
    """MP_WXS_{uin}_{shortid} -> {shortid}（对应 mp.weixin.qq.com/s/{shortid}）。

    微信读书返回的 reviewId 里，短 id 可能带 ``~`` 分隔符（例如
    ``MP_WXS_3236757533_T9n8Fqloo4ZcxjB~O00ULw``）。但微信短链的字符集只有
    ``[A-Za-z0-9_-]``，``~`` 形式的链接访问会直接返回「参数错误」，而把 ``~``
    换成 ``_`` 后能正常打开同一篇文章。因此这里统一把 ``~`` 规范化为 ``_``。

    短 id 本身理论上也可能包含 ``_``，所以只按前两段 ``_`` 切分 ``MP_WXS_``
    前缀，剩下部分按第一个 ``_`` 再切一次，避免把短 id 截断。
    """
    raw = str(review_id or "")
    prefix = "MP_WXS_"
    if not raw.startswith(prefix):
        return ""
    remainder = raw[len(prefix) :]
    uin, sep, short = remainder.partition("_")
    if not sep or not uin or not short:
        return ""
    return short.replace("~", "_")


class WeReadClient:
    """微信读书公众号推文抓取客户端。"""

    base_url = "https://i.weread.qq.com"

    def __init__(
        self,
        credentials: dict[str, Any],
        timeout_seconds: int = 20,
        on_refresh: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds
        # 续期成功后回调，用于把新凭据写回 vault。
        self.on_refresh = on_refresh
        self._refreshed = False
        self.session = requests.Session()
        self.session.trust_env = False

    # ---------- 凭据与请求 ----------

    def _headers(self) -> dict[str, str]:
        vid = str(self.credentials.get("vid", ""))
        skey = str(self.credentials.get("skey", ""))
        token = str(self.credentials.get("accessToken", "") or skey)
        return {
            "User-Agent": BROWSER_UA,
            "Content-Type": "application/json; charset=UTF-8",
            "vid": vid,
            "accessToken": token,
            "skey": skey,
            "Cookie": f"wr_vid={vid}; wr_skey={skey}",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.get(
                url, params=params, headers=self._headers(), timeout=self.timeout_seconds
            )
        except requests.RequestException as error:
            raise RuntimeError(f"微信读书请求失败: {error}") from error
        if response.status_code == 401:
            # accessToken 短效过期：用 refreshToken 静默换一份新的再试一次。
            if self._refresh_once():
                try:
                    response = self.session.get(
                        url, params=params, headers=self._headers(), timeout=self.timeout_seconds
                    )
                except requests.RequestException as error:
                    raise RuntimeError(f"微信读书请求失败: {error}") from error
            if response.status_code == 401:
                raise WeReadAuthExpired(f"微信读书凭据失效 (401): {path}")
        return response

    def _refresh_once(self) -> bool:
        """静默续期一次；成功则更新内存中的凭据并返回 True。"""
        if self._refreshed:
            return False
        try:
            refreshed = refresh_credentials(self.credentials, self.timeout_seconds)
        except WeReadRefreshFailed:
            return False
        self.credentials = refreshed
        self._refreshed = True
        if self.on_refresh is not None:
            try:
                self.on_refresh(refreshed)
            except Exception:  # noqa: BLE001 - 持久化失败不应中断抓取
                pass
        return True

    # ---------- 会话激活 ----------

    def activate(self) -> bool:
        """POST /mobileSync 激活终端会话，解除 /mp/chapters 的 499 (-2041) 拦截。"""
        headers = self._headers()
        try:
            self.session.post(
                f"{self.base_url}/mobileSync",
                json=_MOBILE_SYNC_PAYLOAD,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            return True
        except requests.RequestException:
            return False

    def _fetch_activated(self, path: str, params: dict[str, Any], attempts: int = 3) -> dict[str, Any]:
        """带自动激活重试的 GET。"""
        last_text = ""
        for attempt in range(attempts):
            response = self._get(path, params)
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    return {}
            last_text = response.text[:200]
            if response.status_code == 429 or "-2014" in last_text:
                # 微信读书频控（-2014 请求频率过高）。
                #
                # 只退避后重试**一次**：连续重试会不断刷新账号的频控窗口，
                # 反而把冷却越拖越长。参考实现 wechat-mp-tools 也是
                # "退避 1.5 秒重试一次，仍失败就交给上层熔断冷却"。
                #
                # 注意顺序：-2014 可能也带 HTTP 499，必须先判定频控，
                # 否则会给一个被限流的账号反复 mobileSync 激活并重试。
                if attempt == 0:
                    time.sleep(1.5)
                    continue
                break
            if response.status_code == 499 or "-2041" in last_text:
                # 会话休眠 -> 重新激活后再试
                self.activate()
                time.sleep(1.2)
                continue
            raise RuntimeError(f"微信读书返回 {response.status_code}: {last_text}")
        if "-2014" in last_text:
            raise WeReadRateLimited(f"微信读书频控 (-2014): {last_text}")
        raise WeReadSessionInactive(f"微信读书会话持续休眠: {last_text}")

    # ---------- 文章列表 ----------

    def fetch_articles(
        self,
        fakeid: str,
        *,
        begin: int = 0,
        count: int = 20,
        account_name: str = "",
    ) -> tuple[list[WeReadArticle], int]:
        """抓取一个公众号的推文列表。

        返回 (文章列表, 接口报告的总数)。总数无法取得时为 -1。
        """
        book_id = biz_to_book_id(fakeid)
        if not book_id.startswith("MP_WXS_"):
            raise ValueError(f"无法从 fakeid 推导 book_id: {fakeid!r}")

        # 通道 1（首选）：mp/chapters 官方分页推文列表
        items: list[dict[str, Any]] = []
        total = -1
        try:
            payload = self._fetch_activated(
                "/mp/chapters",
                {"bookId": book_id, "count": count, "offset": begin, "pf": "android", "synckey": 0},
            )
            items = payload.get("data") or payload.get("chapters") or payload.get("list") or []
            try:
                total = int(payload.get("totalCount", -1))
            except (TypeError, ValueError):
                total = -1
        except WeReadRateLimited:
            # 账号被频控：必须上抛，让账号池冷却该账号并换号。
            #
            # 以前这里把频控降级成"0 篇"，于是账号池永远不会换号，
            # 整个采集静默返回空页，看起来像"公众号没有文章"。
            raise
        except WeReadSessionInactive:
            # 会话休眠：退到 article/list
            try:
                items, total = self._fetch_via_article_list(book_id)
            except (WeReadRateLimited, WeReadSessionInactive):
                items, total = [], -1

        if not items and begin == 0:
            # 通道 3：兜底取置顶/最新一篇（mp/cover 成功率最高）
            try:
                cover = self.fetch_cover(fakeid, account_name=account_name)
            except (WeReadRateLimited, WeReadSessionInactive, WeReadAuthExpired):
                cover = None
            if cover is not None:
                return [cover], total if total >= 0 else 1

        articles = [self._to_article(item, account_name) for item in items]
        articles = [article for article in articles if article.title]
        return articles, total

    def _fetch_via_article_list(self, book_id: str) -> tuple[list[dict[str, Any]], int]:
        """备用通道：article/list。它不受 mp/chapters 的分页频控影响。"""
        payload = self._fetch_activated("/article/list", {"bookId": book_id, "synckey": 0})
        items = payload.get("reviews") or payload.get("data") or []
        try:
            total = int(payload.get("totalCount", -1))
        except (TypeError, ValueError):
            total = -1
        return list(items), total

    def fetch_cover(self, fakeid: str, *, account_name: str = "") -> WeReadArticle | None:
        """抓取置顶/最新一篇（mp/cover）。"""
        book_id = biz_to_book_id(fakeid)
        if not book_id.startswith("MP_WXS_"):
            return None
        payload = self._fetch_activated("/mp/cover", {"bookId": book_id, "synckey": 0})
        review_id = payload.get("reviewId", "")
        if not review_id:
            return None
        article = self._to_article({"reviewId": review_id, "mpInfo": payload}, account_name)
        if not article.title:
            article.title = payload.get("title", "")
        if not article.cover_url:
            article.cover_url = payload.get("pic", "")
        detail = self.fetch_detail(review_id)
        article.url = detail.get("doc_url") or article.url
        article.digest = detail.get("digest") or article.digest
        article.publish_time = detail.get("create_time") or article.publish_time
        article.cover_url = detail.get("cover") or article.cover_url
        article.author = detail.get("author") or article.author
        return article

    def fetch_detail(self, review_id: str) -> dict[str, Any]:
        """单篇详情：解析出原始 mp.weixin.qq.com 链接、时间戳、摘要。"""
        payload = self._fetch_activated("/review/single", {"reviewId": review_id})
        review = payload.get("review") or {}
        mp_info = review.get("mpInfo") or {}
        doc_url = mp_info.get("doc_url", "")
        original_id = mp_info.get("originalId", "")
        if not doc_url and original_id:
            doc_url = f"https://mp.weixin.qq.com/s/{original_id}"
        if doc_url and "__biz=" in doc_url:
            # 长链在微信网关易触发风控，优先换成短链
            short = short_id_from_review_id(review_id)
            if short:
                doc_url = f"https://mp.weixin.qq.com/s/{short}"
        return {
            "doc_url": doc_url,
            "title": mp_info.get("title") or review.get("title", ""),
            "digest": str(mp_info.get("content") or mp_info.get("digest") or ""),
            "author": mp_info.get("author", ""),
            "cover": mp_info.get("pic_url", ""),
            "create_time": int(
                review.get("createTime")
                or review.get("updateTime")
                or mp_info.get("time")
                or mp_info.get("create_time")
                or 0
            ),
        }

    def fetch_summary(self, review_id: str) -> str:
        """取单篇微信摘要。

        ``/review/single`` 的 ``review.mpInfo.content`` 才是真正的微信摘要；
        ``mpInfo.digest`` 这个字段并不存在，``review.digest`` 为空。
        """
        payload = self._fetch_activated("/review/single", {"reviewId": review_id})
        review = payload.get("review") or {}
        mp_info = review.get("mpInfo") or {}
        return str(mp_info.get("content") or mp_info.get("digest") or "").strip()

    def _to_article(self, item: dict[str, Any], account_name: str) -> WeReadArticle:
        mp_info = item.get("mpInfo") if isinstance(item.get("mpInfo"), dict) else {}
        mp_info = mp_info or {}
        review_id = str(item.get("reviewId") or "")
        short = short_id_from_review_id(review_id)
        url = f"https://mp.weixin.qq.com/s/{short}" if short else mp_info.get("doc_url", "")
        publish_time = int(
            item.get("createTime")
            or item.get("updateTime")
            or item.get("publishTime")
            or mp_info.get("time")
            or mp_info.get("create_time")
            or 0
        )
        return WeReadArticle(
            review_id=review_id,
            title=str(mp_info.get("title") or item.get("title") or "").strip(),
            url=url,
            digest=str(mp_info.get("digest") or ""),
            cover_url=str(mp_info.get("pic_url") or ""),
            author=str(mp_info.get("author") or ""),
            publish_time=publish_time,
            account_name=account_name,
        )


def load_credentials(path: str | Path) -> dict[str, Any]:
    """读取微信读书凭据 JSON。"""
    return json.loads(Path(path).read_text())