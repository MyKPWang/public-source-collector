from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import load_settings
from .errors import CollectorError
from .logging_config import configure_logging
from .operations import OperationError
from .service import CollectorService
from .session_import import import_werss_license
from .store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="public-source-collector")
    parser.add_argument("--config", type=Path, default=Path("collector.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    database = commands.add_parser("db")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("upgrade")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    backfill = commands.add_parser("backfill-summaries")
    backfill.add_argument("--limit", type=int, default=60)
    backfill.add_argument("--command-id")
    enqueue = commands.add_parser("enqueue")
    enqueue_commands = enqueue.add_subparsers(dest="enqueue_command", required=True)
    collect_all = enqueue_commands.add_parser("collect-all")
    collect_all.add_argument("--business-date")
    collect_all.add_argument("--idempotency-key")
    collect_account = enqueue_commands.add_parser("collect-account")
    collect_account.add_argument("--account-id", required=True)
    collect_account.add_argument("--idempotency-key", required=True)
    api = commands.add_parser("api")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=4100)
    admin = commands.add_parser("admin")
    admin.add_argument("--host", default="127.0.0.1")
    admin.add_argument("--port", type=int, default=4101)
    backup = commands.add_parser("backup")
    backup.add_argument("--to", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--from", dest="from_path", type=Path, required=True)
    commands.add_parser("integrity-check")
    auth = commands.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_import = auth_commands.add_parser("import-werss-license")
    auth_import.add_argument("--license", type=Path, required=True)
    auth_commands.add_parser("status")
    commands.add_parser("init")
    importer = commands.add_parser("import-werss-license")
    importer.add_argument("--license", type=Path, required=True)
    search = commands.add_parser("search")
    search.add_argument("keyword")
    search.add_argument("--limit", type=int, default=5)
    add = commands.add_parser("add-feed")
    add.add_argument("--id", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--fakeid", required=True)
    commands.add_parser("list-feeds")
    fetch = commands.add_parser("fetch")
    target = fetch.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true")
    target.add_argument("--feed")
    commands.add_parser("status")
    runs = commands.add_parser("runs")
    runs.add_argument("--limit", type=int, default=20)
    sources = commands.add_parser("sources")
    source_commands = sources.add_subparsers(dest="sources_command", required=True)
    source_sync = source_commands.add_parser("sync")
    source_sync.add_argument("--catalog", type=Path)
    source_collect = source_commands.add_parser("collect")
    source_collect.add_argument("--catalog", type=Path)
    source_collect.add_argument("--scheduled", action="store_true")
    source_collect.add_argument("--idempotency-key")
    source_collect.add_argument("--source", dest="source_slugs", action="append", default=[])
    source_diagnose = source_commands.add_parser("diagnose")
    source_diagnose.add_argument("--run-id")
    source_health = source_commands.add_parser("health")
    source_health.add_argument("--run-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    logger = configure_logging(settings)
    if args.command == "db":
        if args.database_command == "upgrade":
            try:
                from .infrastructure.db import upgrade_database

                upgrade_database(settings.database)
                _print({"database": str(settings.database), "status": "upgraded"})
                return 0
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()
    if args.command in {"worker", "enqueue", "api", "admin", "backup", "restore", "integrity-check", "auth", "sources", "backfill-summaries"}:
        return _run_production_command(args, settings)
    store = Store(settings.database)
    store.initialize()
    try:
        if args.command == "init":
            logger.info("initialized", extra={"operation": "init"})
            _print({"database": str(settings.database), "status": "initialized"})
        elif args.command == "import-werss-license":
            store.save_credentials(import_werss_license(args.license))
            logger.info("credentials_imported", extra={"operation": "import_werss_license"})
            _print({"status": "credentials_imported"})
        elif args.command == "add-feed":
            logger.info("feed_added", extra={"operation": "add_feed", "feed_id": args.id, "feed_name": args.name})
            _print(store.add_feed(args.id, args.name, args.fakeid).__dict__)
        elif args.command == "list-feeds":
            _print([feed.__dict__ for feed in store.list_feeds()])
        elif args.command == "runs":
            if args.limit < 1:
                raise ValueError("runs limit must be positive")
            _print(store.list_runs(args.limit))
        else:
            service = CollectorService(store, settings)
            if args.command == "search":
                _print(service.search(args.keyword, args.limit))
            elif args.command == "fetch":
                result = service.fetch_all() if args.all else service.fetch_feed(args.feed)
                _print([item.__dict__ for item in result])
            elif args.command == "status":
                _print(service.status())
    except (CollectorError, OperationError, KeyError, ValueError) as error:
        _print({"status": "error", "message": str(error)})
        return 2
    finally:
        store.close()
    return 0


def _run_production_command(args, settings: Settings) -> int:
    from .application.collection_runner import CollectionRunner
    from .application.account_search import AccountSearchCoordinator
    from .application.read_models import ReadModel
    from .application.public_read_models import PublicReadModel
    from .application.public_sources import Crawl4AIDetailEnricher, HybridListFetcher, PublicSourceCollector, load_source_catalog
    from .application.worker import CollectionWorker
    from .bootstrap import create_production_runtime
    from .entrypoints.read_api import create_read_api
    from .entrypoints.admin_app import create_admin_app
    from .infrastructure.wechat import PlaywrightLoginCoordinator, RefreshingRequestsWeChatGateway, RequestsWeChatGateway
    from .application.summary_backfill import SummaryBackfiller
    from .application.summary_enricher import SummaryEnricher
    from .application.summary_service import SummaryFetchService
    from .infrastructure.weread.gateway import WeReadArticleGateway
    from .operations import backup_database, integrity_check, restore_database
    from .session_import import import_werss_license
    from .infrastructure.db.public_sources_repository import PublicSourceRepository

    if args.command == "restore":
        restore_database(args.from_path, settings.database)
        _print({"database": str(settings.database), "status": "restored"})
        return 0

    runtime = create_production_runtime(settings)

    def new_public_collector(catalog_path: Path):
        catalog = load_source_catalog(catalog_path)
        repository = PublicSourceRepository(runtime.repository.engine)
        detail_enricher = (
            Crawl4AIDetailEnricher(
                timeout_seconds=settings.public_sources_detail_enrichment_timeout_seconds,
                max_items_per_endpoint=settings.public_sources_detail_enrichment_max_items_per_endpoint,
                browser=settings.public_sources_browser,
                base_directory=settings.public_sources_detail_enrichment_directory,
            )
            if settings.public_sources_detail_enrichment_enabled
            else None
        )
        return catalog, repository, PublicSourceCollector(
            repository,
            HybridListFetcher(
                timeout_seconds=settings.public_sources_request_timeout_seconds,
                user_agent=settings.public_sources_user_agent,
                browser=settings.public_sources_browser,
                browser_mode=settings.public_sources_browser_mode,
                browser_profile=settings.public_sources_browser_profile,
                proxy_url=settings.public_sources_proxy_url,
            ),
            detail_enricher=detail_enricher,
            stale_run_seconds=settings.public_sources_stale_run_seconds,
            health_stale_after_hours=settings.public_health_stale_after_hours,
            health_baseline_runs=settings.public_health_baseline_runs,
        )

    def refresh_public_sources(source_slugs: tuple[str, ...], idempotency_key: str | None) -> dict[str, object]:
        catalog_path = settings.public_sources_catalog
        catalog, _, collector = new_public_collector(catalog_path)
        collector.sync_catalog(catalog)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        scope = "all" if not source_slugs else hashlib.sha256(",".join(source_slugs).encode("utf-8")).hexdigest()[:16]
        key = idempotency_key or f"pipeline:manual:{now.strftime('%Y%m%dT%H%M%S%f')}:{scope}"
        result = collector.collect_all(
            trigger="pipeline",
            idempotency_key=key,
            business_date=now.date().isoformat(),
            source_slugs=source_slugs,
        )
        result["health"] = _persist_public_health(settings, result["health"])
        return {"refresh": result, "selected_source_slugs": list(source_slugs)}

    if args.command == "sources":
        catalog_path = getattr(args, "catalog", None) or settings.public_sources_catalog
        catalog, repository, collector = new_public_collector(catalog_path)
        if args.sources_command == "diagnose":
            _print(repository.diagnostics(args.run_id))
            return 0
        if args.sources_command == "health":
            _print(
                repository.health_summary(
                    args.run_id,
                    stale_after_hours=settings.public_health_stale_after_hours,
                    baseline_runs=settings.public_health_baseline_runs,
                )
            )
            return 0
        if args.sources_command == "sync":
            _print({"catalog": str(catalog_path), **collector.sync_catalog(catalog)})
            return 0
        collector.sync_catalog(catalog)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        business_date = now.date().isoformat() if args.scheduled else None
        selected_source_slugs = tuple(args.source_slugs)
        scope = "all" if not selected_source_slugs else hashlib.sha256(",".join(sorted(selected_source_slugs)).encode("utf-8")).hexdigest()[:16]
        key = args.idempotency_key or (
            f"public-sources:daily:{business_date}:{scope}"
            if args.scheduled
            else f"public-sources:manual:{now.strftime('%Y%m%dT%H%M%S%f')}:{scope}"
        )
        result = collector.collect_all(
            trigger="scheduled" if args.scheduled else "manual_cli",
            idempotency_key=key,
            business_date=business_date,
            source_slugs=selected_source_slugs,
        )
        result["health"] = _persist_public_health(settings, result["health"])
        _print(result)
        return 0
    if args.command == "auth":
        if args.auth_command == "import-werss-license":
            runtime.vault.replace_wechat(import_werss_license(args.license))
            _print({"status": "credentials_imported"})
            return 0
        try:
            runtime.vault.load_wechat()
        except Exception:
            _print({"status": "missing"})
            return 2
        _print({"status": "available"})
        return 0
    if args.command == "backup":
        backup_database(settings.database, args.to)
        _print({"database": str(settings.database), "backup": str(args.to), "status": "backed_up"})
        return 0
    if args.command == "integrity-check":
        result = integrity_check(settings.database)
        _print({"database": str(settings.database), "result": result, "status": "ok" if result == "ok" else "error"})
        return 0 if result == "ok" else 2
    if args.command == "enqueue":
        if args.enqueue_command == "collect-all":
            account_ids = runtime.repository.enabled_account_ids()
            if not account_ids:
                _print({"status": "error", "message": "no enabled accounts"})
                return 2
            business_date = args.business_date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            submitted = runtime.repository.submit_collection_command(
                command_type="collect_all",
                trigger="scheduled",
                account_ids=account_ids,
                idempotency_key=args.idempotency_key or f"scheduled:daily:{business_date}",
                business_date=business_date,
            )
        else:
            submitted = runtime.repository.submit_collection_command(
                command_type="collect_account",
                trigger="manual_cli",
                account_ids=[args.account_id],
                idempotency_key=args.idempotency_key,
            )
        _print({"command_id": submitted.command_id, "run_id": submitted.run_id, "reused": submitted.reused})
        return 0
    if args.command == "backfill-summaries":
        reader_gateway = WeReadArticleGateway(runtime.vault, settings.request_timeout_seconds)
        command_id = args.command_id or runtime.repository.latest_command_id()
        if not command_id:
            _print({"status": "error", "message": "no collection command to attribute the requests to"})
            return 2
        result = SummaryBackfiller(
            runtime.repository,
            SummaryFetchService(
                runtime.reader_gate,
                reader_gateway,
                minimum_interval_seconds=settings.reader_summary_minimum_interval_seconds,
            ),
            limit=args.limit,
        ).run(command_id=command_id)
        _print(result.as_dict())
        return 0
    if args.command == "worker":
        credentials = runtime.vault.load_wechat()
        reader_gateway = WeReadArticleGateway(runtime.vault, settings.request_timeout_seconds)
        worker = CollectionWorker(
            runtime.repository,
            CollectionRunner(
                runtime.repository,
                runtime.reader_gate,
                reader_gateway,
                runtime.vault,
                publish_record_page_size=settings.page_size,
                max_pages=settings.pages_per_feed,
                summary_enricher=SummaryEnricher(
                    runtime.repository,
                    SummaryFetchService(
                        runtime.reader_gate,
                        reader_gateway,
                        minimum_interval_seconds=settings.reader_summary_minimum_interval_seconds,
                    ),
                    max_items_per_page=settings.reader_summary_max_items_per_page,
                ),
            ),
            runtime.reader_gate,
            worker_id=f"{socket.gethostname()}:{os.getpid()}",
        )
        if args.once:
            _print({"processed": worker.run_once()})
        else:
            worker.run_forever()
        return 0
    import uvicorn
    if args.command == "api":
        token = os.getenv("PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN") or os.getenv("WECHAT_COLLECTOR_PIPELINE_TOKEN")
        if not token:
            _print({"status": "error", "message": "PUBLIC_SOURCE_COLLECTOR_PIPELINE_TOKEN is not set"})
            return 2
        app = create_read_api(
            PublicReadModel(runtime.repository.engine),
            bearer_token=token,
            schema_version="2.1",
            refresh_public_sources=refresh_public_sources,
            wechat_read_model=ReadModel(runtime.repository.engine),
        )
    else:
        password = os.getenv("WECHAT_COLLECTOR_ADMIN_PASSWORD")
        session_secret = os.getenv("WECHAT_COLLECTOR_ADMIN_SESSION_SECRET")
        if not password or not session_secret:
            _print({"status": "error", "message": "WECHAT_COLLECTOR_ADMIN_PASSWORD and WECHAT_COLLECTOR_ADMIN_SESSION_SECRET are required"})
            return 2
        app = create_admin_app(
            runtime.repository,
            ReadModel(runtime.repository.engine),
            runtime.vault,
            PlaywrightLoginCoordinator(
                runtime.repository,
                runtime.vault,
                timeout_seconds=settings.login_timeout_seconds,
                browser=settings.login_browser,
                delivery_mode=settings.login_delivery_mode,
            ),
            AccountSearchCoordinator(
                runtime.repository,
                runtime.vault,
                runtime.gate,
                lambda: RequestsWeChatGateway(runtime.vault.load_wechat(), settings.request_timeout_seconds),
                minimum_interval_seconds=settings.account_search_minimum_interval_seconds,
            ),
            admin_password=password,
            session_secret=session_secret,
        )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _persist_public_health(settings, summary: dict[str, object]) -> dict[str, object]:
    """Write one inspectable health artifact and optionally send compact alerts."""
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    artifact = {**summary, "generated_at": generated_at}
    directory = settings.public_health_summary_directory
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(summary.get("run_id") or "unknown")
    business_date = str(summary.get("business_date") or generated_at[:10])
    output_path = directory / f"{business_date}-{run_id}.json"
    _write_json(output_path, artifact)
    _write_json(directory / "latest.json", artifact)
    artifact["artifact_path"] = str(output_path)

    alerts = summary.get("alerts")
    logger = logging.getLogger("public_source_collector")
    logger.info(
        "public_source_health_summary",
        extra={
            "run_id": run_id,
            "operation": "summarize_public_source_health",
            "status": summary.get("status", "unknown"),
            "new_count": summary.get("new_article_count", 0),
            "updated_count": summary.get("updated_article_count", 0),
            "failed_endpoint_count": summary.get("failed_endpoint_count", 0),
            "alert_count": len(alerts) if isinstance(alerts, list) else 0,
        },
    )
    if isinstance(alerts, list) and alerts and settings.public_health_webhook_url:
        _send_public_health_alert(settings.public_health_webhook_url, artifact)
    return artifact


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _send_public_health_alert(webhook_url: str, summary: dict[str, object]) -> None:
    """Alert delivery is best-effort and never changes collection success."""
    payload = {
        "event": "public_source_health_alert",
        "run_id": summary.get("run_id"),
        "status": summary.get("status"),
        "business_date": summary.get("business_date"),
        "failed_endpoint_count": summary.get("failed_endpoint_count"),
        "new_article_count": summary.get("new_article_count"),
        "alerts": summary.get("alerts"),
    }
    request = Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except (OSError, URLError) as error:
        logging.getLogger("public_source_collector").warning(
            "public_source_health_alert_failed",
            extra={"operation": "deliver_public_source_alert", "reason_code": "HEALTH_ALERT_DELIVERY_FAILED", "error_message": str(error)[:300]},
        )
