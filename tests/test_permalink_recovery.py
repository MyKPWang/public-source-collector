"""补采模块的单元测试。

用假 HTTP 层跑完整链路，不触网。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
    PermalinkCoords,
    PermalinkRecoveryClient,
    PermalinkRecoveryRunner,
    PermalinkTokenStore,
    RecoveredArticle,
    to_article_inputs,
)

BIZ = "MjM5MjAyNDUyMA=="


def _page(biz: str, mid: str, idx: str, sn: str, title: str, links: str = "") -> str:
    """构造一个形似微信文章页面的 HTML。"""
    return f"""
<html><head>
<meta property="og:title" content="{title}">
<meta property="og:description" content="摘要-{title}">
<meta property="og:image" content="https://mmbiz.qpic.cn/cover-{mid}.jpg">
</head><body>
<script>
var biz = "{biz}";
var mid = "{mid}";
var idx = "{idx}";
var sn = "{sn}";
var ct = "1763000000";
var msg_link = "http://mp.weixin.qq.com/s?__biz={biz}&amp;mid={mid}&amp;idx={idx}&amp;sn={sn}&amp;chksm=TOK_{biz}";
create_time: '2026-11-11 08:20'
</script>
{links}
</body></html>
"""


class FakeVault:
    """内存版 vault，接口与 ``EncryptedFileCredentialVault`` 一致。

    每个测试用独立的临时目录，避免 token 文件在测试之间互相污染。
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self._tempdir = tempfile.TemporaryDirectory()
        self._directory = Path(self._tempdir.name)

    def cleanup(self) -> None:
        self._tempdir.cleanup()

    def encrypt_text(self, value: str) -> bytes:
        return value.encode("utf-8")

    def decrypt_text(self, ciphertext: bytes) -> str:
        return ciphertext.decode("utf-8")

    @property
    def credentials_path(self) -> Path:
        return self._directory / "credentials.enc"


class FakeHttp:
    """按 URL 片段返回预置页面。"""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def __call__(self, url: str, timeout: int, referer: str | None) -> tuple[str, int]:
        self.requested.append(url)
        for key, body in self.pages.items():
            if key in url:
                return body, 200
        return "参数错误", 200


def _short_link(mid: str) -> str:
    return f"https://mp.weixin.qq.com/s/Short{mid}"


def _perm_url(biz: str, mid: str, idx: str, sn: str) -> str:
    return f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn={sn}"


class PermalinkRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault()
        self.tokens = PermalinkTokenStore(self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def _client(self, http: FakeHttp) -> PermalinkRecoveryClient:
        return PermalinkRecoveryClient(
            self.tokens, http_get=http, sleep=lambda _: None, min_interval_seconds=0.0
        )

    def test_short_link_yields_self_coords_and_learns_token(self):
        body = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲")
        client = self._client(FakeHttp({"Short1111111111": body}))

        metadata, links, coords = client.fetch_short_link(_short_link("1111111111"))

        self.assertEqual(metadata["title"], "标题甲")
        self.assertEqual(metadata["published_at"], "2026-11-11 08:20")
        self.assertIsNotNone(coords)
        assert coords is not None
        self.assertEqual(
            (coords.biz, coords.mid, coords.idx, coords.sn),
            (BIZ, "1111111111", "1", "a" * 32),
        )
        # 短链页面的 canonical msg_link 里带 chksm，应当被学到
        self.assertEqual(self.tokens.get(BIZ), f"TOK_{BIZ}")

    def test_permalink_requires_token(self):
        body = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲")
        client = self._client(FakeHttp({"__biz": body}))

        metadata, links, ok = client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="1111111111", idx="1", sn="a" * 32)
        )

        self.assertFalse(ok)
        self.assertEqual(metadata, {})

    def test_permalink_succeeds_with_learned_token(self):
        body = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲")
        self.tokens.put(BIZ, f"TOK_{BIZ}")
        client = self._client(FakeHttp({"__biz": body}))

        metadata, links, ok = client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="1111111111", idx="1", sn="a" * 32)
        )

        self.assertTrue(ok)
        self.assertEqual(metadata["title"], "标题甲")

    def test_related_links_are_collected(self):
        related = f'<a href="{_perm_url(BIZ, "2222222222", "1", "b" * 32)}">相关一</a>'
        related += f'<a href="{_perm_url(BIZ, "3333333333", "1", "c" * 32)}">相关二</a>'
        body = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲", related)
        client = self._client(FakeHttp({"Short1111111111": body}))

        metadata, links, coords = client.fetch_short_link(_short_link("1111111111"))

        mids = {c.mid for c in links}
        # 页面里共有三个永久链接：自身 + 两条「相关阅读」。
        self.assertEqual(mids, {"1111111111", "2222222222", "3333333333"})
        self.assertTrue(all(c.sn for c in links))

    def test_runner_expands_related_articles(self):
        related = f'<a href="{_perm_url(BIZ, "2222222222", "1", "b" * 32)}">相关一</a>'
        seed = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲", related)
        target = _page(BIZ, "2222222222", "1", "b" * 32, "标题乙")
        client = self._client(FakeHttp({"Short1111111111": seed, "mid=2222222222": target}))
        runner = PermalinkRecoveryRunner(client)

        report = runner.run([(BIZ, _short_link("1111111111"))])

        titles = {a.title for a in report.new_articles}
        self.assertIn("标题甲", titles)
        self.assertIn("标题乙", titles)

    def test_runner_produces_no_duplicate_source_ids(self):
        body = _page(BIZ, "1111111111", "1", "a" * 32, "标题甲")
        client = self._client(FakeHttp({"Short1111111111": body}))
        runner = PermalinkRecoveryRunner(client)

        report = runner.run([(BIZ, _short_link("1111111111"))])

        source_ids = [a.source_article_id() for a in report.new_articles]
        self.assertEqual(len(source_ids), len(set(source_ids)))

    def test_source_article_id_is_stable_and_distinct(self):
        a = RecoveredArticle("t", "u", "d", "c", "2026-11-11 08:20:00", BIZ, "111", "1", "short")
        b = RecoveredArticle("t", "u", "d", "c", "2026-11-11 08:20:00", BIZ, "111", "1", "related")
        c = RecoveredArticle("t", "u", "d", "c", "2026-11-11 08:20:00", BIZ, "111", "2", "short")
        self.assertEqual(a.source_article_id(), b.source_article_id())
        self.assertNotEqual(a.source_article_id(), c.source_article_id())

    def test_to_article_inputs_skips_unparsable_dates(self):
        good = RecoveredArticle("好", "u", "d", "c", "2026-11-11 08:20:00", BIZ, "1", "1", "short")
        bad = RecoveredArticle("坏", "u", "d", "c", "not-a-date", BIZ, "2", "1", "short")
        inputs = list(to_article_inputs([good, bad], account_id="acct-1"))
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0].source_article_id, f"MP_{BIZ}_1_1")
        self.assertEqual(inputs[0].title, "好")

    def test_learn_from_ignores_links_without_chksm(self):
        # 只含 __biz/mid/idx/sn、不含 chksm 的链接不该产生凭据
        self.tokens.learn_from(f'<a href="{_perm_url(BIZ, "1", "1", "s" * 32)}">x</a>')
        self.assertEqual(self.tokens.get(BIZ), "")

    def test_learn_from_ignores_amp_encoded_links_without_chksm(self):
        url = _perm_url(BIZ, "1", "1", "s" * 32).replace("&", "&amp;")
        self.tokens.learn_from(f'<a href="{url}">x</a>')
        self.assertEqual(self.tokens.get(BIZ), "")

    def test_token_store_round_trips_through_vault(self):
        store = PermalinkTokenStore(self.vault)
        store.put(BIZ, "TOK")
        reloaded = PermalinkTokenStore(self.vault)
        self.assertEqual(reloaded.get(BIZ), "TOK")


