import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_list_collector.models import Credentials
from wechat_list_collector.wechat_client import WeChatListClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.payload)


class WeChatClientTests(unittest.TestCase):
    def test_parses_article_list_without_fetching_content(self):
        publish_page = {"publish_list": [{"publish_info": json.dumps({"appmsgex": [{"aid": "A1", "title": "Title", "link": "https://example.test/a", "digest": "Summary", "cover": "https://example.test/cover", "update_time": 100}]})}]}
        session = FakeSession({"base_resp": {"ret": 0}, "publish_page": json.dumps(publish_page)})
        client = WeChatListClient(Credentials("token", "a=b"), 30, session=session)
        articles = client.fetch_articles("feed-1", "fakeid", pages=1, page_size=5)
        self.assertEqual(1, len(articles))
        self.assertEqual("A1", articles[0].id)
        self.assertEqual("feed-1", articles[0].feed_id)
        self.assertEqual("https://mp.weixin.qq.com/cgi-bin/appmsgpublish", session.calls[0][0])

    def test_expands_multiple_articles_from_one_publish_record(self):
        publish_info = {
            "appmsgex": [
                {"aid": "A1", "title": "First", "link": "https://example.test/a", "update_time": 100},
                {"aid": "A2", "title": "Second", "link": "https://example.test/b", "update_time": 101},
            ]
        }
        publish_page = {"publish_list": [{"publish_info": json.dumps(publish_info)}]}
        session = FakeSession({"base_resp": {"ret": 0}, "publish_page": json.dumps(publish_page)})
        client = WeChatListClient(Credentials("token", "a=b"), 30, session=session)

        articles = client.fetch_articles("feed-1", "fakeid", pages=1, page_size=5)

        self.assertEqual(["A1", "A2"], [article.id for article in articles])
