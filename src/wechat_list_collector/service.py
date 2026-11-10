from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from .config import Settings
from .errors import CooldownActive, RunInProgress, WeChatRateLimited, WeChatSessionExpired
from .logging_config import LOGGER
from .models import Feed, FetchResult
from .store import Store
from .throttle import RequestGate
from .wechat_client import WeChatListClient


class CollectorService:
    def __init__(self, store: Store, settings: Settings, client_factory=WeChatListClient):
        self.store = store
        self.settings = settings
        self.client_factory = client_factory
        self.gate = RequestGate(store, settings.minimum_interval_seconds, settings.cooldown_seconds)
        if not LOGGER.handlers:
            LOGGER.addHandler(logging.NullHandler())
        self.logger = LOGGER

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        run_id = self._begin_run("search", feed_count=0)
        status = "completed"
        result_count = 0
        message = ""
        try:
            self.logger.info("search_started", extra={"run_id": run_id, "operation": "search"})
            wait_seconds = self.gate.wait_for_turn()
            self._log_wait(run_id, "search", wait_seconds)
            results = self._client().search(keyword, limit)
            result_count = len(results)
            self.logger.info("search_completed", extra={"run_id": run_id, "operation": "search", "count": result_count})
            return results
        except WeChatRateLimited:
            cooldown_until = self.gate.record_rate_limit()
            status = "rate_limited"
            message = "WeChat returned 200013"
            self.logger.warning("rate_limited", extra={"run_id": run_id, "operation": "search", "cooldown_until": cooldown_until})
            raise
        except CooldownActive as error:
            status = "cooldown"
            message = "cooldown active"
            self.logger.warning("cooldown_active", extra={"run_id": run_id, "operation": "search", "cooldown_until": error.until})
            raise
        except WeChatSessionExpired:
            status = "session_expired"
            message = "WeChat returned 200003"
            self.logger.warning("session_expired", extra={"run_id": run_id, "operation": "search"})
            raise
        except Exception as error:
            status = "failed"
            message = type(error).__name__
            self.logger.error("run_failed", extra={"run_id": run_id, "operation": "search", "status": message})
            raise
        finally:
            self._finish_run(run_id, status, result_count, message)

    def fetch_all(self) -> list[FetchResult]:
        return self._fetch("fetch_all", self.store.list_feeds(enabled_only=True))

    def fetch_feed(self, feed_id: str) -> list[FetchResult]:
        feed = self.store.get_feed(feed_id)
        if feed is None:
            raise KeyError(f"unknown feed: {feed_id}")
        return self._fetch("fetch_feed", [feed])

    def _fetch(self, command: str, feeds: Sequence[Feed]) -> list[FetchResult]:
        run_id = self._begin_run(command, feed_count=len(feeds))
        status = "completed"
        message = ""
        total_articles = 0
        results: list[FetchResult] = []
        try:
            self.logger.info("run_started", extra={"run_id": run_id, "operation": command, "count": len(feeds)})
            client = self._client()
            for feed in feeds:
                try:
                    wait_seconds = self.gate.wait_for_turn()
                    self._log_wait(run_id, "list", wait_seconds, feed)
                    self.logger.info("request_started", extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name})
                    articles = client.fetch_articles(
                        feed_id=feed.id,
                        fakeid=feed.fakeid,
                        pages=self.settings.pages_per_feed,
                        page_size=self.settings.page_size,
                    )
                    stored = self.store.upsert_articles(articles)
                    total_articles += stored
                    results.append(FetchResult(feed.id, len(articles), stored, "ok"))
                    self.logger.info(
                        "articles_stored",
                        extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name, "count": stored},
                    )
                except WeChatRateLimited:
                    cooldown_until = self.gate.record_rate_limit()
                    status = "rate_limited"
                    message = "WeChat returned 200013"
                    results.append(FetchResult(feed.id, 0, 0, status))
                    self.logger.warning(
                        "rate_limited",
                        extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name, "cooldown_until": cooldown_until},
                    )
                    break
                except CooldownActive as error:
                    status = "cooldown"
                    message = "cooldown active"
                    results.append(FetchResult(feed.id, 0, 0, status))
                    self.logger.warning(
                        "cooldown_active",
                        extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name, "cooldown_until": error.until},
                    )
                    break
                except WeChatSessionExpired:
                    status = "session_expired"
                    message = "WeChat returned 200003"
                    results.append(FetchResult(feed.id, 0, 0, status))
                    self.logger.warning(
                        "session_expired",
                        extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name},
                    )
                    break
                except Exception as error:
                    status = "failed"
                    message = type(error).__name__
                    results.append(FetchResult(feed.id, 0, 0, status))
                    self.logger.error(
                        "feed_failed",
                        extra={"run_id": run_id, "operation": "list", "feed_id": feed.id, "feed_name": feed.name, "status": message},
                    )
                    break
            return results
        finally:
            self._finish_run(run_id, status, total_articles, message)

    def status(self) -> dict[str, int]:
        return {
            "last_request_at": self.store.get_state_int("last_request_at"),
            "cooldown_until": self.store.get_state_int("cooldown_until"),
            "enabled_feed_count": len(self.store.list_feeds(enabled_only=True)),
        }

    def _client(self) -> WeChatListClient:
        return self.client_factory(self.store.load_credentials(), self.settings.request_timeout_seconds)

    def _begin_run(self, command: str, feed_count: int) -> str:
        run_id = str(uuid.uuid4())
        self.store.start_run(run_id, command, feed_count)
        lease_seconds = max(
            300,
            (self.settings.minimum_interval_seconds + self.settings.request_timeout_seconds * self.settings.pages_per_feed) * max(feed_count, 1) + 600,
        )
        if not self.store.acquire_run_lock(run_id, lease_seconds):
            self.store.finish_run(run_id, "rejected", message="another local run owns the lease")
            self.logger.warning("run_rejected", extra={"run_id": run_id, "operation": command})
            raise RunInProgress("another local collection run still owns the SQLite lease")
        return run_id

    def _finish_run(self, run_id: str, status: str, article_count: int, message: str) -> None:
        self.store.finish_run(run_id, status, article_count, message)
        self.store.release_run_lock(run_id)
        self.logger.info("run_finished", extra={"run_id": run_id, "status": status, "count": article_count})

    def _log_wait(self, run_id: str, operation: str, wait_seconds: int, feed: Feed | None = None) -> None:
        if wait_seconds <= 0:
            return
        extra = {"run_id": run_id, "operation": operation, "wait_seconds": wait_seconds}
        if feed is not None:
            extra.update(feed_id=feed.id, feed_name=feed.name)
        self.logger.info("minimum_interval_waited", extra=extra)
