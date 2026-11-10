from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING

from sqlalchemy import Engine, and_, func, or_, select, update

from ...domain.ids import new_uuid7
from .schema import (
    public_article_enrichment_attempts,
    public_collection_runs,
    public_endpoint_run_articles,
    public_endpoint_runs,
    public_source_articles,
    public_source_endpoints,
    public_sources,
)

if TYPE_CHECKING:
    from ...application.public_sources import DetailEnrichmentAttempt, ListingItem, SourceDefinition


class PublicSourceRepository:
    """Persistence for public list endpoints and their metadata-only articles."""

    def __init__(self, engine: Engine, *, now: callable | None = None):
        self.engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def sync_catalog(self, catalog: Iterable[SourceDefinition]) -> dict[str, int]:
        catalog = tuple(catalog)
        now = self._timestamp()
        source_count = endpoint_count = disabled_source_count = disabled_endpoint_count = 0
        catalog_slugs = {source.slug for source in catalog}
        with self.engine.begin() as connection:
            for source in catalog:
                source_id = connection.execute(
                    select(public_sources.c.id).where(public_sources.c.slug == source.slug)
                ).scalar_one_or_none()
                values = {
                    "name": source.name,
                    "homepage_url": source.homepage_url,
                    "enabled": int(source.enabled),
                    "updated_at": now,
                }
                if source_id is None:
                    source_id = new_uuid7()
                    connection.execute(public_sources.insert().values(id=source_id, slug=source.slug, created_at=now, **values))
                else:
                    connection.execute(update(public_sources).where(public_sources.c.id == source_id).values(**values))
                source_count += 1
                for endpoint in source.endpoints:
                    endpoint_id = connection.execute(
                        select(public_source_endpoints.c.id).where(
                            public_source_endpoints.c.source_id == source_id,
                            public_source_endpoints.c.endpoint_key == endpoint.key,
                        )
                    ).scalar_one_or_none()
                    endpoint_values = {
                        "kind": endpoint.kind,
                        "list_url": endpoint.list_url,
                        "enabled": int(source.enabled and endpoint.enabled),
                        "minimum_interval_seconds": endpoint.minimum_interval_seconds,
                        "max_items": endpoint.max_items,
                        "updated_at": now,
                    }
                    if endpoint_id is None:
                        connection.execute(
                            public_source_endpoints.insert().values(
                                id=new_uuid7(), source_id=source_id, endpoint_key=endpoint.key, created_at=now, **endpoint_values
                            )
                        )
                    else:
                        connection.execute(update(public_source_endpoints).where(public_source_endpoints.c.id == endpoint_id).values(**endpoint_values))
                    endpoint_count += 1
                active_endpoint_keys = {endpoint.key for endpoint in source.endpoints}
                stale_endpoints = connection.execute(
                    select(public_source_endpoints.c.id, public_source_endpoints.c.enabled).where(
                        public_source_endpoints.c.source_id == source_id,
                        ~public_source_endpoints.c.endpoint_key.in_(active_endpoint_keys),
                    )
                ).mappings().all()
                for stale_endpoint in stale_endpoints:
                    if stale_endpoint["enabled"]:
                        disabled_endpoint_count += 1
                    connection.execute(
                        update(public_source_endpoints)
                        .where(public_source_endpoints.c.id == stale_endpoint["id"])
                        .values(enabled=0, updated_at=now)
                    )

            stale_sources = connection.execute(
                select(public_sources.c.id, public_sources.c.enabled).where(~public_sources.c.slug.in_(catalog_slugs))
            ).mappings().all()
            for stale_source in stale_sources:
                if stale_source["enabled"]:
                    disabled_source_count += 1
                connection.execute(
                    update(public_sources).where(public_sources.c.id == stale_source["id"]).values(enabled=0, updated_at=now)
                )
                disabled_endpoint_count += connection.execute(
                    update(public_source_endpoints)
                    .where(public_source_endpoints.c.source_id == stale_source["id"], public_source_endpoints.c.enabled == 1)
                    .values(enabled=0, updated_at=now)
                ).rowcount or 0
        return {
            "source_count": source_count,
            "endpoint_count": endpoint_count,
            "disabled_source_count": disabled_source_count,
            "disabled_endpoint_count": disabled_endpoint_count,
        }

    def recover_stale_runs(self, *, stale_after_seconds: int = 1_800) -> list[str]:
        """Fail abandoned runs so an interrupted process cannot block all future work."""
        if stale_after_seconds < 60:
            raise ValueError("stale_after_seconds must be at least 60")
        now_dt = self._now().astimezone(UTC)
        now = self._timestamp(now_dt)
        cutoff = self._timestamp(now_dt - timedelta(seconds=stale_after_seconds))
        with self.engine.begin() as connection:
            stale_ids = [
                str(value)
                for value in connection.execute(
                    select(public_collection_runs.c.id).where(
                        public_collection_runs.c.status == "running",
                        public_collection_runs.c.started_at < cutoff,
                    )
                ).scalars()
            ]
            if not stale_ids:
                return []
            connection.execute(
                update(public_endpoint_runs)
                .where(public_endpoint_runs.c.run_id.in_(stale_ids), public_endpoint_runs.c.status == "running")
                .values(status="failed", finished_at=now, reason_code="RUN_LEASE_EXPIRED", message="collection process did not finish before lease expiry")
            )
            connection.execute(
                update(public_collection_runs)
                .where(public_collection_runs.c.id.in_(stale_ids))
                .values(status="failed", finished_at=now, reason_code="RUN_LEASE_EXPIRED", message="collection process did not finish before lease expiry")
            )
        return stale_ids

    def enabled_endpoints(self, source_slugs: tuple[str, ...] = ()) -> list[dict[str, object]]:
        filters = [public_sources.c.enabled == 1, public_source_endpoints.c.enabled == 1]
        if source_slugs:
            filters.append(public_sources.c.slug.in_(source_slugs))
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    public_source_endpoints,
                    public_sources.c.slug.label("source_slug"),
                    public_sources.c.name.label("source_name"),
                )
                .join(public_sources, public_sources.c.id == public_source_endpoints.c.source_id)
                .where(*filters)
                .order_by(public_sources.c.slug, public_source_endpoints.c.endpoint_key)
            ).mappings().all()
        return [dict(row) for row in rows]

    def start_run(
        self,
        *,
        trigger: str,
        idempotency_key: str,
        business_date: str | None,
        source_slugs: tuple[str, ...] = (),
    ) -> tuple[dict[str, object], bool]:
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        now = self._timestamp()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(public_collection_runs).where(public_collection_runs.c.idempotency_key == idempotency_key)
            ).mappings().one_or_none()
            if existing is not None:
                return dict(existing), True
            active = connection.execute(
                select(public_collection_runs.c.id).where(public_collection_runs.c.status == "running").limit(1)
            ).scalar_one_or_none()
            if active is not None:
                raise ValueError("a public source collection run is already active")
            if source_slugs:
                enabled_slugs = {
                    str(value)
                    for value in connection.execute(
                        select(public_sources.c.slug).where(public_sources.c.enabled == 1, public_sources.c.slug.in_(source_slugs))
                    ).scalars()
                }
                missing = sorted(set(source_slugs) - enabled_slugs)
                if missing:
                    raise ValueError(f"unknown or disabled public sources: {', '.join(missing)}")
            run_id = new_uuid7()
            filters = [public_sources.c.enabled == 1, public_source_endpoints.c.enabled == 1]
            if source_slugs:
                filters.append(public_sources.c.slug.in_(source_slugs))
            planned_count = connection.execute(
                select(func.count())
                .select_from(public_source_endpoints.join(public_sources))
                .where(*filters)
            ).scalar_one()
            if planned_count == 0:
                raise ValueError("no enabled public source endpoints")
            connection.execute(
                public_collection_runs.insert().values(
                    id=run_id,
                    trigger=trigger,
                    business_date=business_date,
                    idempotency_key=idempotency_key,
                    selected_source_slugs_json=json.dumps(source_slugs, separators=(",", ":")),
                    status="running",
                    planned_endpoint_count=planned_count,
                    started_at=now,
                )
            )
            row = connection.execute(select(public_collection_runs).where(public_collection_runs.c.id == run_id)).mappings().one()
            return dict(row), False

    def start_endpoint_run(self, run_id: str, endpoint: dict[str, object]) -> dict[str, object] | None:
        now_dt = self._now().astimezone(UTC)
        now = self._timestamp(now_dt)
        last_fetch = self._parse_timestamp(endpoint.get("last_fetch_started_at"))
        interval = int(endpoint["minimum_interval_seconds"])
        with self.engine.begin() as connection:
            if last_fetch is not None and now_dt < last_fetch + timedelta(seconds=interval):
                connection.execute(
                    public_endpoint_runs.insert().values(
                        id=new_uuid7(), run_id=run_id, endpoint_id=endpoint["id"], status="skipped", started_at=now, finished_at=now,
                        reason_code="SOURCE_INTERVAL_ACTIVE",
                    )
                )
                return None
            endpoint_run_id = new_uuid7()
            connection.execute(
                update(public_source_endpoints)
                .where(public_source_endpoints.c.id == endpoint["id"])
                .values(last_fetch_started_at=now, updated_at=now)
            )
            connection.execute(
                public_endpoint_runs.insert().values(id=endpoint_run_id, run_id=run_id, endpoint_id=endpoint["id"], status="running", started_at=now)
            )
            row = connection.execute(select(public_endpoint_runs).where(public_endpoint_runs.c.id == endpoint_run_id)).mappings().one()
            return dict(row)

    def store_items(self, endpoint_run_id: str, endpoint: dict[str, object], items: Iterable[ListingItem]) -> dict[str, object]:
        now = self._timestamp()
        new_count = updated_count = 0
        article_ids_by_source_item_key: dict[str, str] = {}
        with self.engine.begin() as connection:
            for item in items:
                existing = connection.execute(
                    select(public_source_articles)
                    .where(
                        or_(
                            and_(
                                public_source_articles.c.endpoint_id == endpoint["id"],
                                public_source_articles.c.source_item_key == item.source_item_key,
                            ),
                            and_(
                                public_source_articles.c.source_id == endpoint["source_id"],
                                public_source_articles.c.url == item.url,
                            ),
                        ),
                    )
                    .order_by(public_source_articles.c.id)
                    .limit(1)
                ).mappings().one_or_none()
                published_at = self._timestamp(item.published_at)
                content_hash = self._content_hash(item, published_at)
                is_first_seen = existing is None
                if existing is None:
                    article_id = new_uuid7()
                    connection.execute(
                        public_source_articles.insert().values(
                            id=article_id,
                            source_id=endpoint["source_id"],
                            endpoint_id=endpoint["id"],
                            source_item_key=item.source_item_key,
                            title=item.title,
                            digest=item.digest,
                            digest_source=item.digest_source,
                            url=item.url,
                            cover_url=item.cover_url,
                            published_at=published_at,
                            first_seen_at=now,
                            last_seen_at=now,
                            content_hash=content_hash,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    new_count += 1
                else:
                    article_id = str(existing["id"])
                    values = {"last_seen_at": now}
                    if existing["content_hash"] != content_hash:
                        values.update(
                            title=item.title, digest=item.digest, digest_source=item.digest_source, url=item.url, cover_url=item.cover_url,
                            published_at=published_at, content_hash=content_hash, updated_at=now,
                        )
                        updated_count += 1
                    connection.execute(update(public_source_articles).where(public_source_articles.c.id == article_id).values(**values))
                article_ids_by_source_item_key[item.source_item_key] = article_id
                connection.execute(
                    public_endpoint_run_articles.insert().prefix_with("OR IGNORE").values(
                        endpoint_run_id=endpoint_run_id, article_id=article_id, observed_at=now, is_first_seen=int(is_first_seen)
                    )
                )
        return {
            "new_count": new_count,
            "updated_count": updated_count,
            "article_ids_by_source_item_key": article_ids_by_source_item_key,
        }

    def store_enrichment_attempts(
        self,
        endpoint_run_id: str,
        article_ids_by_source_item_key: dict[str, str],
        attempts: Iterable[DetailEnrichmentAttempt],
    ) -> None:
        now = self._timestamp()
        with self.engine.begin() as connection:
            for attempt in attempts:
                connection.execute(
                    public_article_enrichment_attempts.insert().values(
                        id=new_uuid7(),
                        endpoint_run_id=endpoint_run_id,
                        article_id=article_ids_by_source_item_key.get(attempt.source_item_key),
                        source_item_key=attempt.source_item_key,
                        source_url=attempt.source_url,
                        method=attempt.method,
                        status=attempt.status,
                        duration_ms=attempt.duration_ms,
                        attempted_at=now,
                        reason_code=attempt.reason_code,
                        message=attempt.message,
                    )
                )

    def diagnostics(self, run_id: str | None = None) -> dict[str, object]:
        with self.engine.connect() as connection:
            selected_run_id = run_id
            if selected_run_id is None:
                selected_run_id = connection.execute(
                    select(public_collection_runs.c.id)
                    .order_by(public_collection_runs.c.started_at.desc(), public_collection_runs.c.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
            if selected_run_id is None:
                return {"run": None, "endpoints": []}
            run = connection.execute(
                select(public_collection_runs).where(public_collection_runs.c.id == selected_run_id)
            ).mappings().one_or_none()
            if run is None:
                raise ValueError("public collection run was not found")
            endpoint_rows = connection.execute(
                select(
                    public_endpoint_runs,
                    public_sources.c.slug.label("source_slug"),
                    public_source_endpoints.c.endpoint_key,
                    public_source_endpoints.c.kind,
                )
                .join(public_source_endpoints, public_source_endpoints.c.id == public_endpoint_runs.c.endpoint_id)
                .join(public_sources, public_sources.c.id == public_source_endpoints.c.source_id)
                .where(public_endpoint_runs.c.run_id == selected_run_id)
                .order_by(public_sources.c.slug, public_source_endpoints.c.endpoint_key)
            ).mappings().all()
            attempts = connection.execute(
                select(public_article_enrichment_attempts)
                .where(public_article_enrichment_attempts.c.endpoint_run_id.in_([row["id"] for row in endpoint_rows]))
                .order_by(public_article_enrichment_attempts.c.attempted_at, public_article_enrichment_attempts.c.id)
            ).mappings().all() if endpoint_rows else []
        attempts_by_endpoint: dict[str, list[dict[str, object]]] = {}
        for attempt in attempts:
            attempts_by_endpoint.setdefault(str(attempt["endpoint_run_id"]), []).append(
                {
                    "source_item_key": str(attempt["source_item_key"]),
                    "source_url": str(attempt["source_url"]),
                    "article_id": str(attempt["article_id"]) if attempt["article_id"] else None,
                    "method": str(attempt["method"]),
                    "status": str(attempt["status"]),
                    "duration_ms": int(attempt["duration_ms"]),
                    "reason_code": str(attempt["reason_code"]),
                    "message": str(attempt["message"]),
                }
            )
        return {
            "run": dict(run),
            "endpoints": [
                {
                    "endpoint_id": str(row["endpoint_id"]),
                    "endpoint_run_id": str(row["id"]),
                    "source_slug": str(row["source_slug"]),
                    "endpoint_key": str(row["endpoint_key"]),
                    "kind": str(row["kind"]),
                    "status": str(row["status"]),
                    "fetched_count": int(row["fetched_count"]),
                    "accepted_count": int(row["accepted_count"]),
                    "rejected_count": int(row["rejected_count"]),
                    "new_count": int(row["new_count"]),
                    "updated_count": int(row["updated_count"]),
                    "reason_code": str(row["reason_code"]),
                    "message": str(row["message"]),
                    "rejection_summary": _json_object(row["rejection_summary_json"]),
                    "rejection_samples": _json_list(row["rejection_samples_json"]),
                    "detail_enrichment_attempts": attempts_by_endpoint.get(str(row["id"]), []),
                }
                for row in endpoint_rows
            ],
        }
    def health_summary(
        self,
        run_id: str | None = None,
        *,
        stale_after_hours: int = 48,
        baseline_runs: int = 3,
    ) -> dict[str, object]:
        """Compact, non-sensitive operational outcome for one collection batch."""
        diagnostics = self.diagnostics(run_id)
        run = diagnostics["run"]
        if run is None:
            return {"status": "unknown", "alerts": [{"severity": "error", "code": "NO_COLLECTION_RUN"}], "endpoints": []}
        endpoints = diagnostics["endpoints"]
        alerts: list[dict[str, str]] = []
        for endpoint in endpoints:
            status = str(endpoint["status"])
            quality = self._endpoint_quality(
                str(endpoint["endpoint_id"]),
                str(endpoint["endpoint_run_id"]),
                stale_after_hours=stale_after_hours,
                baseline_runs=baseline_runs,
            )
            endpoint.update(quality)
            if status == "failed":
                alerts.append({"severity": "error", "code": str(endpoint["reason_code"] or "LIST_FETCH_FAILED"), "source_slug": str(endpoint["source_slug"])})
            elif status == "partial":
                alerts.append({"severity": "warning", "code": str(endpoint["reason_code"] or "PARTIAL_LIST_RESULT"), "source_slug": str(endpoint["source_slug"])})
            if status in {"completed", "partial"} and int(endpoint["accepted_count"]) == 0:
                alerts.append({"severity": "warning", "code": "EMPTY_LIST_RESULT", "source_slug": str(endpoint["source_slug"])})
            if int(quality["consecutive_empty_result_count"]) >= 2:
                alerts.append({"severity": "warning", "code": "CONSECUTIVE_EMPTY_LIST_RESULTS", "source_slug": str(endpoint["source_slug"])})
            if quality["is_stale"]:
                alerts.append({"severity": "warning", "code": "SOURCE_STALE", "source_slug": str(endpoint["source_slug"])})
            if quality["volume_anomaly"]:
                alerts.append({"severity": "warning", "code": "LIST_VOLUME_ANOMALY", "source_slug": str(endpoint["source_slug"])})
        if endpoints and all(str(endpoint["status"]) == "skipped" for endpoint in endpoints):
            alerts.append({"severity": "warning", "code": "ALL_ENDPOINTS_SKIPPED"})
        return {
            "run_id": str(run["id"]),
            "business_date": run["business_date"],
            "trigger": str(run["trigger"]),
            "status": str(run["status"]),
            "finished_at": run["finished_at"],
            "planned_endpoint_count": int(run["planned_endpoint_count"]),
            "success_endpoint_count": int(run["success_endpoint_count"]),
            "failed_endpoint_count": int(run["failed_endpoint_count"]),
            "new_article_count": int(run["new_article_count"]),
            "updated_article_count": int(run["updated_article_count"]),
            "alerts": alerts,
            "endpoints": endpoints,
        }

    def _endpoint_quality(
        self,
        endpoint_id: str,
        current_endpoint_run_id: str,
        *,
        stale_after_hours: int,
        baseline_runs: int,
    ) -> dict[str, object]:
        with self.engine.connect() as connection:
            recent = connection.execute(
                select(public_endpoint_runs.c.id, public_endpoint_runs.c.accepted_count, public_endpoint_runs.c.status, public_endpoint_runs.c.finished_at)
                .where(public_endpoint_runs.c.endpoint_id == endpoint_id)
                .order_by(public_endpoint_runs.c.started_at.desc(), public_endpoint_runs.c.id.desc())
                .limit(max(baseline_runs + 2, 5))
            ).mappings().all()
        consecutive_empty = 0
        for row in recent:
            if str(row["status"]) not in {"completed", "partial"}:
                continue
            if int(row["accepted_count"]) == 0:
                consecutive_empty += 1
            else:
                break
        previous_counts = [
            int(row["accepted_count"])
            for row in recent
            if str(row["status"]) in {"completed", "partial"}
            and int(row["accepted_count"]) > 0
            and str(row["id"]) != current_endpoint_run_id
        ][:baseline_runs]
        last_success = next(
            (
                self._parse_timestamp(str(row["finished_at"]))
                for row in recent
                if str(row["status"]) in {"completed", "partial"}
                and int(row["accepted_count"]) > 0
                and row["finished_at"]
            ),
            None,
        )
        baseline = median(previous_counts) if previous_counts else None
        current = next((int(row["accepted_count"]) for row in recent if str(row["id"]) == current_endpoint_run_id), 0)
        return {
            "last_success_at": self._timestamp(last_success) if last_success else None,
            "consecutive_empty_result_count": consecutive_empty,
            "is_stale": last_success is None or self._now().astimezone(UTC) - last_success > timedelta(hours=stale_after_hours),
            "volume_baseline": baseline,
            "volume_anomaly": bool(baseline is not None and baseline >= 5 and current < max(1, int(baseline * 0.2))),
        }

    def finish_endpoint_run(
        self,
        endpoint_run_id: str,
        *,
        status: str,
        fetched_count: int = 0,
        accepted_count: int = 0,
        rejected_count: int = 0,
        new_count: int = 0,
        updated_count: int = 0,
        reason_code: str = "",
        message: str = "",
        rejection_summary: dict[str, int] | None = None,
        rejection_samples: list[dict[str, object]] | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(public_endpoint_runs)
                .where(public_endpoint_runs.c.id == endpoint_run_id)
                .values(
                    status=status, fetched_count=fetched_count, accepted_count=accepted_count, rejected_count=rejected_count,
                    new_count=new_count, updated_count=updated_count, reason_code=reason_code, message=message,
                    rejection_summary_json=json.dumps(rejection_summary or {}, ensure_ascii=False, separators=(",", ":")),
                    rejection_samples_json=json.dumps(rejection_samples or [], ensure_ascii=False, separators=(",", ":")),
                    finished_at=self._timestamp(),
                )
            )

    def finalize_run(self, run_id: str) -> dict[str, object]:
        with self.engine.begin() as connection:
            rows = connection.execute(select(public_endpoint_runs).where(public_endpoint_runs.c.run_id == run_id)).mappings().all()
            statuses = [str(row["status"]) for row in rows]
            failed = sum(status == "failed" for status in statuses)
            successful = sum(status == "completed" for status in statuses)
            partial = any(status in {"partial", "skipped"} for status in statuses)
            status = "failed" if rows and failed == len(rows) else "partial" if failed or partial else "completed"
            values = {
                "status": status,
                "success_endpoint_count": successful,
                "failed_endpoint_count": failed,
                "new_article_count": sum(int(row["new_count"]) for row in rows),
                "updated_article_count": sum(int(row["updated_count"]) for row in rows),
                "finished_at": self._timestamp(),
            }
            connection.execute(update(public_collection_runs).where(public_collection_runs.c.id == run_id).values(**values))
            row = connection.execute(select(public_collection_runs).where(public_collection_runs.c.id == run_id)).mappings().one()
            return dict(row)

    @staticmethod
    def _content_hash(item: ListingItem, published_at: str) -> str:
        payload = "\x1f".join((item.title, item.digest, item.digest_source, item.url, item.cover_url, published_at))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None

    def _timestamp(self, value: datetime | None = None) -> str:
        return (value or self._now()).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: object) -> list[object]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
