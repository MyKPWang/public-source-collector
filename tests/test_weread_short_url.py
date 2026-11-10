"""微信读书短 id -> 微信短链的规范化规则。

微信短链字符集只有 ``[A-Za-z0-9_-]``；微信读书的 reviewId 里出现的 ``~``
必须规范化为 ``_``，否则生成的链接访问时只会返回「参数错误」。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.infrastructure.weread.weread_client import (
    short_id_from_review_id,
)


class ShortIdFromReviewIdTests(unittest.TestCase):
    def test_plain_short_id_is_unchanged(self):
        self.assertEqual(
            "16LU9gk7AkZPYdwfWDTPqQ",
            short_id_from_review_id("MP_WXS_3236757533_16LU9gk7AkZPYdwfWDTPqQ"),
        )

    def test_tilde_is_normalized_to_underscore(self):
        self.assertEqual(
            "T9n8Fqloo4ZcxjB_O00ULw",
            short_id_from_review_id("MP_WXS_3236757533_T9n8Fqloo4ZcxjB~O00ULw"),
        )

    def test_short_id_starting_with_dash_is_preserved(self):
        self.assertEqual(
            "-sTITModceMfVPCwqpFxTw",
            short_id_from_review_id("MP_WXS_3236757533_-sTITModceMfVPCwqpFxTw"),
        )

    def test_generated_wechat_url_never_contains_tilde(self):
        short = short_id_from_review_id("MP_WXS_3236757533_pnV01H~SN7jkGa-qpH-lXA")
        self.assertNotIn("~", f"https://mp.weixin.qq.com/s/{short}")

    def test_review_id_without_short_part_returns_empty(self):
        self.assertEqual("", short_id_from_review_id("MP_WXS_3236757533"))
        self.assertEqual("", short_id_from_review_id(""))


if __name__ == "__main__":
    unittest.main()
