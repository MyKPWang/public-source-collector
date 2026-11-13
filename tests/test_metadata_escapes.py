"""回归测试：og 标签中的 JS 十六进制转义必须还原为真实字符。

微信 og:description 里常见 ``\\x0d\\x0a``（换行）与 ``\\x26``（&），
若不还原，摘要会写入库成字面 "\\x0d\\x0a"。
"""
import unittest

from wechat_list_collector.infrastructure.wechat.permalink_recovery import PermalinkRecoveryClient


class ParseMetadataEscapeTest(unittest.TestCase):
    def test_digest_hex_escape_decoded(self):
        body = (
            '<meta property="og:description" content="第一行\\x0d\\x0a第二行\\x0d\\x0a第三行"/>'
        )
        meta = PermalinkRecoveryClient.parse_metadata(body)
        self.assertNotIn("\\x", meta["digest"])
        self.assertIn("\n", meta["digest"])
        self.assertEqual(meta["digest"].count("\n"), 2)

    def test_title_hex_escape_decoded(self):
        body = '<meta property="og:title" content="标题\\x0d副标题"/>'
        meta = PermalinkRecoveryClient.parse_metadata(body)
        self.assertNotIn("\\x", meta["title"])
        self.assertIn("\r", meta["title"])

    def test_plain_text_unchanged(self):
        body = '<meta property="og:description" content="普通摘要，没有转义"/>'
        meta = PermalinkRecoveryClient.parse_metadata(body)
        self.assertEqual(meta["digest"], "普通摘要，没有转义")


if __name__ == "__main__":
    unittest.main()
