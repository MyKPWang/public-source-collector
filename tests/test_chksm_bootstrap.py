"""外部种子自举 ``chksm`` 的回归测试。

背景：外部索引站（aipick）给出的永久链接自带 ``chksm``。以前抓取时只看
本地 token store，于是「不在账号池里」的公众号一个都抓不回来（实测
173 条种子里失败 114 条）。现在改为：

* 抓取时优先用 token store，没有就用链接自带的 ``chksm``；
* 抓通了才把这个 ``chksm`` 记进 token store，后续 BFS 扩展不用再依赖
  链接自带凭证；
* 抓失败（``参数错误`` / ``环境异常``）不写入，避免污染 token store。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
    PermalinkCoords,
    PermalinkRecoveryClient,
    PermalinkTokenStore,
)


def _load_script(name: str):
    """按路径加载 ``scripts/`` 下的模块（那个目录不是包）。"""
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_scripts_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BIZ = "MzI3MTA0MTk1MA=="


def _page(title: str) -> str:
    return f"""
<html><head>
<meta property="og:title" content="{title}">
<meta property="og:description" content="摘要-{title}">
<meta property="og:image" content="https://mmbiz.qpic.cn/cover.jpg">
</head><body>
<script>
var biz = "{BIZ}";
var mid = "2654706359";
var idx = "1";
var sn = "a7efdc18c8b597e6b82371a4dc12f32e";
var ct = "1763000000";
create_time: '2026-11-11 08:20'
</script>
</body></html>
"""


class FakeVault:
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
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    def __call__(self, url: str, timeout: int, referer: str | None) -> tuple[str, int]:
        self.requested.append(url)
        for key, body in self.pages.items():
            if key in url:
                return body, 200
        return "参数错误", 200


class ChksmBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault()
        self.tokens = PermalinkTokenStore(self.vault)

    def tearDown(self):
        self.vault.cleanup()

    def _client(self, http: FakeHttp) -> PermalinkRecoveryClient:
        return PermalinkRecoveryClient(
            self.tokens, http_get=http, sleep=lambda _: None, min_interval_seconds=0.0
        )

    def test_permalink_falls_back_to_coords_chksm(self):
        """token store 里没有该公众号时，用链接自带的 chksm 也能抓通。"""
        body = _page("标题甲")
        http = FakeHttp({"__biz": body})
        client = self._client(http)

        metadata, _links, ok = client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="2654706359", idx="1", sn="a" * 32, chksm="FROM_LINK")
        )

        self.assertTrue(ok)
        self.assertEqual(metadata["title"], "标题甲")
        self.assertIn("chksm=FROM_LINK", http.requested[0])

    def test_successful_fetch_records_chksm_in_store(self):
        """抓通后要把 chksm 记进 token store，后续扩展不再依赖链接自带。"""
        body = _page("标题甲")
        client = self._client(FakeHttp({"__biz": body}))

        client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="2654706359", idx="1", sn="a" * 32, chksm="FROM_LINK")
        )

        self.assertEqual(self.tokens.get(BIZ), "FROM_LINK")

    def test_failed_fetch_does_not_record_chksm(self):
        """抓失败（环境异常页）不能把失效 chksm 写进 token store。"""
        client = self._client(FakeHttp({}))

        metadata, _links, ok = client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="2654706359", idx="1", sn="a" * 32, chksm="STALE")
        )

        self.assertFalse(ok)
        self.assertEqual(metadata, {})
        self.assertEqual(self.tokens.get(BIZ), "")

    def test_store_value_wins_over_coords_chksm(self):
        """token store 里的 chksm 优先（它是已经验证过的凭证）。"""
        self.tokens.put(BIZ, "FROM_STORE")
        http = FakeHttp({"__biz": _page("标题甲")})
        client = self._client(http)

        client.fetch_permalink(
            PermalinkCoords(biz=BIZ, mid="2654706359", idx="1", sn="a" * 32, chksm="FROM_LINK")
        )

        self.assertIn("chksm=FROM_STORE", http.requested[0])

    def test_parse_permalinks_extracts_chksm(self):
        body = (
            '<a href="https://mp.weixin.qq.com/s?__biz=' + BIZ +
            '&amp;mid=2654706359&amp;idx=1&amp;sn=' + "a" * 32 +
            '&amp;chksm=abc123">x</a>'
        )
        links = PermalinkRecoveryClient.parse_permalinks(body)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].chksm, "abc123")

    def test_parse_permalinks_without_chksm_is_empty(self):
        body = (
            '<a href="https://mp.weixin.qq.com/s?__biz=' + BIZ +
            '&amp;mid=2654706359&amp;idx=1&amp;sn=' + "a" * 32 + '">x</a>'
        )
        links = PermalinkRecoveryClient.parse_permalinks(body)
        self.assertEqual(links[0].chksm, "")


class HarvestKeepsChksmTests(unittest.TestCase):
    """harvest 必须把 aipick 链接里的 chksm 一起抽出来。"""

    @classmethod
    def setUpClass(cls):
        cls.harvest = _load_script("harvest_external_permalinks")

    def test_extract_keeps_chksm(self):
        text = (
            'https://mp.weixin.qq.com/s?__biz=' + BIZ +
            '&amp;mid=2654706359&amp;idx=1&amp;sn=' + "a" * 32 + '&amp;chksm=deadbeef'
        )
        seeds = self.harvest.extract_permalinks(text)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["chksm"], "deadbeef")
        self.assertEqual(seeds[0]["biz"], BIZ)

    def test_extract_handles_unicode_separator(self):
        text = (
            'https://mp.weixin.qq.com/s?__biz=' + BIZ +
            '&\\u0026mid=2654706359&\\u0026idx=1&\\u0026sn=' + "a" * 32 + '&\\u0026chksm=cafe'
        )
        seeds = self.harvest.extract_permalinks(text)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["chksm"], "cafe")

    def test_extract_without_chksm_omits_the_key(self):
        text = (
            'https://mp.weixin.qq.com/s?__biz=' + BIZ +
            '&mid=2654706359&idx=1&sn=' + "a" * 32
        )
        seeds = self.harvest.extract_permalinks(text)
        self.assertEqual(len(seeds), 1)
        self.assertNotIn("chksm", seeds[0])


class RecoverPassesChksmTests(unittest.TestCase):
    """recover 脚本必须把 chksm 透传给 ``PermalinkCoords``。"""

    @classmethod
    def setUpClass(cls):
        cls.recover = _load_script("recover_from_external_seeds")

    def test_coords_from_mapping_keeps_chksm(self):
        coords = self.recover._coords_from_mapping(
            {"biz": BIZ, "mid": "2654706359", "idx": "1", "sn": "a" * 32, "chksm": "cafe"}
        )
        assert coords is not None
        self.assertEqual(coords["chksm"], "cafe")

    def test_coords_from_mapping_without_chksm(self):
        coords = self.recover._coords_from_mapping(
            {"biz": BIZ, "mid": "2654706359", "idx": "1", "sn": "a" * 32}
        )
        assert coords is not None
        self.assertNotIn("chksm", coords)


class AccountNameTests(unittest.TestCase):
    """跨通道对齐账号靠的是公众号显示名，不是 biz。"""

    def test_parse_account_name_from_js_name(self):
        body = '<span id="js_name">\n   新智元\n</span>'
        from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
            _parse_account_name,
        )

        self.assertEqual(_parse_account_name(body), "新智元")

    def test_parse_account_name_falls_back_to_nickname(self):
        body = 'var nickname = "量子位";'
        from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
            _parse_account_name,
        )

        self.assertEqual(_parse_account_name(body), "量子位")

    def test_parse_account_name_ignores_generic_site_name(self):
        body = '<meta property="og:site_name" content="微信公众平台">'
        from wechat_list_collector.infrastructure.wechat.permalink_recovery import (
            _parse_account_name,
        )

        self.assertEqual(_parse_account_name(body), "")

    def test_metadata_carries_account_name(self):
        body = _page("标题甲").replace(
            "<html><head>", '<html><head><span id="js_name">差评</span>'
        )
        metadata = PermalinkRecoveryClient.parse_metadata(body)
        self.assertEqual(metadata["account_name"], "差评")

    def test_normalize_name_strips_whitespace(self):
        recover = _load_script("recover_from_external_seeds")
        self.assertEqual(recover._normalize_name(" 新智元 "), "新智元")
        self.assertEqual(recover._normalize_name("Apple 开发者"), "apple开发者")


if __name__ == "__main__":
    unittest.main()
