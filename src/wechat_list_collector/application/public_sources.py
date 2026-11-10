from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import tomllib
from collections import Counter
from json import JSONDecodeError
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol, TypeVar
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
import feedparser

from ..infrastructure.db.public_sources_repository import PublicSourceRepository


logger = logging.getLogger(__name__)
T = TypeVar("T")


class PublicSourceError(RuntimeError):
    def __init__(self, message: str, *, reason_code: str = "LIST_FETCH_FAILED"):
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class EndpointDefinition:
    key: str
    kind: str
    list_url: str
    enabled: bool
    minimum_interval_seconds: int
    max_items: int


@dataclass(frozen=True)
class SourceDefinition:
    slug: str
    name: str
    homepage_url: str
    enabled: bool
    endpoints: tuple[EndpointDefinition, ...]


@dataclass(frozen=True)
class ListingItem:
    source_item_key: str
    title: str
    digest: str
    url: str
    published_at: datetime | None
    cover_url: str = ""
    digest_source: str = "listing"


@dataclass(frozen=True)
class RejectedListingItem:
    source_item_key: str
    title: str
    url: str
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class BrowserListAdapter:
    """A vetted, source-owned selector contract for one public list page."""

    ready_selector: str
    extraction_script: str
    allowed_path_prefixes: tuple[str, ...] = ()
    requires_interactive: bool = False
    blocked_markers: tuple[str, ...] = ()
    ready_state: str = "visible"


@dataclass(frozen=True)
class BrowserNetworkAdapter:
    """A source-owned contract for a JSON response initiated by a public list page."""

    response_url_fragment: str


@dataclass(frozen=True)
class DetailEnrichmentPolicy:
    """The explicit public-detail boundary for one source."""

    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True)
class DetailEnrichmentAttempt:
    source_item_key: str
    source_url: str
    method: str
    status: str
    digest: str = ""
    duration_ms: int = 0
    reason_code: str = ""
    message: str = ""


class ListFetcher(Protocol):
    def fetch(self, endpoint: dict[str, object]) -> list[ListingItem]: ...


class DetailEnricher(Protocol):
    def enrich(self, endpoint: dict[str, object], items: list[ListingItem]) -> list[DetailEnrichmentAttempt]: ...


def load_source_catalog(path: Path) -> list[SourceDefinition]:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    sources: list[SourceDefinition] = []
    for source in raw.get("sources", []):
        endpoints = tuple(
            EndpointDefinition(
                key=_required_text(endpoint, "key"),
                kind=_required_text(endpoint, "kind"),
                list_url=_required_text(endpoint, "list_url"),
                enabled=bool(endpoint.get("enabled", True)),
                minimum_interval_seconds=int(endpoint.get("minimum_interval_seconds", 900)),
                max_items=int(endpoint.get("max_items", 50)),
            )
            for endpoint in source.get("endpoints", [])
        )
        if not endpoints:
            raise ValueError("every public source needs at least one endpoint")
        sources.append(
            SourceDefinition(
                slug=_required_text(source, "slug"),
                name=_required_text(source, "name"),
                homepage_url=_required_text(source, "homepage_url"),
                enabled=bool(source.get("enabled", True)),
                endpoints=endpoints,
            )
        )
    if not sources:
        raise ValueError("public source catalog has no sources")
    return sources


