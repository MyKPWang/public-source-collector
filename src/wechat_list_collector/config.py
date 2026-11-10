from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Settings:
    database: Path
    pages_per_feed: int
    page_size: int
    minimum_interval_seconds: int
    cooldown_seconds: int
    request_timeout_seconds: int
    log_file: Path = Path("data/collector.log")
    log_level: str = "INFO"
    log_max_bytes: int = 1_048_576
    log_backup_count: int = 5
    login_timeout_seconds: int = 300
    login_browser: str = "chrome"
    login_delivery_mode: str = "desktop"
    account_search_minimum_interval_seconds: int = 15
    public_sources_catalog: Path = Path("public-sources.toml")
    public_sources_request_timeout_seconds: int = 20
    public_sources_user_agent: str = "PublicNewsCollector/1.0 (+local-pipeline)"
    public_sources_browser: str = "chrome"
    public_sources_browser_mode: str = "headless"
    public_sources_browser_profile: Path = Path("data/public-browser-profile")
    public_sources_proxy_url: str = ""
    public_sources_detail_enrichment_enabled: bool = False
    public_sources_detail_enrichment_max_items_per_endpoint: int = 2
    public_sources_detail_enrichment_timeout_seconds: int = 30
    public_sources_detail_enrichment_directory: Path = Path("data/crawl4ai")
    public_sources_stale_run_seconds: int = 1_800
    public_health_summary_directory: Path = Path("data/health")
    public_health_webhook_url: str = ""
    public_health_stale_after_hours: int = 48
    public_health_baseline_runs: int = 3
    reader_minimum_interval_seconds: int = 12
    reader_jitter_min_seconds: int = 3
    reader_jitter_max_seconds: int = 8
    reader_cooldown_seconds: int = 1800
    reader_rolling_window_limit: int = 600
    reader_batch_limit: int = 200
    reader_summary_minimum_interval_seconds: int = 12
    reader_summary_max_items_per_page: int = 2
    reader_summary_rolling_window_limit: int = 200
    reader_summary_batch_limit: int = 60