class RealPageFormatTests(unittest.TestCase):
    """按真实微信页面的写法构造 HTML，验证解析器能扛住实际格式。

    真实页面里的关键差异（都已从抓取到的真实 HTML 里核对过）：

    * 取值写成「或」表达式：``var biz = "" || "MjM5MjAyNDUyMA=="``；
    * 分隔符有多种转义层数：``&`` / ``&amp;`` / ``&amp;amp;`` / ``\\x26``；
    * 同一个变量名会出现多次，混着 ``var sn = opt.sn || window.sn`` 之类噪声。
    """

    def test_parses_or_expression_vars(self):
        body = (
            'var biz = "" || "MjM5MjAyNDUyMA==" || ""\n'
            'var mid = "" || "2651116032" || ""\n'
            'var idx = "" || "1" || ""\n'
            'var sn = "" || "fb40be8c5739a6cc4bb81c5b44b3ca3f" || ""\n'
            'var ct = "1794358800"'
        )
        coords = PermalinkRecoveryClient.parse_self_coords(body)
        self.assertIsNotNone(coords)
        assert coords is not None
        self.assertEqual(coords.biz, "MjM5MjAyNDUyMA==")
        self.assertEqual(coords.mid, "2651116032")
        self.assertEqual(coords.idx, "1")
        self.assertEqual(coords.sn, "fb40be8c5739a6cc4bb81c5b44b3ca3f")

    def test_ignores_noisy_var_declarations(self):
        from wechat_list_collector.infrastructure.wechat.permalink_recovery import _var_value

        body = (
            'var sn={deltaE76:function(t,e){return ar(t,e,"lab")}}\n'
            'var sn = opt.sn || window.sn\n'
            'var sn = g$2.appidSnInfo[i].sn\n'
            'var sn = "6a6ef227aad445fb9da2862caf60cb82" || "" || ""'
        )
        self.assertEqual(_var_value(body, "sn"), "6a6ef227aad445fb9da2862caf60cb82")

    def test_parses_amp_amp_escaped_links(self):
        body = (
            '<a href="https://mp.weixin.qq.com/s?__biz=MjM5MjAyNDUyMA=='
            '&amp;amp;mid=2652396877&amp;amp;idx=2&amp;amp;sn=' + "d" * 32 + '">x</a>'
        )
        links = PermalinkRecoveryClient.parse_permalinks(body)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].mid, "2652396877")
        self.assertEqual(links[0].idx, "2")

    def test_parses_hex_escaped_links(self):
        body = (
            '<a href="https://mp.weixin.qq.com/s?__biz=MjM5MjAyNDUyMA=='
            '\\x26amp;mid=2651116031\\x26amp;idx=1\\x26amp;sn=' + "e" * 32 + '">x</a>'
        )
        links = PermalinkRecoveryClient.parse_permalinks(body)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].mid, "2651116031")

    def test_normalizes_percent_encoded_biz(self):
        body = (
            '<a href="https://mp.weixin.qq.com/s?__biz=MjgzMTAwODI0MA%3D%3D'
            '&amp;mid=2652474903&amp;idx=1&amp;sn=' + "f" * 32 + '">x</a>'
        )
        links = PermalinkRecoveryClient.parse_permalinks(body)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].biz, "MjgzMTAwODI0MA==")

    def test_learns_chksm_from_amp_encoded_canonical_link(self):
        body = (
            '<meta property="og:url" content="https://mp.weixin.qq.com/s?__biz=MjM5MjAyNDUyMA=='
            '&amp;mid=2651116032&amp;idx=1&amp;sn=' + "a" * 32 +
            '&amp;chksm=abc123" />'
        )
        vault = FakeVault()
        try:
            tokens = PermalinkTokenStore(vault)
            tokens.learn_from(body)
            self.assertEqual(tokens.get("MjM5MjAyNDUyMA=="), "abc123")
        finally:
            vault.cleanup()


if __name__ == "__main__":
    unittest.main()