class RequestsListFetcher:
    """Fetches only configured listing endpoints; article links are never opened."""

    def __init__(self, *, timeout_seconds: int, user_agent: str, proxy_url: str = ""):
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self._proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    def fetch(self, endpoint: dict[str, object]) -> list[ListingItem]:
        kind = str(endpoint["kind"])
        if kind == "rss":
            response = requests.get(
                str(endpoint["list_url"]),
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.user_agent, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
                proxies=self._proxies,
            )
            response.raise_for_status()
            try:
                return _parse_feed(response.content, base_url=str(endpoint["list_url"]), max_items=int(endpoint["max_items"]))
            except ElementTree.ParseError as error:
                items = _parse_feed_tolerant(response.content, base_url=str(endpoint["list_url"]), max_items=int(endpoint["max_items"]))
                logger.warning(
                    "public_source_rss_tolerant_parse_used",
                    extra={
                        "source_slug": endpoint.get("source_slug", ""),
                        "endpoint_key": endpoint.get("endpoint_key", ""),
                        "operation": "parse_public_rss",
                        "reason_code": "RSS_STRICT_PARSE_FAILED",
                        "item_count": len(items),
                        "parse_error": str(error),
                    },
                )
                return items
        if kind != "json":
            raise PublicSourceError(f"requests fetcher does not support parser kind: {kind}")
        response = requests.get(
            str(endpoint["list_url"]),
            timeout=self.timeout_seconds,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            proxies=self._proxies,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except JSONDecodeError as error:
            raise PublicSourceError("public JSON list endpoint returned invalid JSON", reason_code="LIST_RESPONSE_INVALID") from error
        return json_payload_to_items(payload, source_slug=str(endpoint.get("source_slug", "")), max_items=int(endpoint["max_items"]))


class HybridListFetcher:
    """One list-only client with RSS plus headless or interactive public-list browsing."""

    def __init__(
        self,
        *,
        timeout_seconds: int,
        user_agent: str,
        browser: str = "chrome",
        browser_mode: str = "headless",
        browser_profile: Path | None = None,
        proxy_url: str = "",
    ):
        if browser_mode not in {"headless", "interactive"}:
            raise ValueError("browser_mode must be headless or interactive")
        self._requests = RequestsListFetcher(timeout_seconds=timeout_seconds, user_agent=user_agent, proxy_url=proxy_url)
        self._timeout_milliseconds = timeout_seconds * 1_000
        self._user_agent = user_agent
        self._browser_channel = browser
        self._browser_mode = browser_mode
        self._browser_profile = browser_profile
        self._proxy_url = proxy_url
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._persistent_context: Any | None = None

    def fetch(self, endpoint: dict[str, object]) -> list[ListingItem]:
        if str(endpoint["kind"]) in {"rss", "json"}:
            return self._requests.fetch(endpoint)
        if str(endpoint["kind"]) == "browser_network":
            return self._fetch_browser_network_list(endpoint)
        if str(endpoint["kind"]) != "browser":
            raise PublicSourceError(f"unsupported parser kind: {endpoint['kind']}")
        return self._fetch_browser_list(endpoint)

    def close(self) -> None:
        if self._persistent_context is not None:
            self._persistent_context.close()
            self._persistent_context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _fetch_browser_list(self, endpoint: dict[str, object]) -> list[ListingItem]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise PublicSourceError("browser endpoint requires playwright") from error
        source_slug = str(endpoint.get("source_slug", ""))
        adapter = browser_adapter_for(source_slug)
        context, temporary_context = self._browser_context(sync_playwright, interactive=adapter.requires_interactive)
        try:
            page = context.new_page()
            page.goto(str(endpoint["list_url"]), wait_until="domcontentloaded", timeout=self._timeout_milliseconds)
            self._wait_for_render(page)
            page_text = page.locator("body").inner_text(timeout=self._timeout_milliseconds).casefold()
            if any(marker.casefold() in page_text for marker in adapter.blocked_markers):
                raise PublicSourceError(
                    f"public list page is blocked for {source_slug}", reason_code="LIST_ACCESS_BLOCKED"
                )
            page.wait_for_selector(adapter.ready_selector, state=adapter.ready_state, timeout=self._timeout_milliseconds)
            cards = page.evaluate(adapter.extraction_script)
        finally:
            if temporary_context:
                context.close()
        if not isinstance(cards, list):
            raise PublicSourceError("public list adapter returned invalid card data", reason_code="LIST_RESPONSE_INVALID")
        return browser_cards_to_items(cards, max_items=int(endpoint["max_items"]), adapter=adapter)

    def _fetch_browser_network_list(self, endpoint: dict[str, object]) -> list[ListingItem]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise PublicSourceError("browser network endpoint requires playwright") from error
        source_slug = str(endpoint.get("source_slug", ""))
        adapter = browser_network_adapter_for(source_slug)
        captured_payloads: list[object] = []
        context, temporary_context = self._browser_context(sync_playwright)
        try:
            page = context.new_page()

            def capture_response(response: Any) -> None:
                if adapter.response_url_fragment not in response.url:
                    return
                try:
                    captured_payloads.append(response.json())
                except Exception:
                    # A matching non-JSON/error response is not usable list data.
                    return

            page.on("response", capture_response)
            page.goto(str(endpoint["list_url"]), wait_until="domcontentloaded", timeout=self._timeout_milliseconds)
            page.wait_for_timeout(min(self._timeout_milliseconds, 5_000))
        finally:
            if temporary_context:
                context.close()
        if not captured_payloads:
            raise PublicSourceError(
                f"no matching public list response was observed for {source_slug or 'unknown-source'}",
                reason_code="BROWSER_NETWORK_RESPONSE_MISSING",
            )
        return json_payload_to_items(
            captured_payloads[-1], source_slug=source_slug, max_items=int(endpoint["max_items"])
        )

    def _browser_context(self, sync_playwright: Any, *, interactive: bool = False) -> tuple[Any, bool]:
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        channel_options: dict[str, object] = {}
        if self._browser_channel == "chrome":
            channel_options["channel"] = "chrome"
        if self._proxy_url:
            channel_options["proxy"] = {"server": self._proxy_url}
        if self._browser_mode == "interactive" or interactive:
            if self._persistent_context is None:
                if self._browser_profile is None:
                    raise PublicSourceError("interactive browser mode requires a dedicated browser profile path")
                self._browser_profile.mkdir(parents=True, exist_ok=True)
                self._persistent_context = self._playwright.chromium.launch_persistent_context(
                    str(self._browser_profile),
                    **channel_options,
                    headless=False,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 1200},
                )
            return self._persistent_context, False
        if self._browser is None:
            self._browser = self._playwright.chromium.launch(**channel_options, headless=True)
        return self._browser.new_context(
            user_agent=self._user_agent,
            locale="zh-CN",
            viewport={"width": 1440, "height": 1200},
        ), True

    def _wait_for_render(self, page: Any) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=min(self._timeout_milliseconds, 5_000))
        except Exception:
            # Some SPAs keep telemetry connections open; rendered text is the fallback readiness signal.
            pass
        page.wait_for_function(
            "() => document.readyState === 'complete' && (document.body?.innerText || '').trim().length >= 200",
            timeout=self._timeout_milliseconds,
        )


def browser_adapter_for(source_slug: str) -> BrowserListAdapter:
    adapter = _BROWSER_LIST_ADAPTERS.get(source_slug)
    if adapter is None:
        raise PublicSourceError(
            f"no source-specific browser adapter is registered for {source_slug or 'unknown-source'}",
            reason_code="SOURCE_ADAPTER_MISSING",
        )
    return adapter


def browser_network_adapter_for(source_slug: str) -> BrowserNetworkAdapter:
    adapter = _BROWSER_NETWORK_ADAPTERS.get(source_slug)
    if adapter is None:
        raise PublicSourceError(
            f"no source-specific browser network adapter is registered for {source_slug or 'unknown-source'}",
            reason_code="SOURCE_ADAPTER_MISSING",
        )
    return adapter