def load_settings(config_path: Path) -> Settings:
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    storage = raw.get("storage", {})
    collection = raw.get("collection", {})
    rate_limit = raw.get("rate_limit", {})
    logging = raw.get("logging", {})
    database = Path(storage.get("database", "data/collector.db"))
    if not database.is_absolute():
        database = config_path.parent / database
    log_file = Path(logging.get("file", "data/collector.log"))
    if not log_file.is_absolute():
        log_file = config_path.parent / log_file

    pages = int(collection.get("pages_per_feed", 3))
    page_size = int(collection.get("page_size", 5))
    minimum_interval = int(rate_limit.get("minimum_interval_seconds", 240))
    cooldown = int(rate_limit.get("cooldown_seconds", 21600))
    timeout = int(rate_limit.get("request_timeout_seconds", 30))
    reader_limits = rate_limit.get("reader", {}) or {}
    reader_minimum_interval = int(reader_limits.get("minimum_interval_seconds", 12))
    reader_jitter_min = int(reader_limits.get("jitter_min_seconds", 3))
    reader_jitter_max = int(reader_limits.get("jitter_max_seconds", 8))
    reader_cooldown = int(reader_limits.get("cooldown_seconds", 1800))
    reader_rolling_window_limit = int(reader_limits.get("rolling_window_limit", 600))
    reader_batch_limit = int(reader_limits.get("batch_limit", 200))
    reader_summary_limits = rate_limit.get("reader_summary", {}) or {}
    reader_summary_minimum_interval = int(reader_summary_limits.get("minimum_interval_seconds", 12))
    reader_summary_max_items_per_page = int(reader_summary_limits.get("max_items_per_page", 2))
    reader_summary_rolling_window_limit = int(reader_summary_limits.get("rolling_window_limit", 200))
    reader_summary_batch_limit = int(reader_summary_limits.get("batch_limit", 60))
    log_level = str(logging.get("level", "INFO")).upper()
    log_max_bytes = int(logging.get("max_bytes", 1_048_576))
    log_backup_count = int(logging.get("backup_count", 5))
    auth = raw.get("auth", {})
    login_timeout_seconds = int(auth.get("login_timeout_seconds", 300))
    login_browser = str(auth.get("browser", "chrome"))
    login_delivery_mode = str(auth.get("delivery_mode", "desktop"))
    account_search_minimum_interval_seconds = int(auth.get("search_minimum_interval_seconds", 15))
    public_sources = raw.get("public_sources", {})
    catalog = Path(str(public_sources.get("catalog", "public-sources.toml")))
    if not catalog.is_absolute():
        catalog = config_path.parent / catalog
    public_sources_request_timeout_seconds = int(public_sources.get("request_timeout_seconds", 20))
    public_sources_user_agent = str(public_sources.get("user_agent", "PublicNewsCollector/1.0 (+local-pipeline)"))
    public_sources_browser = str(public_sources.get("browser", "chrome"))
    public_sources_browser_mode = str(public_sources.get("browser_mode", "headless"))
    public_sources_browser_profile = Path(str(public_sources.get("browser_profile", "data/public-browser-profile")))
    if not public_sources_browser_profile.is_absolute():
        public_sources_browser_profile = config_path.parent / public_sources_browser_profile
    public_sources_proxy_url = str(public_sources.get("proxy_url", "")).strip()
    detail_enrichment = raw.get("detail_enrichment", {})
    public_sources_detail_enrichment_enabled = bool(detail_enrichment.get("enabled", False))
    public_sources_detail_enrichment_max_items_per_endpoint = int(detail_enrichment.get("max_items_per_endpoint", 2))
    public_sources_detail_enrichment_timeout_seconds = int(detail_enrichment.get("timeout_seconds", 30))
    public_sources_detail_enrichment_directory = Path(str(detail_enrichment.get("base_directory", "data/crawl4ai")))
    if not public_sources_detail_enrichment_directory.is_absolute():
        public_sources_detail_enrichment_directory = config_path.parent / public_sources_detail_enrichment_directory
    public_sources_stale_run_seconds = int(public_sources.get("stale_run_seconds", 1_800))
    health = raw.get("health", {})
    public_health_summary_directory = Path(str(health.get("summary_directory", "data/health")))
    if not public_health_summary_directory.is_absolute():
        public_health_summary_directory = config_path.parent / public_health_summary_directory
    public_health_webhook_url = str(health.get("webhook_url", "")).strip()
    public_health_stale_after_hours = int(health.get("stale_after_hours", 48))
    public_health_baseline_runs = int(health.get("baseline_runs", 3))
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("logging.level must be DEBUG, INFO, WARNING, or ERROR")
    if login_browser not in {"chrome", "chromium"}:
        raise ValueError("auth.browser must be chrome or chromium")
    if public_sources_browser not in {"chrome", "chromium"}:
        raise ValueError("public_sources.browser must be chrome or chromium")
    if public_sources_browser_mode not in {"headless", "interactive"}:
        raise ValueError("public_sources.browser_mode must be headless or interactive")
    if login_delivery_mode not in {"desktop", "web_qr"}:
        raise ValueError("auth.delivery_mode must be desktop or web_qr")
    if (
        pages < 1
        or page_size < 1
        or minimum_interval < 0
        or cooldown < 1
        or timeout < 1
        or log_max_bytes < 1
        or log_backup_count < 1
        or login_timeout_seconds < 30
        or account_search_minimum_interval_seconds < 0
        or public_sources_request_timeout_seconds < 1
        or public_sources_detail_enrichment_max_items_per_endpoint < 1
        or public_sources_detail_enrichment_timeout_seconds < 1
        or public_sources_stale_run_seconds < 60
        or public_health_stale_after_hours < 1
        or public_health_baseline_runs < 1
        or reader_minimum_interval < 0
        or reader_jitter_min < 0
        or reader_jitter_max < reader_jitter_min
        or reader_cooldown < 1
        or reader_rolling_window_limit < 1
        or reader_batch_limit < 1
        or reader_summary_minimum_interval < 0
        or reader_summary_max_items_per_page < 1
        or reader_summary_rolling_window_limit < 1
        or reader_summary_batch_limit < 1
    ):
        raise ValueError("collector configuration contains an invalid non-positive value")

    return Settings(
        database=database,
        pages_per_feed=pages,
        page_size=page_size,
        minimum_interval_seconds=minimum_interval,
        cooldown_seconds=cooldown,
        request_timeout_seconds=timeout,
        log_file=log_file,
        log_level=log_level,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        login_timeout_seconds=login_timeout_seconds,
        login_browser=login_browser,
        login_delivery_mode=login_delivery_mode,
        account_search_minimum_interval_seconds=account_search_minimum_interval_seconds,
        public_sources_catalog=catalog,
        public_sources_request_timeout_seconds=public_sources_request_timeout_seconds,
        public_sources_user_agent=public_sources_user_agent,
        public_sources_browser=public_sources_browser,
        public_sources_browser_mode=public_sources_browser_mode,
        public_sources_browser_profile=public_sources_browser_profile,
        public_sources_proxy_url=public_sources_proxy_url,
        public_sources_detail_enrichment_enabled=public_sources_detail_enrichment_enabled,
        public_sources_detail_enrichment_max_items_per_endpoint=public_sources_detail_enrichment_max_items_per_endpoint,
        public_sources_detail_enrichment_timeout_seconds=public_sources_detail_enrichment_timeout_seconds,
        public_sources_detail_enrichment_directory=public_sources_detail_enrichment_directory,
        public_sources_stale_run_seconds=public_sources_stale_run_seconds,
        public_health_summary_directory=public_health_summary_directory,
        public_health_webhook_url=public_health_webhook_url,
        public_health_stale_after_hours=public_health_stale_after_hours,
        public_health_baseline_runs=public_health_baseline_runs,
        reader_minimum_interval_seconds=reader_minimum_interval,
        reader_jitter_min_seconds=reader_jitter_min,
        reader_jitter_max_seconds=reader_jitter_max,
        reader_cooldown_seconds=reader_cooldown,
        reader_rolling_window_limit=reader_rolling_window_limit,
        reader_batch_limit=reader_batch_limit,
        reader_summary_minimum_interval_seconds=reader_summary_minimum_interval,
        reader_summary_max_items_per_page=reader_summary_max_items_per_page,
        reader_summary_rolling_window_limit=reader_summary_rolling_window_limit,
        reader_summary_batch_limit=reader_summary_batch_limit,
    )
