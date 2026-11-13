"""公众号文章补采：从官方短链恢复漏采文章。

背景
----
微信读书的 ``/mp/chapters`` 对单个公众号账号有服务端展示上限（实测 APPSO
稳定为每天 3 条），且 ``synckey`` 翻页不推进、``/article/list`` 恒返回
``reviews=[]``，因此微信读书会稳定漏掉同一公众号当天发布的其余文章。

本模块提供一条**不依赖微信读书接口**的补采链路：

    官方短链（无需鉴权）
        └── 解析页面 var mid/idx/sn/biz   → 得到该文的官方永久链接坐标
        └── 解析页面正文「🔗 相关阅读」    → 得到同账号其它文章的永久链接
        └── 带账号级 chksm 请求永久链接      → 补采被微信读书漏掉的文章

实测结论（均已验证）
--------------------
* 短链 ``https://mp.weixin.qq.com/s/{shortid}`` **无需任何鉴权**即可访问，
  页面内直接给出 ``var mid`` / ``var idx`` / ``var sn`` / ``var biz``。
* 永久链接 ``/s?__biz=..&mid=..&idx=..&sn=..`` **必须同时带** ``sn`` 与
  ``chksm``；缺 ``sn`` 返回「参数错误」，缺 ``chksm`` 返回「环境异常」。
* ``sn`` 绑定 ``(mid, idx)``，换 ``idx`` 复用同一 ``sn`` 会失败。
* ``chksm`` 是**账号级**的：同一公众号内跨文章、跨日期均可复用。
* 账号级 ``chksm`` 会出现在该账号任意一篇文章页面的 canonical ``msg_link``
  里，因此它可以被自动学习并落盘，无需硬编码。

安全约束
--------
``chksm`` 与 ``sn`` 属于访问凭据。本模块把它们放在
:class:`PermalinkTokenStore` 里，由加密 vault 持久化，**不写进源码、
不写进日志、不写进结果文件**。
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from ...domain.models import ArticleInput

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 抓取间隔：官方对高频访问会返回「环境异常」，因此固定节流并带抖动。
DEFAULT_MIN_INTERVAL_SECONDS = 4.0
DEFAULT_JITTER_SECONDS = 2.0

_PERMALINK_RE = re.compile(
    r"https?://mp\.weixin\.qq\.com/s\?__biz=([A-Za-z0-9%=_]+)([^\s\"'<>]*)"
)
_CANONICAL_RE = re.compile(r"msg_link\s*=\s*['\"]([^'\"]+)")
_CREATE_TIME_RE = re.compile(r"create_time:\s*'([^']+)'")
_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"')
_OG_DESC_RE = re.compile(r'<meta\s+property="og:description"\s+content="([^"]*)"')
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]*)"')
_OG_URL_RE = re.compile(r'<meta\s+property="og:url"\s+content="([^"]*)"')

# ``var`` 声明形如 ``var biz = "" || "MjM5MjAyNDUyMA==``：真实值在第二个
# 引号里。页面里同一个变量名会出现多次（还有 ``var sn = opt.sn || window.sn``
# 这类噪声），因此要遍历所有出现位置并挑出第一个「像样」的引号值。
_QUOTED_RE = re.compile(r"['\"]([^'\"\n]{1,200})['\"]")

_VALIDATORS = {
    "biz": re.compile(r"^[A-Za-z0-9%=_]+$"),
    "mid": re.compile(r"^\d{8,}$"),
    "idx": re.compile(r"^\d{1,3}$"),
    "sn": re.compile(r"^[0-9a-f]{32}$"),
    "ct": re.compile(r"^\d{10}$"),
}


def _normalize_separators(text: str) -> str:
    """把各种分隔符写法统一成 ``&``。

    微信页面里同一篇链接可能出现多种转义层数：
    ``&`` / ``&amp;`` / ``&amp;amp;`` / ``\\x26`` / ``\\x26amp;``，
    必须反复替换直到收敛，否则解析出的参数会带上 ``amp;`` 尾巴。
    """
    previous = None
    while previous != text:
        previous = text
        text = text.replace("\\x26amp;", "&").replace("\\x26", "&").replace("&amp;", "&")
    return text


def _normalize_biz(biz: str) -> str:
    """归一化 ``__biz``。

    同一公众号的 ``__biz`` 在不同页面可能写成明文（``...MA==``）或百分号
    编码（``...MA%3D%3D``）。两者必须归一到同一个 key，否则账号级 ``chksm``
    会被拆成两条记录，导致补采时拿不到凭据。
    """
    value = urllib.parse.unquote(_unescape(biz or ""))
    return value.strip()


def _parse_params(query: str) -> dict[str, str]:
    """解析查询串参数（分隔符已统一成 ``&``）。"""
    params: dict[str, str] = {}
    for name, value in re.findall(r"([a-z_]+)=([^&\s\"'<>]+)", query):
        params[name] = value
    return params


def _var_value(body: str, name: str) -> str:
    """从 ``var <name> = ...`` 声明里取出真实值。

    真实页面里取值写成 ``var sn = "" || "fb40be..." || ""``，是「或」表达式，
    真实值在其中一个分支里。直接按引号配对会误配到 ``" || "`` 这种噪声，
    因此先按 ``||`` 拆分支，再在每个分支里找引号内容。
    """
    pattern = re.compile(r"var\s+" + name + r"\s*=\s*([^;\n]{0,240})")
    validator = _VALIDATORS.get(name)
    for match in pattern.finditer(body):
        region = match.group(1)
        for branch in region.split("||"):
            for quoted in _QUOTED_RE.findall(branch):
                value = quoted.strip()
                if not value:
                    continue
                if validator is None or validator.match(value):
                    return value
    return ""


_JS_HEX_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _decode_js_escapes(text: str) -> str:
    """还原微信 og 标签里的 JS 十六进制转义（如 ``\\x0d\\x0a`` 换行）。"""
    if "\\x" not in text:
        return text
    return _JS_HEX_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _unescape(text: str) -> str:
    """还原微信页面里的多层转义（``\\x26amp;`` / ``&amp;`` / ``\\x26``）。"""
    return (
        text.replace("\\x26amp;", "&")
        .replace("&amp;", "&")
        .replace("\\x26", "&")
        .replace("\\x3d", "=")
        .replace("\\x3D", "=")
        .replace("\\", "")
    )


@dataclass(frozen=True)
class PermalinkCoords:
    """一篇微信文章的官方永久链接坐标。"""

    biz: str
    mid: str
    idx: str
    sn: str

    def key(self) -> tuple[str, str, str, str]:
        return (self.biz, self.mid, self.idx, self.sn)


@dataclass(frozen=True)
class RecoveredArticle:
    """从官方页面恢复出来的文章元数据。"""

    title: str
    url: str
    digest: str
    cover_url: str
    published_at: str
    biz: str
    mid: str
    idx: str
    source: str

    def source_article_id(self) -> str:
        """稳定的文章标识，用作 ``articles.source_article_id``。

        必须稳定且唯一：同一篇文章重复补采时才能命中同一条记录，而不是新建。
        """
        return f"MP_{self.biz}_{self.mid}_{self.idx}"


class PermalinkTokenStore:
    """账号级 ``chksm`` 的加密持久化。

    ``chksm`` 只能从页面里学到，而页面本身要用 ``chksm`` 才能取到——这是个
    自举问题。解法是：**短链无需鉴权**，短链页面的 canonical ``msg_link``
    就带着该账号可用的 ``chksm``。所以只要先抓一篇短链，就能自动学到
    ``chksm`` 并继续扩展。
    """

    def __init__(self, vault: Any, path_hint: str = "wechat_tokens.enc"):
        self._vault = vault
        self._path_hint = path_hint
        self._tokens: dict[str, str] | None = None

    def _file_path(self) -> Path:
        base = Path(self._vault.credentials_path)
        return base.with_name(self._path_hint)

    def load(self) -> dict[str, str]:
        if self._tokens is not None:
            return dict(self._tokens)
        path = self._file_path()
        if not path.exists():
            self._tokens = {}
            return {}
        try:
            payload = json.loads(self._vault.decrypt_text(path.read_bytes()))
        except Exception:  # noqa: BLE001 - 凭据损坏时按无凭据处理
            self._tokens = {}
            return {}
        if not isinstance(payload, dict):
            self._tokens = {}
            return {}
        self._tokens = {str(k): str(v) for k, v in payload.items()}
        return dict(self._tokens)

    def save(self) -> None:
        if self._tokens is None:
            return
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(self._vault.encrypt_text(
            json.dumps(self._tokens, ensure_ascii=False, separators=(",", ":"))
        ))
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def get(self, biz: str) -> str:
        return self.load().get(biz, "")

    def put(self, biz: str, chksm: str) -> None:
        if not biz or not chksm:
            return
        self.load()
        assert self._tokens is not None
        if self._tokens.get(biz) != chksm:
            self._tokens[biz] = chksm
            self.save()

    def learn_from(self, body: str) -> None:
        """从页面里学习各账号的 ``chksm``。"""
        for match in _PERMALINK_RE.finditer(body):
            biz = _normalize_biz(match.group(1))
            query = _normalize_separators(_unescape(match.group(2)))
            params = _parse_params(query)
            chksm = params.get("chksm", "")
            if chksm:
                self.put(biz, chksm)


class PermalinkRecoveryClient:
    """抓取并解析微信官方文章页面。"""

    def __init__(
        self,
        tokens: PermalinkTokenStore,
        *,
        timeout_seconds: int = 30,
        min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS,
        jitter_seconds: float = DEFAULT_JITTER_SECONDS,
        sleep: Any = time.sleep,
        http_get: Any = None,
    ):
        self._tokens = tokens
        self._timeout_seconds = timeout_seconds
        self._min_interval = min_interval_seconds
        self._jitter = jitter_seconds
        self._sleep = sleep
        self._http_get = http_get or _default_http_get
        self._last_request_at = 0.0

    # -- 节流 ---------------------------------------------------------------

    def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            self._sleep(wait)
        self._last_request_at = time.monotonic()

    # -- HTTP ---------------------------------------------------------------

    def _get(self, url: str, referer: str | None = None) -> tuple[str, int]:
        self._throttle()
        body, status = self._http_get(url, self._timeout_seconds, referer)
        return body, status

    # -- 解析 ---------------------------------------------------------------

    @staticmethod
    def parse_permalinks(body: str) -> list[PermalinkCoords]:
        """从页面里抽取所有官方永久链接坐标（任意公众号）。"""
        found: dict[tuple[str, str, str, str], PermalinkCoords] = {}
        for match in _PERMALINK_RE.finditer(body):
            biz = _normalize_biz(match.group(1))
            query = _normalize_separators(_unescape(match.group(2)))
            params = _parse_params(query)
            mid = params.get("mid", "")
            sn = params.get("sn", "")
            if not mid or not sn:
                continue
            coords = PermalinkCoords(biz=biz, mid=mid, idx=params.get("idx", "1"), sn=sn)
            found.setdefault(coords.key(), coords)
        return list(found.values())

    @staticmethod
    def parse_self_coords(body: str) -> PermalinkCoords | None:
        """解析页面**自身**的永久链接坐标（来自 ``var mid/idx/sn/biz``）。"""
        biz = _normalize_biz(_var_value(body, "biz"))
        mid = _var_value(body, "mid")
        idx = _var_value(body, "idx")
        sn = _var_value(body, "sn")
        if not (biz and mid and idx and sn):
            return None
        return PermalinkCoords(biz=biz, mid=mid, idx=idx, sn=sn)

    @staticmethod
    def parse_metadata(body: str) -> dict[str, str]:
        """解析标题 / 摘要 / 封面 / 发布时间。"""
        title = _OG_TITLE_RE.search(body)
        digest = _OG_DESC_RE.search(body)
        image = _OG_IMAGE_RE.search(body)
        created = _CREATE_TIME_RE.search(body)
        if created is None:
            ct = _var_value(body, "ct")
            published = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ct))) if ct else ""
        else:
            published = created.group(1).strip()
        return {
            "title": _decode_js_escapes(html.unescape(title.group(1)).strip()) if title else "",
            "digest": _decode_js_escapes(html.unescape(digest.group(1)).strip()) if digest else "",
            "cover_url": html.unescape(image.group(1)).strip() if image else "",
            "published_at": published,
        }

    # -- 抓取 ---------------------------------------------------------------

    def fetch_short_link(self, short_url: str) -> tuple[dict[str, str], list[PermalinkCoords], PermalinkCoords | None]:
        """抓取官方短链（无需鉴权）。

        返回 ``(元数据, 页面内发现的永久链接, 自身永久链接坐标)``。
        """
        body, status = self._get(short_url)
        if status != 200 or not body:
            return {}, [], None
        self._tokens.learn_from(body)
        metadata = self.parse_metadata(body)
        metadata["_status"] = str(status)
        return metadata, self.parse_permalinks(body), self.parse_self_coords(body)

    def fetch_permalink(self, coords: PermalinkCoords) -> tuple[dict[str, str], list[PermalinkCoords], bool]:
        """抓取官方永久链接。

        返回 ``(元数据, 页面内发现的永久链接, 是否成功)``。
        """
        chksm = self._tokens.get(coords.biz)
        if not chksm:
            return {}, [], False
        query = urllib.parse.urlencode(
            {"__biz": coords.biz, "mid": coords.mid, "idx": coords.idx, "sn": coords.sn, "chksm": chksm}
        )
        url = f"https://mp.weixin.qq.com/s?{query}"
        body, status = self._get(url)
        if status != 200 or not body:
            return {}, [], False
        metadata = self.parse_metadata(body)
        if not metadata.get("title"):
            # 「参数错误」/「环境异常」页没有 og:title，视为失败。
            return {}, [], False
        self._tokens.learn_from(body)
        return metadata, self.parse_permalinks(body), True

    def permalink_url(self, coords: PermalinkCoords) -> str:
        """构造可对外展示的官方永久链接（不含 chksm）。"""
        query = urllib.parse.urlencode(
            {"__biz": coords.biz, "mid": coords.mid, "idx": coords.idx, "sn": coords.sn}
        )
        return f"https://mp.weixin.qq.com/s?{query}"


@dataclass
class RecoveryReport:
    """补采结果统计。"""

    seeds: int = 0
    fetched: int = 0
    new_articles: list[RecoveredArticle] = field(default_factory=list)
    failed: int = 0
    visited: set[tuple[str, str, str, str]] = field(default_factory=set)


class PermalinkRecoveryRunner:
    """补采编排：从短链种子出发做广度优先扩展。"""

    def __init__(
        self,
        client: PermalinkRecoveryClient,
        *,
        max_articles: int = 200,
        max_depth: int = 3,
    ):
        self._client = client
        self._max_articles = max_articles
        self._max_depth = max_depth

    def run(
        self,
        short_links: Iterable[tuple[str, str]],
        *,
        permalink_seeds: Iterable[PermalinkCoords] = (),
    ) -> RecoveryReport:
        """执行补采。

        ``short_links`` 是 ``(账号 biz, 短链 URL)`` 序列；短链不需要鉴权，
        因此这里不需要任何凭据即可启动。

        ``permalink_seeds`` 是已知坐标（``biz/mid/idx/sn``）的补充种子。
        只用短链做种子时，广度优先扩展到某一层就停住了 —— 因为短链页面里的
        「🔗 相关阅读」只覆盖作者手动挑过的文章。而库里已有的补采记录自带
        完整的永久链接（含 ``sn``），把它们也当种子，扩展就能继续往下走。
        """
        report = RecoveryReport()
        queue: list[tuple[int, PermalinkCoords, str]] = []
        for biz, short_url in short_links:
            report.seeds += 1
            metadata, links, self_coords = self._client.fetch_short_link(short_url)
            if not metadata.get("title"):
                report.failed += 1
                continue
            report.fetched += 1
            if self_coords is not None:
                report.new_articles.append(self._to_article(self_coords, metadata, "short"))
            for coords in links:
                if coords.key() not in report.visited:
                    report.visited.add(coords.key())
                    queue.append((1, coords, biz))
        for coords in permalink_seeds:
            report.seeds += 1
            key = coords.key()
            if key in report.visited:
                continue
            report.visited.add(key)
            queue.append((1, coords, coords.biz))
        self._expand(queue, report)
        return report

    def _expand(self, queue: list[tuple[int, PermalinkCoords, str]], report: RecoveryReport) -> None:
        while queue:
            depth, coords, _biz = queue.pop(0)
            if depth > self._max_depth or len(report.new_articles) >= self._max_articles:
                continue
            metadata, links, ok = self._client.fetch_permalink(coords)
            if not ok:
                report.failed += 1
                continue
            report.fetched += 1
            report.new_articles.append(self._to_article(coords, metadata, "related"))
            for child in links:
                key = child.key()
                if key in report.visited:
                    continue
                report.visited.add(key)
                queue.append((depth + 1, child, coords.biz))

    def _to_article(self, coords: PermalinkCoords, metadata: dict[str, str], source: str) -> RecoveredArticle:
        return RecoveredArticle(
            title=metadata.get("title", ""),
            url=self._client.permalink_url(coords),
            digest=metadata.get("digest", ""),
            cover_url=metadata.get("cover_url", ""),
            published_at=metadata.get("published_at", ""),
            biz=coords.biz,
            mid=coords.mid,
            idx=coords.idx,
            source=source,
        )


def to_article_inputs(
    articles: Iterable[RecoveredArticle],
    *,
    account_id: str,
) -> Iterator[ArticleInput]:
    """把恢复结果转成可入库的 ``ArticleInput``。

    微信页面的 ``create_time`` 是**北京时间**（UTC+8）且不带时区，而仓库统一
    存 UTC，必须显式补上 ``Asia/Shanghai`` 再由 repository 转成 UTC，
    否则会被 ``_timestamp`` 的时区校验拒绝。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    cst = ZoneInfo("Asia/Shanghai")
    for article in articles:
        if not article.title or not article.published_at:
            continue
        published_at: datetime | None = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                published_at = datetime.strptime(article.published_at, fmt)
                break
            except ValueError:
                continue
        if published_at is None:
            continue
        yield ArticleInput(
            source_article_id=article.source_article_id(),
            title=article.title,
            digest=article.digest,
            url=article.url,
            cover_url=article.cover_url,
            published_at=published_at.replace(tzinfo=cst),
            source_payload={
                "biz": article.biz,
                "mid": article.mid,
                "idx": article.idx,
                "recovery_source": article.source,
            },
        )


def _default_http_get(url: str, timeout_seconds: int, referer: str | None) -> tuple[str, int]:
    """默认 HTTP 抓取（用 curl，避免引入额外依赖）。"""
    import subprocess

    command = [
        "curl", "-sL", "--max-time", str(timeout_seconds),
        "-A", USER_AGENT,
        "-H", "Accept-Language: zh-CN,zh;q=0.9",
        "-w", "\n@@%{http_code}",
    ]
    if referer:
        command += ["-H", f"Referer: {referer}"]
    command.append(url)
    completed = subprocess.run(command, capture_output=True)
    # 微信页面混有非 UTF-8 字节，必须按字节切分状态码再容错解码，
    # 否则 text=True 会在解码阶段直接抛 UnicodeDecodeError。
    raw = completed.stdout
    marker = b"\n@@"
    position = raw.rfind(marker)
    if position < 0:
        return raw.decode("utf-8", errors="replace"), 0
    body = raw[:position]
    status_text = raw[position + len(marker):].strip()
    try:
        status = int(status_text)
    except ValueError:
        status = 0
    return body.decode("utf-8", errors="replace"), status