def browser_cards_to_items(
    cards: list[dict[str, object]], *, max_items: int, adapter: BrowserListAdapter
) -> list[ListingItem]:
    items: list[ListingItem] = []
    for card in cards[:max_items]:
        published_at = parse_published_at(str(card.get("published", "")))
        url = canonicalize_url(str(card.get("url", ""))) if card.get("url") else ""
        if adapter.allowed_path_prefixes and not urlsplit(url).path.startswith(adapter.allowed_path_prefixes):
            continue
        items.append(
            ListingItem(
                source_item_key=url,
                title=_clean_text(str(card.get("title", ""))),
                digest=_clean_text(str(card.get("digest", "")))[:800],
                url=url,
                published_at=published_at,
            )
        )
    return items


def json_payload_to_items(payload: object, *, source_slug: str, max_items: int) -> list[ListingItem]:
    parser = _JSON_LIST_ADAPTERS.get(source_slug)
    if parser is None:
        raise PublicSourceError(
            f"no source-specific JSON adapter is registered for {source_slug or 'unknown-source'}",
            reason_code="SOURCE_ADAPTER_MISSING",
        )
    if not isinstance(payload, dict):
        raise PublicSourceError("public JSON list endpoint returned a non-object payload", reason_code="LIST_RESPONSE_INVALID")
    return parser(payload, max_items)


