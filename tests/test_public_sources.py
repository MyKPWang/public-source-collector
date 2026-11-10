from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from wechat_list_collector.application.public_read_models import PublicReadModel
from wechat_list_collector.application.public_sources import (
    DetailEnrichmentAttempt,
    EndpointDefinition,
    HybridListFetcher,
    ListingItem,
    PublicSourceCollector,
    PublicSourceError,
    RequestsListFetcher,
    SourceDefinition,
    _parse_feed,
    _run_async_safely,
    _summary_from_detail,
    browser_adapter_for,
    browser_network_adapter_for,
    browser_cards_to_items,
    json_payload_to_items,
    parse_published_at,
)
from wechat_list_collector.entrypoints.read_api import create_read_api
from wechat_list_collector.infrastructure.db import PublicSourceRepository, create_sqlite_engine, upgrade_database
from wechat_list_collector.infrastructure.db.schema import public_source_articles


class RecordingFetcher:
    def __init__(self, items: list[ListingItem]):
        self.items = items
        self.requested_urls: list[str] = []

    def fetch(self, endpoint: dict[str, object]) -> list[ListingItem]:
        self.requested_urls.append(str(endpoint["list_url"]))
        return self.items


class RecordingDetailEnricher:
    def __init__(self, attempts: list[DetailEnrichmentAttempt]):
        self.attempts = attempts
        self.candidates: list[ListingItem] = []

    def enrich(self, endpoint: dict[str, object], items: list[ListingItem]) -> list[DetailEnrichmentAttempt]:
        self.candidates = items
        return self.attempts


class FailingDetailEnricher:
    def enrich(self, endpoint: dict[str, object], items: list[ListingItem]) -> list[DetailEnrichmentAttempt]:
        raise RuntimeError("browser unavailable")


class OneEndpointFailsFetcher:
    def fetch(self, endpoint: dict[str, object]) -> list[ListingItem]:
        if endpoint["source_slug"] == "blocked-source":
            raise PublicSourceError("verification screen", reason_code="LIST_ACCESS_BLOCKED")
        return [ListingItem("one", "One", "Summary", "https://example.invalid/one", datetime(2026, 8, 5, 8, tzinfo=UTC))]


class PublicSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database = Path(self.temp_dir.name) / "collector.db"
        upgrade_database(database)
        self.engine = create_sqlite_engine(database)
        self.now = datetime(2026, 8, 5, 16, tzinfo=UTC)
        self.repository = PublicSourceRepository(self.engine, now=lambda: self.now)
        self.catalog = [
            SourceDefinition(
                slug="example-news",
                name="Example News",
                homepage_url="https://example.invalid/",
                enabled=True,
                endpoints=(
                    EndpointDefinition(
                        key="rss", kind="rss", list_url="https://example.invalid/feed.xml", enabled=True,
                        minimum_interval_seconds=900, max_items=50,
                    ),
                ),
            )
        ]

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_collects_list_metadata_without_opening_article_urls(self) -> None:
        article_url = "https://example.invalid/articles/one"
        fetcher = RecordingFetcher(
            [
                ListingItem("one", "One", "A useful summary", article_url, datetime(2026, 8, 5, 8, tzinfo=UTC)),
                ListingItem("bad", "Bad", "", "https://example.invalid/articles/bad", datetime(2026, 8, 5, 8, tzinfo=UTC)),
            ]
        )
        collector = PublicSourceCollector(self.repository, fetcher)
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="scheduled", idempotency_key="daily:2026-08-05", business_date="2026-08-05")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(fetcher.requested_urls, ["https://example.invalid/feed.xml"])
        self.assertNotIn(article_url, fetcher.requested_urls)

        client = TestClient(create_read_api(PublicReadModel(self.engine, now=lambda: self.now), bearer_token="token", schema_version="2.0"))
        response = client.get(
            "/api/v1/pipeline/articles",
            params={
                "published_from": "2026-08-05T00:00:00Z",
                "published_before": "2026-08-06T00:00:00Z",
                "batch_id": str(result["id"]),
            },
            headers={"Authorization": "Bearer token"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "2.0")
        self.assertEqual(payload["batch"]["id"], str(result["id"]))
        self.assertEqual(len(payload["articles"]), 1)
        self.assertEqual(payload["articles"][0]["source"]["slug"], "example-news")
        self.assertEqual(payload["articles"][0]["digest"], "A useful summary")

    def test_diagnostics_expose_missing_field_counts_and_safe_samples(self) -> None:
        fetcher = RecordingFetcher(
            [
                ListingItem("good", "Good", "Summary", "https://example.invalid/good", datetime(2026, 8, 5, 8, tzinfo=UTC)),
                ListingItem("no-digest", "No digest", "", "https://example.invalid/no-digest", datetime(2026, 8, 5, 8, tzinfo=UTC)),
                ListingItem("no-time", "No time", "Summary", "https://example.invalid/no-time", None),
            ]
        )
        collector = PublicSourceCollector(self.repository, fetcher)
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:field-audit")

        endpoint = self.repository.diagnostics(str(result["id"]))["endpoints"][0]
        self.assertEqual({"digest": 1, "published_at": 1}, endpoint["rejection_summary"])
        self.assertEqual(["digest"], endpoint["rejection_samples"][0]["missing_fields"])
        self.assertEqual("No digest", endpoint["rejection_samples"][0]["title"])

    def test_rss_fetcher_recovers_from_malformed_xml_with_tolerant_parser(self) -> None:
        malformed_feed = b"""<?xml version='1.0'?><rss><channel><item><guid>one</guid><title>Fish & Chips</title><link>https://example.invalid/one</link><description>Summary</description><pubDate>Tue, 05 Aug 2026 08:00:00 +0000</pubDate></item></channel></rss>"""

        class Response:
            content = malformed_feed

            @staticmethod
            def raise_for_status() -> None:
                return None

        with patch("wechat_list_collector.application.public_sources.requests.get", return_value=Response()):
            items = RequestsListFetcher(timeout_seconds=20, user_agent="test").fetch(
                {"kind": "rss", "list_url": "https://example.invalid/feed", "max_items": 10, "source_slug": "example-news", "endpoint_key": "rss"}
            )

        self.assertEqual(1, len(items))
        self.assertEqual("Fish & Chips", items[0].title)

    def test_detail_summary_ignores_placeholder_metadata_description(self) -> None:
        self.assertEqual(
            "A sufficiently descriptive public lead paragraph for the news item.",
            _summary_from_detail(
                "News",
                "# Heading\n\nA sufficiently descriptive public lead paragraph for the news item.",
            ),
        )
        self.assertEqual("", _summary_from_detail("Tiny", "Too short"))

    def test_detail_async_task_uses_worker_thread_when_an_event_loop_is_active(self) -> None:
        async def value() -> str:
            return "completed"

        with patch("wechat_list_collector.application.public_sources.asyncio.get_running_loop", return_value=object()):
            self.assertEqual("completed", _run_async_safely(value))

    def test_health_summary_exposes_source_failure_without_hiding_successes(self) -> None:
        catalog = [
            *self.catalog,
            SourceDefinition(
                slug="blocked-source",
                name="Blocked Source",
                homepage_url="https://blocked.invalid/",
                enabled=True,
                endpoints=(
                    EndpointDefinition(
                        key="list", kind="browser", list_url="https://blocked.invalid/list", enabled=True,
                        minimum_interval_seconds=900, max_items=10,
                    ),
                ),
            ),
        ]
        collector = PublicSourceCollector(self.repository, OneEndpointFailsFetcher())
        collector.sync_catalog(catalog)
        result = collector.collect_all(trigger="scheduled", idempotency_key="daily:health", business_date="2026-08-05")

        health = result["health"]
        self.assertEqual("partial", health["status"])
        self.assertEqual(1, health["failed_endpoint_count"])
        self.assertIn(
            {"severity": "error", "code": "LIST_ACCESS_BLOCKED", "source_slug": "blocked-source"},
            health["alerts"],
        )

    def test_stale_running_run_is_failed_before_new_collection_starts(self) -> None:
        collector = PublicSourceCollector(self.repository, RecordingFetcher([]), stale_run_seconds=1_800)
        collector.sync_catalog(self.catalog)
        old_run, _ = self.repository.start_run(
            trigger="scheduled", idempotency_key="daily:abandoned", business_date="2026-08-05"
        )
        self.now += timedelta(minutes=31)

        collector.collect_all(trigger="scheduled", idempotency_key="daily:replacement", business_date="2026-08-05")

        diagnostics = self.repository.diagnostics(str(old_run["id"]))
        self.assertEqual("failed", diagnostics["run"]["status"])
        self.assertEqual("RUN_LEASE_EXPIRED", diagnostics["run"]["reason_code"])

    def test_catalog_reconciliation_disables_removed_sources_and_endpoints(self) -> None:
        expanded_catalog = [
            SourceDefinition(
                slug="example-news",
                name="Example News",
                homepage_url="https://example.invalid/",
                enabled=True,
                endpoints=(
                    *self.catalog[0].endpoints,
                    EndpointDefinition("top", "rss", "https://example.invalid/top.xml", True, 900, 50),
                ),
            ),
            SourceDefinition(
                slug="removed-news",
                name="Removed News",
                homepage_url="https://removed.invalid/",
                enabled=True,
                endpoints=(EndpointDefinition("rss", "rss", "https://removed.invalid/feed.xml", True, 900, 50),),
            ),
        ]
        self.repository.sync_catalog(expanded_catalog)
        result = self.repository.sync_catalog(self.catalog)

        self.assertEqual(1, len(self.repository.enabled_endpoints()))
        self.assertEqual("example-news", self.repository.enabled_endpoints()[0]["source_slug"])
        self.assertGreaterEqual(result["disabled_source_count"], 1)
        self.assertGreaterEqual(result["disabled_endpoint_count"], 2)

    def test_empty_listing_is_a_visible_data_quality_warning(self) -> None:
        collector = PublicSourceCollector(self.repository, RecordingFetcher([]))
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="scheduled", idempotency_key="daily:empty", business_date="2026-08-05")

        self.assertIn(
            {"severity": "warning", "code": "EMPTY_LIST_RESULT", "source_slug": "example-news"},
            result["health"]["alerts"],
        )

    def test_rss_parser_extracts_only_list_metadata(self) -> None:
        items = _parse_feed(
            b"""<?xml version='1.0'?><rss><channel><item><guid>x</guid><title>Title</title><link>https://example.invalid/post?utm_source=x</link><description><![CDATA[<p>Summary</p>]]></description><pubDate>Tue, 05 Aug 2026 08:00:00 +0000</pubDate></item></channel></rss>""",
            base_url="https://example.invalid/feed.xml",
            max_items=10,
        )
        self.assertEqual(items[0].title, "Title")
        self.assertEqual(items[0].digest, "Summary")
        self.assertEqual(items[0].url, "https://example.invalid/post")
        self.assertEqual(items[0].published_at, datetime(2026, 8, 5, 8, tzinfo=UTC))

    def test_selected_sources_collect_only_the_requested_endpoint(self) -> None:
        catalog = [
            *self.catalog,
            SourceDefinition(
                slug="other-news",
                name="Other News",
                homepage_url="https://other.invalid/",
                enabled=True,
                endpoints=(
                    EndpointDefinition(
                        key="rss", kind="rss", list_url="https://other.invalid/feed.xml", enabled=True,
                        minimum_interval_seconds=900, max_items=50,
                    ),
                ),
            ),
        ]
        fetcher = RecordingFetcher([ListingItem("one", "One", "Summary", "https://example.invalid/one", datetime(2026, 8, 5, 8, tzinfo=UTC))])
        collector = PublicSourceCollector(self.repository, fetcher)
        collector.sync_catalog(catalog)

        result = collector.collect_all(
            trigger="manual_cli",
            idempotency_key="manual:selected-source",
            source_slugs=("example-news",),
        )

        self.assertEqual(result["planned_endpoint_count"], 1)
        self.assertEqual(fetcher.requested_urls, ["https://example.invalid/feed.xml"])
        diagnostics = self.repository.diagnostics(str(result["id"]))
        self.assertEqual(diagnostics["run"]["selected_source_slugs_json"], '["example-news"]')

    def test_one_source_failure_is_logged_and_does_not_stop_other_sources(self) -> None:
        catalog = [
            *self.catalog,
            SourceDefinition(
                slug="blocked-source",
                name="Blocked Source",
                homepage_url="https://blocked.invalid/",
                enabled=True,
                endpoints=(
                    EndpointDefinition(
                        key="list", kind="browser", list_url="https://blocked.invalid/list", enabled=True,
                        minimum_interval_seconds=900, max_items=10,
                    ),
                ),
            ),
        ]
        collector = PublicSourceCollector(self.repository, OneEndpointFailsFetcher())
        collector.sync_catalog(catalog)

        with self.assertLogs("public_source_collector", level="WARNING") as logs:
            result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:source-failure")

        self.assertEqual(result["status"], "partial")
        self.assertIn("public_source_endpoint_failed", "\n".join(logs.output))
        diagnostics = self.repository.diagnostics(str(result["id"]))
        blocked = next(endpoint for endpoint in diagnostics["endpoints"] if endpoint["source_slug"] == "blocked-source")
        completed = next(endpoint for endpoint in diagnostics["endpoints"] if endpoint["source_slug"] == "example-news")
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(blocked["reason_code"], "LIST_ACCESS_BLOCKED")
        self.assertEqual(completed["status"], "completed")

    def test_cache_deduplicates_two_listing_items_with_the_same_canonical_url(self) -> None:
        url = "https://example.invalid/articles/one"
        fetcher = RecordingFetcher(
            [
                ListingItem("changed-guid-1", "One", "Summary", url, datetime(2026, 8, 5, 8, tzinfo=UTC)),
                ListingItem("changed-guid-2", "One", "Summary", url, datetime(2026, 8, 5, 8, tzinfo=UTC)),
            ]
        )
        collector = PublicSourceCollector(self.repository, fetcher)
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:url-dedup")

        self.assertEqual(result["new_article_count"], 1)
        with self.engine.connect() as connection:
            rows = connection.execute(public_source_articles.select()).mappings().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], url)

    def test_cache_deduplicates_the_same_url_from_two_endpoints_of_one_source(self) -> None:
        url = "https://example.invalid/articles/one"
        source = self.catalog[0]
        catalog = [
            SourceDefinition(
                slug=source.slug,
                name=source.name,
                homepage_url=source.homepage_url,
                enabled=True,
                endpoints=(
                    *source.endpoints,
                    EndpointDefinition(
                        key="top", kind="rss", list_url="https://example.invalid/top.xml", enabled=True,
                        minimum_interval_seconds=900, max_items=50,
                    ),
                ),
            )
        ]
        collector = PublicSourceCollector(
            self.repository,
            RecordingFetcher([ListingItem("one", "One", "Summary", url, datetime(2026, 8, 5, 8, tzinfo=UTC))]),
        )
        collector.sync_catalog(catalog)

        result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:source-url-dedup")

        self.assertEqual(result["new_article_count"], 1)
        with self.engine.connect() as connection:
            rows = connection.execute(public_source_articles.select()).mappings().all()
        self.assertEqual(len(rows), 1)

    def test_pipeline_refresh_api_accepts_all_or_selected_sources(self) -> None:
        calls: list[tuple[tuple[str, ...], str | None]] = []

        def refresh(source_slugs: tuple[str, ...], idempotency_key: str | None) -> dict[str, object]:
            calls.append((source_slugs, idempotency_key))
            return {"refresh": {"run_id": "run-1", "status": "completed"}, "selected_source_slugs": list(source_slugs)}

        client = TestClient(
            create_read_api(
                PublicReadModel(self.engine, now=lambda: self.now),
                bearer_token="token",
                schema_version="2.1",
                refresh_public_sources=refresh,
            )
        )
        response = client.post(
            "/api/v1/pipeline/refresh",
            json={"source_slugs": ["Example-News", "other-news"], "idempotency_key": "daily:2026-08-05"},
            headers={"Authorization": "Bearer token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [(("example-news", "other-news"), "daily:2026-08-05")])
        self.assertEqual(response.json()["refresh"]["status"], "completed")

    def test_enriches_only_missing_digest_and_records_audit_without_detail_body(self) -> None:
        missing = ListingItem("missing", "Needs a summary", "", "https://example.invalid/articles/missing", datetime(2026, 8, 5, 8, tzinfo=UTC))
        complete = ListingItem("complete", "Complete", "Already present", "https://example.invalid/articles/complete", datetime(2026, 8, 5, 9, tzinfo=UTC))
        fetcher = RecordingFetcher([missing, complete])
        enricher = RecordingDetailEnricher(
            [
                DetailEnrichmentAttempt(
                    source_item_key="missing",
                    source_url=missing.url,
                    method="test.detail",
                    status="succeeded",
                    digest="Recovered metadata description only.",
                    duration_ms=321,
                )
            ]
        )
        collector = PublicSourceCollector(self.repository, fetcher, detail_enricher=enricher)
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:enriched")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(["missing"], [item.source_item_key for item in enricher.candidates])
        with self.engine.connect() as connection:
            row = connection.execute(
                public_source_articles.select().where(public_source_articles.c.source_item_key == "missing")
            ).mappings().one()
        self.assertEqual(row["digest"], "Recovered metadata description only.")
        self.assertEqual(row["digest_source"], "detail")
        diagnostics = self.repository.diagnostics(str(result["id"]))
        attempt = diagnostics["endpoints"][0]["detail_enrichment_attempts"][0]
        self.assertEqual(attempt["status"], "succeeded")
        self.assertEqual(attempt["method"], "test.detail")
        self.assertEqual(attempt["article_id"], row["id"])

    def test_detail_enricher_failure_does_not_discard_complete_list_items(self) -> None:
        missing = ListingItem("missing", "Needs a summary", "", "https://example.invalid/articles/missing", datetime(2026, 8, 5, 8, tzinfo=UTC))
        complete = ListingItem("complete", "Complete", "Already present", "https://example.invalid/articles/complete", datetime(2026, 8, 5, 9, tzinfo=UTC))
        collector = PublicSourceCollector(self.repository, RecordingFetcher([missing, complete]), detail_enricher=FailingDetailEnricher())
        collector.sync_catalog(self.catalog)
        result = collector.collect_all(trigger="manual_cli", idempotency_key="manual:enrichment-failure")

        self.assertEqual(result["status"], "partial")
        diagnostics = self.repository.diagnostics(str(result["id"]))
        attempt = diagnostics["endpoints"][0]["detail_enrichment_attempts"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["reason_code"], "DETAIL_ENRICHER_FAILED")
        with self.engine.connect() as connection:
            article_keys = connection.execute(public_source_articles.select()).scalars().all()
        self.assertEqual(len(article_keys), 1)

    def test_parses_browser_rendered_javascript_date_without_article_fetch(self) -> None:
        self.assertEqual(
            parse_published_at("Wed Jul 29 2026 00:00:00 GMT+0000 (Coordinated Universal Time)"),
            datetime(2026, 7, 29, tzinfo=UTC),
        )

    def test_rejects_unknown_browser_mode(self) -> None:
        with self.assertRaises(ValueError):
            HybridListFetcher(timeout_seconds=20, user_agent="test", browser_mode="stealth")

    def test_requires_a_source_specific_browser_adapter(self) -> None:
        with self.assertRaises(PublicSourceError) as raised:
            browser_adapter_for("unknown-source")
        self.assertEqual(raised.exception.reason_code, "SOURCE_ADAPTER_MISSING")

    def test_infoq_uses_its_own_interactive_browser_context(self) -> None:
        self.assertTrue(browser_adapter_for("infoq").requires_interactive)
        self.assertFalse(browser_adapter_for("qbitai").requires_interactive)
        self.assertEqual(browser_adapter_for("huxiu").ready_state, "attached")

    def test_android_adapter_supports_featured_and_legacy_list_card_shapes(self) -> None:
        script = browser_adapter_for("android-developers").extraction_script
        self.assertIn("li.adb--post-list-item", script)
        self.assertIn(".adb--post-list-item-description p", script)
        self.assertIn(".adb--post-list-item-body p", script)

    def test_machineheart_browser_network_adapter_listens_only_for_its_list_response(self) -> None:
        adapter = browser_network_adapter_for("jiqizhixin")
        self.assertEqual(adapter.response_url_fragment, "/api/article_library/articles.json")

    def test_catalog_persists_browser_network_endpoint_kind(self) -> None:
        catalog = [
            SourceDefinition(
                slug="network-news",
                name="Network News",
                homepage_url="https://example.invalid/",
                enabled=True,
                endpoints=(
                    EndpointDefinition(
                        key="listing", kind="browser_network", list_url="https://example.invalid/list",
                        enabled=True, minimum_interval_seconds=900, max_items=10,
                    ),
                ),
            )
        ]
        self.repository.sync_catalog(catalog)
        self.assertEqual(self.repository.enabled_endpoints()[0]["kind"], "browser_network")

    def test_parses_qwen_page_response_without_persisting_full_article_html(self) -> None:
        items = json_payload_to_items(
            {
                "success": True,
                "data": {
                    "articles": [
                        {
                            "id": "qwen-id",
                            "path": "qwen3-8",
                            "title": "Qwen update",
                            "content": "<article>full article body that must not become the digest</article>",
                            "extra": {
                                "introduction": "A source-provided list summary.",
                                "date": "2026-08-03T10:00:00+08:00",
                                "cover_small": "https://img.example/qwen.png",
                            },
                        }
                    ]
                },
            },
            source_slug="qwen",
            max_items=10,
        )
        self.assertEqual(items[0].url, "https://qwen.ai/blog?id=qwen3-8")
        self.assertEqual(items[0].digest, "A source-provided list summary.")
        self.assertNotIn("full article body", items[0].digest)
        self.assertEqual(items[0].published_at, datetime(2026, 8, 3, 2, tzinfo=UTC))

    def test_normalizes_source_adapter_cards_without_fetching_article_urls(self) -> None:
        adapter = browser_adapter_for("qbitai")
        items = browser_cards_to_items(
            [{"title": "A title", "digest": "A usable summary", "url": "https://example.invalid/post?utm_source=test", "published": "2026-08-04T01:18:00Z"}],
            max_items=10,
            adapter=adapter,
        )
        self.assertEqual(items[0].url, "https://example.invalid/post")
        self.assertEqual(items[0].published_at, datetime(2026, 8, 4, 1, 18, tzinfo=UTC))

    def test_parses_qbitai_named_day(self) -> None:
        self.assertEqual(
            parse_published_at("昨天 01:18", now=datetime(2026, 8, 6, 8, tzinfo=UTC)),
            datetime(2026, 8, 5, 1, 18, tzinfo=UTC),
        )

    def test_parses_month_day_for_zhidx_cards(self) -> None:
        self.assertEqual(
            parse_published_at("08-05", now=datetime(2026, 8, 6, 8, tzinfo=UTC)),
            datetime(2026, 8, 5, tzinfo=UTC),
        )

    def test_parses_machineheart_json_list_without_opening_article_urls(self) -> None:
        items = json_payload_to_items(
            {
                "success": True,
                "articles": [
                    {
                        "id": "article-id",
                        "slug": "2026-08-05-10",
                        "title": "A model update",
                        "content": "A usable public listing summary.",
                        "publishedAt": "2026/08/05 19:01",
                        "coverImageUrl": "https://image.jiqizhixin.com/cover.jpg",
                    }
                ],
            },
            source_slug="jiqizhixin",
            max_items=10,
        )
        self.assertEqual(items[0].source_item_key, "article-id")
        self.assertEqual(items[0].url, "https://www.jiqizhixin.com/articles/2026-08-05-10")
        self.assertEqual(items[0].digest, "A usable public listing summary.")
        self.assertEqual(items[0].published_at, datetime(2026, 8, 5, 11, 1, tzinfo=UTC))