class Crawl4AIDetailEnricher:
    """Opt-in, bounded public-page summary recovery for incomplete list entries.

    Crawl4AI is used only as a public-page text cleaner. It receives no login
    state, proxy, stealth, or LLM configuration, and its raw page output is
    discarded before the result leaves this process.
    """

    method = "crawl4ai.metadata_or_lead"

    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_items_per_endpoint: int,
        browser: str,
        base_directory: Path,
    ):
        if max_items_per_endpoint < 1:
            raise ValueError("detail enrichment max_items_per_endpoint must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_items_per_endpoint = max_items_per_endpoint
        self.browser = browser
        self.base_directory = base_directory

    def enrich(self, endpoint: dict[str, object], items: list[ListingItem]) -> list[DetailEnrichmentAttempt]:
        source_slug = str(endpoint.get("source_slug", ""))
        policy = detail_enrichment_policy_for(source_slug)
        if policy is None:
            return []
        eligible = [item for item in items if _can_enrich_from_detail(item, policy)][: self.max_items_per_endpoint]
        if not eligible:
            return []
        try:
            return _run_async_safely(lambda: self._enrich_async(eligible))
        except ImportError as error:
            return [
                DetailEnrichmentAttempt(
                    source_item_key=item.source_item_key,
                    source_url=item.url,
                    method=self.method,
                    status="failed",
                    reason_code="DETAIL_ENRICHER_UNAVAILABLE",
                    message=_safe_message(error),
                )
                for item in eligible
            ]

    async def _enrich_async(self, items: list[ListingItem]) -> list[DetailEnrichmentAttempt]:
        # Crawl4AI reads this setting during import to choose its own local
        # bookkeeping directory. Keep that state inside the collector data dir.
        os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(self.base_directory)
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        self.base_directory.mkdir(parents=True, exist_ok=True)
        browser_config = BrowserConfig(
            browser_type="chromium",
            chrome_channel=self.browser,
            headless=True,
            enable_stealth=False,
            avoid_ads=False,
            user_agent="PublicNewsCollector/1.0 (+local-pipeline)",
        )
        run_config = CrawlerRunConfig(
            only_text=True,
            cache_mode=CacheMode.BYPASS,
            page_timeout=self.timeout_seconds * 1_000,
            check_robots_txt=True,
            exclude_external_links=True,
        )
        attempts: list[DetailEnrichmentAttempt] = []
        async with AsyncWebCrawler(config=browser_config, base_directory=str(self.base_directory)) as crawler:
            for item in items:
                started = time.monotonic()
                try:
                    result = await crawler.arun(item.url, config=run_config)
                    duration_ms = int((time.monotonic() - started) * 1_000)
                    if not result.success:
                        attempts.append(
                            DetailEnrichmentAttempt(
                                source_item_key=item.source_item_key,
                                source_url=item.url,
                                method=self.method,
                                status="failed",
                                duration_ms=duration_ms,
                                reason_code="DETAIL_FETCH_FAILED",
                                message=_safe_message(RuntimeError(str(getattr(result, "error_message", "crawl failed")))),
                            )
                        )
                        continue
                    metadata = getattr(result, "metadata", {}) or {}
                    markdown = getattr(getattr(result, "markdown", None), "raw_markdown", "") or ""
                    digest = _summary_from_detail(
                        _clean_text(str(metadata.get("description", ""))),
                        str(markdown),
                    )
                    attempts.append(
                        DetailEnrichmentAttempt(
                            source_item_key=item.source_item_key,
                            source_url=item.url,
                            method=self.method,
                            status="succeeded" if digest else "failed",
                            digest=digest,
                            duration_ms=duration_ms,
                            reason_code="" if digest else "DETAIL_SUMMARY_MISSING",
                            message="" if digest else "public detail page had no usable metadata description or lead paragraph",
                        )
                    )
                except Exception as error:
                    attempts.append(
                        DetailEnrichmentAttempt(
                            source_item_key=item.source_item_key,
                            source_url=item.url,
                            method=self.method,
                            status="failed",
                            duration_ms=int((time.monotonic() - started) * 1_000),
                            reason_code="DETAIL_FETCH_FAILED",
                            message=_safe_message(error),
                        )
                    )
        return attempts


class PublicSourceCollector:
    def __init__(
        self,
        repository: PublicSourceRepository,
        fetcher: ListFetcher,
        detail_enricher: DetailEnricher | None = None,
        *,
        stale_run_seconds: int = 1_800,
        health_stale_after_hours: int = 48,
        health_baseline_runs: int = 3,
    ):
        self.repository = repository
        self.fetcher = fetcher
        self.detail_enricher = detail_enricher
        self.stale_run_seconds = stale_run_seconds
        self.health_stale_after_hours = health_stale_after_hours
        self.health_baseline_runs = health_baseline_runs
        self.logger = logging.getLogger("public_source_collector")

    def sync_catalog(self, catalog: list[SourceDefinition]) -> dict[str, int]:
        return self.repository.sync_catalog(catalog)

    def collect_all(
        self,
        *,
        trigger: str,
        idempotency_key: str,
        business_date: str | None = None,
        source_slugs: tuple[str, ...] = (),
    ) -> dict[str, object]:
        selected_source_slugs = _normalized_source_slugs(source_slugs)
        recovered_runs = self.repository.recover_stale_runs(stale_after_seconds=self.stale_run_seconds)
        for recovered_run_id in recovered_runs:
            self.logger.warning(
                "public_source_stale_run_recovered",
                extra={"run_id": recovered_run_id, "operation": "recover_public_source_run", "reason_code": "RUN_LEASE_EXPIRED"},
            )
        run, reused = self.repository.start_run(
            trigger=trigger,
            idempotency_key=idempotency_key,
            business_date=business_date,
            source_slugs=selected_source_slugs,
        )
        if reused:
            return {
                "id": run["id"],
                "run_id": run["id"],
                "status": run["status"],
                "reused": True,
                "health": self.repository.health_summary(
                    str(run["id"]),
                    stale_after_hours=self.health_stale_after_hours,
                    baseline_runs=self.health_baseline_runs,
                ),
            }
        self.logger.info("public_source_run_started", extra={"run_id": run["id"], "operation": "collect_public_lists"})
        try:
            for endpoint in self.repository.enabled_endpoints(selected_source_slugs):
                endpoint_run = self.repository.start_endpoint_run(str(run["id"]), endpoint)
                if endpoint_run is None:
                    self.logger.info(
                        "public_source_endpoint_skipped",
                        extra={"run_id": run["id"], "source_slug": endpoint["source_slug"], "endpoint_key": endpoint["endpoint_key"], "reason_code": "SOURCE_INTERVAL_ACTIVE"},
                    )
                    continue
                endpoint_started = time.monotonic()
                try:
                    items = self.fetcher.fetch(endpoint)
                    attempts: list[DetailEnrichmentAttempt] = []
                    if self.detail_enricher is not None:
                        candidates = [item for item in items if _has_required_list_fields(item, include_digest=False) and not item.digest]
                        try:
                            attempts = self.detail_enricher.enrich(endpoint, candidates)
                        except Exception as error:
                            attempts = [
                                DetailEnrichmentAttempt(
                                    source_item_key=item.source_item_key,
                                    source_url=item.url,
                                    method="detail_enricher",
                                    status="failed",
                                    reason_code="DETAIL_ENRICHER_FAILED",
                                    message=_safe_message(error),
                                )
                                for item in candidates
                            ]
                            self.logger.warning(
                                "public_source_detail_enrichment_failed",
                                extra={
                                    "run_id": run["id"],
                                    "source_slug": endpoint["source_slug"],
                                    "endpoint_key": endpoint["endpoint_key"],
                                    "operation": "enrich_public_detail",
                                    "reason_code": "DETAIL_ENRICHER_FAILED",
                                },
                            )
                        enriched = {attempt.source_item_key: attempt for attempt in attempts if attempt.status == "succeeded" and attempt.digest}
                        items = [
                            replace(item, digest=enriched[item.source_item_key].digest, digest_source="detail")
                            if item.source_item_key in enriched else item
                            for item in items
                        ]
                    accepted, rejected = _validated_items(items)
                    rejection_summary, rejection_samples = _rejection_audit(rejected)
                    result = self.repository.store_items(str(endpoint_run["id"]), endpoint, accepted)
                    if attempts:
                        self.repository.store_enrichment_attempts(
                            str(endpoint_run["id"]),
                            result["article_ids_by_source_item_key"],
                            attempts,
                        )
                    status = "completed" if accepted and not rejected else "partial"
                    self.repository.finish_endpoint_run(
                        str(endpoint_run["id"]),
                        status=status,
                        fetched_count=len(items),
                        accepted_count=len(accepted),
                        rejected_count=len(rejected),
                        new_count=result["new_count"],
                        updated_count=result["updated_count"],
                        reason_code="" if accepted and not rejected else "NO_COMPLETE_LIST_ITEMS" if not accepted else "REQUIRED_LIST_FIELDS_MISSING",
                        rejection_summary=rejection_summary,
                        rejection_samples=rejection_samples,
                    )
                    self.logger.info(
                        "public_source_endpoint_finished",
                        extra={
                            "run_id": run["id"], "source_slug": endpoint["source_slug"], "endpoint_key": endpoint["endpoint_key"],
                            "operation": "fetch_public_list", "status": status, "count": len(items),
                            "accepted_count": len(accepted), "rejected_count": len(rejected),
                            "new_count": result["new_count"], "updated_count": result["updated_count"],
                            "duration_ms": int((time.monotonic() - endpoint_started) * 1_000),
                            "detail_attempt_count": len(attempts),
                            "detail_success_count": sum(attempt.status == "succeeded" for attempt in attempts),
                            "rejection_summary": rejection_summary,
                        },
                    )
                except Exception as error:
                    reason_code = error.reason_code if isinstance(error, PublicSourceError) else "LIST_FETCH_FAILED"
                    self.repository.finish_endpoint_run(
                        str(endpoint_run["id"]), status="failed", reason_code=reason_code, message=_safe_message(error)
                    )
                    self.logger.warning(
                        "public_source_endpoint_failed",
                        extra={
                            "run_id": run["id"], "source_slug": endpoint["source_slug"], "endpoint_key": endpoint["endpoint_key"],
                            "operation": "fetch_public_list", "status": "failed", "reason_code": reason_code,
                            "duration_ms": int((time.monotonic() - endpoint_started) * 1_000), "error_message": _safe_message(error),
                        },
                    )
        finally:
            close = getattr(self.fetcher, "close", None)
            if close is not None:
                close()
        result = self.repository.finalize_run(str(run["id"]))
        health = self.repository.health_summary(
            str(run["id"]),
            stale_after_hours=self.health_stale_after_hours,
            baseline_runs=self.health_baseline_runs,
        )
        result["health"] = health
        self.logger.info(
            "public_source_run_finished",
            extra={
                "run_id": run["id"], "operation": "collect_public_lists", "status": result["status"],
                "count": result["new_article_count"], "failed_endpoint_count": health["failed_endpoint_count"],
                "alert_count": len(health["alerts"]),
            },
        )
        return result


def _parse_feed(content: bytes, *, base_url: str, max_items: int) -> list[ListingItem]:
    root = ElementTree.fromstring(content)
    entries = root.findall(".//item")
    atom = False
    if not entries:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
        atom = True
    items: list[ListingItem] = []
    for entry in entries[:max_items]:
        if atom:
            title = _xml_text(entry, "{http://www.w3.org/2005/Atom}title")
            digest = _xml_text(entry, "{http://www.w3.org/2005/Atom}summary") or _xml_text(entry, "{http://www.w3.org/2005/Atom}content")
            link_element = next((link for link in entry.findall("{http://www.w3.org/2005/Atom}link") if link.get("href")), None)
            link = link_element.get("href", "") if link_element is not None else ""
            published = _xml_text(entry, "{http://www.w3.org/2005/Atom}published") or _xml_text(entry, "{http://www.w3.org/2005/Atom}updated")
            item_id = _xml_text(entry, "{http://www.w3.org/2005/Atom}id")
        else:
            title = _xml_text(entry, "title")
            digest = _xml_text(entry, "description") or _xml_text(entry, "{http://purl.org/rss/1.0/modules/content/}encoded")
            link = _xml_text(entry, "link")
            published = _xml_text(entry, "pubDate") or _xml_text(entry, "{http://purl.org/dc/elements/1.1/}date")
            item_id = _xml_text(entry, "guid")
        canonical_url = canonicalize_url(urljoin(base_url, link.strip())) if link.strip() else ""
        items.append(
            ListingItem(
                source_item_key=item_id.strip() or canonical_url,
                title=_clean_text(title),
                digest=_clean_text(digest),
                url=canonical_url,
                published_at=parse_published_at(published),
            )
        )
    return items


def _parse_feed_tolerant(content: bytes, *, base_url: str, max_items: int) -> list[ListingItem]:
    """Recover list metadata from malformed RSS without weakening item validation."""
    parsed = feedparser.parse(content)
    entries = list(parsed.entries or [])
    if not entries:
        detail = str(getattr(parsed, "bozo_exception", "") or "no feed entries")
        raise PublicSourceError(f"RSS tolerant parser found no entries: {detail}", reason_code="RSS_TOLERANT_PARSE_EMPTY")
    items: list[ListingItem] = []
    for entry in entries[:max_items]:
        content_rows = entry.get("content") or []
        content_value = content_rows[0].get("value", "") if content_rows and isinstance(content_rows[0], dict) else ""
        link = str(entry.get("link") or "")
        canonical_url = canonicalize_url(urljoin(base_url, link.strip())) if link.strip() else ""
        items.append(
            ListingItem(
                source_item_key=str(entry.get("id") or entry.get("guid") or canonical_url).strip(),
                title=_clean_text(str(entry.get("title") or "")),
                digest=_clean_text(str(entry.get("summary") or entry.get("description") or content_value or "")),
                url=canonical_url,
                published_at=parse_published_at(str(entry.get("published") or entry.get("updated") or entry.get("dc_date") or "")),
            )
        )
    return items


def _validated_items(items: list[ListingItem]) -> tuple[list[ListingItem], list[RejectedListingItem]]:
    accepted: list[ListingItem] = []
    keys: set[str] = set()
    urls: set[str] = set()
    rejected: list[RejectedListingItem] = []
    for item in items:
        missing_fields = _missing_required_list_fields(item, include_digest=True)
        if missing_fields:
            rejected.append(
                RejectedListingItem(
                    source_item_key=item.source_item_key,
                    title=item.title,
                    url=item.url,
                    missing_fields=missing_fields,
                )
            )
            continue
        if item.source_item_key in keys or item.url in urls:
            continue
        keys.add(item.source_item_key)
        urls.add(item.url)
        accepted.append(item)
    return accepted, rejected


def _has_required_list_fields(item: ListingItem, *, include_digest: bool) -> bool:
    return not _missing_required_list_fields(item, include_digest=include_digest)


def _missing_required_list_fields(item: ListingItem, *, include_digest: bool) -> tuple[str, ...]:
    missing: list[str] = []
    if not item.source_item_key:
        missing.append("source_item_key")
    if not item.title:
        missing.append("title")
    if not item.url:
        missing.append("url")
    if item.published_at is None:
        missing.append("published_at")
    if include_digest and not item.digest:
        missing.append("digest")
    return tuple(missing)


def _rejection_audit(rejected: list[RejectedListingItem], *, sample_limit: int = 3) -> tuple[dict[str, int], list[dict[str, object]]]:
    summary = dict(sorted(Counter(field for item in rejected for field in item.missing_fields).items()))
    samples = [
        {
            "source_item_key": item.source_item_key[:160],
            "title": item.title[:240],
            "url": item.url[:500],
            "missing_fields": list(item.missing_fields),
        }
        for item in rejected[:sample_limit]
    ]
    return summary, samples


def _normalized_source_slugs(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({str(value).strip().lower() for value in values if str(value).strip()}))
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) for value in normalized):
        raise ValueError("source slugs must use lowercase letters, digits, and hyphens")
    return normalized


def detail_enrichment_policy_for(source_slug: str) -> DetailEnrichmentPolicy | None:
    return _DETAIL_ENRICHMENT_POLICIES.get(source_slug)


def _can_enrich_from_detail(item: ListingItem, policy: DetailEnrichmentPolicy) -> bool:
    if not _has_required_list_fields(item, include_digest=False) or item.digest:
        return False
    hostname = (urlsplit(item.url).hostname or "").lower()
    return any(hostname == allowed or hostname.endswith(f".{allowed}") for allowed in policy.allowed_hosts)


def _summary_from_detail(metadata_description: str, markdown: str) -> str:
    # Some public pages expose a placeholder such as a site name in the meta
    # description. Treat it as absent so it cannot turn into a meaningless
    # persisted digest merely because it is non-empty.
    if len(metadata_description) >= 20:
        return metadata_description[:800]
    paragraphs: list[str] = []
    for line in markdown.splitlines():
        text = _clean_text(re.sub(r"^[#>*\-\d.\s]+", "", line))
        if len(text) >= 50 and not text.startswith(("http://", "https://")):
            paragraphs.append(text)
        if len(" ".join(paragraphs)) >= 240:
            break
    summary = " ".join(paragraphs)[:800]
    return summary if len(summary) >= 20 else ""


def _run_async_safely(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run an async detail task without conflicting with browser event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: list[T] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(factory()))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=runner, name="public-detail-enrichment", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def parse_published_at(value: str, *, now: datetime | None = None) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    normalized = re.sub(r"\s+\([^)]*\)$", "", value)
    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    if parsed is not None:
        return parsed.replace(tzinfo=UTC)
    if parsed is None:
        for pattern in (
            "%Y/%m/%d %H:%M", "%Y年%m月%d日", "%Y.%m.%d", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y",
            "%a %b %d %Y %H:%M:%S GMT%z",
        ):
            try:
                parsed = datetime.strptime(normalized, pattern)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        if parsed is None:
            reference = (now or datetime.now(UTC)).astimezone(UTC)
            relative_named_day = re.fullmatch(r"(昨天|前天)(?:\s+(\d{1,2}):(\d{2}))?", normalized)
            if relative_named_day:
                days = 1 if relative_named_day.group(1) == "昨天" else 2
                candidate = reference.replace(hour=0, minute=0, second=0, microsecond=0)
                candidate = candidate.fromtimestamp(candidate.timestamp() - days * 86400, UTC)
                if relative_named_day.group(2):
                    candidate = candidate.replace(hour=int(relative_named_day.group(2)), minute=int(relative_named_day.group(3)))
                return candidate
            month_day = re.fullmatch(r"(\d{1,2})-(\d{1,2})", normalized)
            if month_day:
                candidate = reference.replace(month=int(month_day.group(1)), day=int(month_day.group(2)), hour=0, minute=0, second=0, microsecond=0)
                if candidate > reference.replace(hour=0, minute=0, second=0, microsecond=0):
                    candidate = candidate.replace(year=candidate.year - 1)
                return candidate
            relative = re.fullmatch(r"(\d+)\s*(分钟|小时|天|minutes?|hours?|days?)前", normalized, flags=re.IGNORECASE)
            if relative:
                amount = int(relative.group(1))
                unit = relative.group(2).lower()
                seconds = amount * (60 if "分" in unit or "minute" in unit else 3600 if "小时" in unit or "hour" in unit else 86400)
                return reference.fromtimestamp(reference.timestamp() - seconds, UTC)
            return None
    return parsed.astimezone(UTC)


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value)
    query = urlencode([(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if not key.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


def _required_text(value: dict[str, object], key: str) -> str:
    text = str(value.get(key, "")).strip()
    if not text:
        raise ValueError(f"catalog field {key} is required")
    return text


def _xml_text(element: ElementTree.Element, tag: str) -> str:
    child = element.find(tag)
    return child.text or "" if child is not None else ""


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(unescape(value))
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _safe_message(error: Exception) -> str:
    return re.sub(r"https?://\S+", "[url]", str(error))[:300]


def _parse_jiqizhixin_articles(payload: dict[str, object], max_items: int) -> list[ListingItem]:
    if payload.get("success") is not True:
        raise PublicSourceError("machineheart article API reported failure", reason_code="LIST_RESPONSE_INVALID")
    articles = payload.get("articles")
    if not isinstance(articles, list):
        raise PublicSourceError("machineheart article API did not include articles", reason_code="LIST_RESPONSE_INVALID")
    items: list[ListingItem] = []
    for article in articles[:max_items]:
        if not isinstance(article, dict):
            continue
        slug = _clean_text(str(article.get("slug", "")))
        item_id = _clean_text(str(article.get("id", "")))
        url = f"https://www.jiqizhixin.com/articles/{slug}" if slug else ""
        items.append(
            ListingItem(
                source_item_key=item_id or url,
                title=_clean_text(str(article.get("title", ""))),
                digest=_clean_text(str(article.get("content", "")))[:800],
                url=url,
                published_at=_parse_jiqizhixin_published_at(_clean_text(str(article.get("publishedAt", "")))),
                cover_url=_clean_text(str(article.get("coverImageUrl", ""))),
            )
        )
    return items


def _parse_jiqizhixin_published_at(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
    except ValueError:
        return None


def _parse_qwen_articles(payload: dict[str, object], max_items: int) -> list[ListingItem]:
    if payload.get("success") is not True:
        raise PublicSourceError("qwen article API reported failure", reason_code="LIST_RESPONSE_INVALID")
    data = payload.get("data")
    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list):
        raise PublicSourceError("qwen article API did not include articles", reason_code="LIST_RESPONSE_INVALID")
    items: list[ListingItem] = []
    for article in articles[:max_items]:
        if not isinstance(article, dict):
            continue
        extra = article.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        path = _clean_text(str(article.get("path", "")))
        item_id = _clean_text(str(article.get("id", "")))
        url = f"https://qwen.ai/blog?{urlencode({'id': path})}" if path else ""
        # The response can include full article HTML. Keep only the list introduction.
        items.append(
            ListingItem(
                source_item_key=item_id or url,
                title=_clean_text(str(article.get("title", ""))),
                digest=_clean_text(str(extra.get("introduction", "")))[:800],
                url=url,
                published_at=parse_published_at(_clean_text(str(extra.get("date", "")))),
                cover_url=_clean_text(str(extra.get("cover_small", ""))),
            )
        )
    return items


_JSON_LIST_ADAPTERS: dict[str, Any] = {
    "jiqizhixin": _parse_jiqizhixin_articles,
    "qwen": _parse_qwen_articles,
}


_INFOQ_AI_BRIEFS_SCRIPT = """
() => {
  const raw = Array.from(document.scripts).map((script) => script.textContent || '').find((value) => value.includes('aibriefsList'));
  if (!raw) return [];
  let table;
  try { table = JSON.parse(raw); } catch (_) { return []; }
  const cache = new Map();
  const resolveRef = (index) => {
    if (cache.has(index)) return cache.get(index);
    const value = table[index];
    if (value === null || typeof value !== 'object') return value;
    if (Array.isArray(value) && typeof value[0] === 'string' && ['ShallowReactive', 'Reactive', 'Ref'].includes(value[0])) {
      const resolved = resolveRef(value[1]);
      cache.set(index, resolved);
      return resolved;
    }
    const resolved = Array.isArray(value) ? [] : {};
    cache.set(index, resolved);
    if (Array.isArray(value)) value.forEach((child) => resolved.push(resolveRef(child)));
    else Object.entries(value).forEach(([key, child]) => { resolved[key] = resolveRef(child); });
    return resolved;
  };
  const items = resolveRef(0)?.data?.aibriefsList?.list;
  if (!Array.isArray(items)) return [];
  return items.map((item) => ({
    title: item.title || '',
    digest: item.description || '',
    url: item.original_link || '',
    published: item.collect_time ? new Date(item.collect_time).toISOString() : '',
  }));
}
"""

_QBITAI_AI_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.article_list .picture_text')).map((card) => {
    const link = card.querySelector('.text_box h4 a[href]');
    const digest = Array.from(card.querySelectorAll('.text_box > p')).map(text).find((value) => value.length >= 8) || '';
    return { title: text(link), digest, url: link?.href || '', published: text(card.querySelector('.info .time')) };
  });
}
"""

_ZHIDX_AI_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('.info-list > li')).map((card) => {
    const link = card.querySelector('.info-left-title > a[href]');
    const digest = text(card.querySelector('.info-left-desc'));
    return {
      title: text(link),
      digest: /^\\d+$/.test(digest) ? '' : digest,
      url: link?.href || '',
      published: text(card.querySelector('.ilr-time')),
    };
  });
}
"""

_AIERA_HOME_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  // The site lists each post as <article class="fi ai-card"> with the headline in
  // h3 > a.ai-title-link, a "置顶" badge span, and a ".tm" stamp shaped
  // "HH:MM MM/DD". The stamp has no year, so derive it and roll back a year
  // when the month/day would otherwise land in the future.
  const now = new Date();
  const yearOf = (month, day) => {
    const candidate = new Date(now.getFullYear(), month - 1, day);
    if (candidate.getTime() - now.getTime() > 86400000) {
      return now.getFullYear() - 1;
    }
    return now.getFullYear();
  };
  return Array.from(document.querySelectorAll('article.ai-card')).map((card) => {
    const link = card.querySelector('h3 a.ai-title-link[href]');
    const badge = card.querySelector('.fpin');
    const title = text(link).replace(text(badge), '').trim();
    const stamp = text(card.querySelector('.tm'));
    const match = stamp.match(/(\\d{1,2}):(\\d{2})\\s*(\\d{1,2})[\\/-](\\d{1,2})/);
    let published = '';
    if (match) {
      const hours = match[1].padStart(2, '0');
      const minutes = match[2];
      const month = Number(match[3]);
      const day = Number(match[4]);
      published = yearOf(month, day) + '/' + String(month).padStart(2, '0') + '/' + String(day).padStart(2, '0') + ' ' + hours + ':' + minutes;
    }
    return {
      title: title,
      digest: text(card.querySelector('.fbd p')),
      url: link?.href || '',
      published: published,
    };
  });
}
"""


_ANDROID_DEVELOPERS_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('a.adb--post-list-link[href*="/blog/posts/"]')).map((link) => {
    // Android Developers currently exposes both featured-category <li> cards
    // and the older post-list-item-inner layout.
    const card = link.closest('li.adb--post-list-item') || link.closest('.adb--post-list-item-inner');
    const time = card?.querySelector('time[datetime]');
    return {
      title: text(link),
      digest: text(card?.querySelector('.adb--post-list-item-description p, .adb--post-list-item-body p')),
      url: link.href,
      published: time?.getAttribute('datetime') || '',
    };
  });
}
"""

_APPLE_DEVELOPER_NEWS_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  return Array.from(document.querySelectorAll('section.article-content-container')).map((card) => {
    const link = card.querySelector('a.article-title[href*="/news/?id="]');
    return {
      title: text(link),
      digest: text(card.querySelector('.article-text > p')),
      url: link?.href || '',
      published: text(card.querySelector('p.article-date')),
    };
  });
}
"""


_HUXIU_AI_NEWS_SCRIPT = """
() => {
  const node = document.getElementById('__NUXT_DATA__');
  if (!node?.textContent) return [];
  let data;
  try { data = JSON.parse(node.textContent); } catch (_) { return []; }
  const resolve = (value) => Number.isInteger(value) && value >= 0 && value < data.length ? data[value] : value;
  let references = null;
  for (const entry of data) {
    if (entry && typeof entry === 'object' && !Array.isArray(entry) && 'aiNewsList' in entry) {
      const value = resolve(entry.aiNewsList);
      if (Array.isArray(value)) { references = value; break; }
    }
  }
  if (!references) {
    for (const entry of data) {
      if (!Array.isArray(entry) || !Array.isArray(entry[0])) continue;
      const marker = entry[0];
      if (marker[0] !== 'ShallowReactive') continue;
      const template = resolve(marker[1]);
      if (template && typeof template === 'object' && 'ainews_id' in template) {
        references = entry.slice(1);
        break;
      }
    }
  }
  if (!Array.isArray(references)) return [];
  return references.flatMap((reference) => {
    const article = resolve(reference);
    if (!article || typeof article !== 'object') return [];
    const id = resolve(article.ainews_id);
    const title = resolve(article.title);
    const digest = resolve(article.desc);
    const timestamp = Number(resolve(article.publish_time));
    const published = Number.isFinite(timestamp)
      ? new Date(timestamp > 1e12 ? timestamp : timestamp * 1000).toISOString()
      : '';
    return [{
      title: title || '',
      digest: digest || '',
      url: id ? `https://www.huxiu.com/ainews/${id}.html` : '',
      published,
    }];
  });
}
"""


_AIBASE_NEWS_SCRIPT = """
() => {
  const text = (node) => (node?.innerText || node?.textContent || '').replace(/\\s+/g, ' ').trim();
  const timePattern = /(?:刚刚|\\d+\\s*(?:分钟|小时|天)前)/;
  return Array.from(document.querySelectorAll('div.grid a[href*="/zh/news/"]')).map((link) => {
    const fields = (link.innerText || link.textContent || '')
      .split(/\\n+/)
      .map((value) => value.replace(/\\s+/g, ' ').trim())
      .filter(Boolean);
    const title = fields[0] || '';
    const digest = fields[1] || '';
    const published = fields.find((value) => timePattern.test(value)) || '';
    return {title, digest, url: link.href || '', published};
  });
}
"""


_BROWSER_LIST_ADAPTERS: dict[str, BrowserListAdapter] = {
    "infoq": BrowserListAdapter(
        ready_selector='div[class*="_item_"]',
        extraction_script=_INFOQ_AI_BRIEFS_SCRIPT,
        requires_interactive=True,
    ),
    "huxiu": BrowserListAdapter(
        ready_selector='script#__NUXT_DATA__',
        extraction_script=_HUXIU_AI_NEWS_SCRIPT,
        requires_interactive=True,
        blocked_markers=("aliyun_waf", "滑动验证", "verification"),
        ready_state="attached",
    ),
    "aibase": BrowserListAdapter(
        ready_selector='div.grid a[href*="/zh/news/"]',
        extraction_script=_AIBASE_NEWS_SCRIPT,
        allowed_path_prefixes=("/zh/news/",),
    ),
    "qbitai": BrowserListAdapter(
        ready_selector='.article_list .picture_text h4 a',
        extraction_script=_QBITAI_AI_SCRIPT,
    ),
    "zhidx": BrowserListAdapter(
        ready_selector='.info-list > li .info-left-title > a',
        extraction_script=_ZHIDX_AI_SCRIPT,
    ),
    "aiera": BrowserListAdapter(
        ready_selector='article.ai-card h3 a.ai-title-link',
        extraction_script=_AIERA_HOME_SCRIPT,
        allowed_path_prefixes=("/asi-item.html",),
    ),
    "android-developers": BrowserListAdapter(
        ready_selector='a.adb--post-list-link[href*="/blog/posts/"]',
        extraction_script=_ANDROID_DEVELOPERS_SCRIPT,
        allowed_path_prefixes=("/blog/posts/",),
    ),
    "apple-developer": BrowserListAdapter(
        ready_selector='section.article-content-container a.article-title[href*="/news/?id="]',
        extraction_script=_APPLE_DEVELOPER_NEWS_SCRIPT,
        allowed_path_prefixes=("/news/",),
    ),
}


_BROWSER_NETWORK_ADAPTERS: dict[str, BrowserNetworkAdapter] = {
    "jiqizhixin": BrowserNetworkAdapter(
        response_url_fragment="/api/article_library/articles.json",
    ),
    "qwen": BrowserNetworkAdapter(
        response_url_fragment="/api/v2/article/retrieval",
    ),
}


# A detail request is allowed only for sources whose public article URL shape
# has already been verified. Adding a source here is an explicit admission
# decision, never a generic fallback for arbitrary links.
_DETAIL_ENRICHMENT_POLICIES: dict[str, DetailEnrichmentPolicy] = {
    "android-developers": DetailEnrichmentPolicy(allowed_hosts=("developer.android.com",)),
    "qbitai": DetailEnrichmentPolicy(allowed_hosts=("qbitai.com",)),
    "zhidx": DetailEnrichmentPolicy(allowed_hosts=("zhidx.com",)),
}
