from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, and_, or_, select

from ..infrastructure.db.schema import public_collection_runs, public_endpoint_runs, public_source_articles, public_source_endpoints, public_sources
from .read_models import InvalidCursor


class PublicReadModel:
    """Pipeline projection over metadata collected from public list endpoints."""

    def __init__(self, engine: Engine, *, now: callable | None = None):
        self.engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def pipeline_articles(
        self,
        *,
        published_from: datetime,
        published_before: datetime,
        limit: int,
        cursor: str | None = None,
        source_slugs: tuple[str, ...] = (),
    ) -> tuple[list[dict[str, object]], str | None]:
        if published_from.tzinfo is None or published_before.tzinfo is None or published_from >= published_before:
            raise ValueError("published range must include a valid timezone-aware interval")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        lower, upper = self._timestamp(published_from), self._timestamp(published_before)
        article_id_column = public_source_articles.c.id
        published_column = public_source_articles.c.published_at
        statement = (
            select(
                article_id_column.label("id"),
                public_source_articles.c.source_item_key,
                public_source_articles.c.title,
                public_source_articles.c.digest,
                public_source_articles.c.url,
                public_source_articles.c.cover_url,
                published_column,
                public_source_articles.c.first_seen_at,
                public_sources.c.id.label("source_id"),
                public_sources.c.slug.label("source_slug"),
                public_sources.c.name.label("source_name"),
            )
            .join(public_sources, public_sources.c.id == public_source_articles.c.source_id)
            .where(public_sources.c.enabled == 1, published_column >= lower, published_column < upper)
        )
        if source_slugs:
            statement = statement.where(public_sources.c.slug.in_(source_slugs))
        if cursor:
            cursor_published_at, cursor_article_id = self._decode_cursor(cursor)
            statement = statement.where(
                or_(
                    published_column < cursor_published_at,
                    and_(published_column == cursor_published_at, article_id_column < cursor_article_id),
                )
            )
        with self.engine.connect() as connection:
            rows = connection.execute(statement.order_by(published_column.desc(), article_id_column.desc()).limit(limit + 1)).mappings().all()
        visible, has_more = rows[:limit], len(rows) > limit
        next_cursor = self._encode_cursor(str(visible[-1]["published_at"]), str(visible[-1]["id"])) if has_more and visible else None
        return [
            {
                "id": str(row["id"]),
                "source_article_id": str(row["source_item_key"]),
                "source": {"id": str(row["source_id"]), "slug": str(row["source_slug"]), "name": str(row["source_name"])},
                # Kept temporarily so existing Pipeline consumers that only use
                # account.name continue to work while moving to source.name.
                "account": {"id": str(row["source_id"]), "name": str(row["source_name"])},
                "title": str(row["title"]),
                "digest": str(row["digest"]),
                "url": str(row["url"]),
                "cover_url": str(row["cover_url"]),
                "published_at": str(row["published_at"]),
                "first_seen_at": str(row["first_seen_at"]),
            }
            for row in visible
        ], next_cursor

    def collection_batch(self, batch_id: str | None = None, freshness_hours: int = 36) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            statement = select(public_collection_runs).where(public_collection_runs.c.trigger.in_(("scheduled", "pipeline")))
            if batch_id:
                statement = statement.where(public_collection_runs.c.id == batch_id)
            else:
                statement = statement.order_by(public_collection_runs.c.finished_at.desc(), public_collection_runs.c.id.desc()).limit(1)
            run = connection.execute(statement).mappings().one_or_none()
            if run is None:
                return None
            endpoint_rows = connection.execute(
                select(
                    public_endpoint_runs.c.status,
                    public_endpoint_runs.c.reason_code,
                    public_sources.c.slug.label("source_slug"),
                    public_sources.c.name.label("source_name"),
                )
                .select_from(
                    public_endpoint_runs
                    .join(public_source_endpoints, public_source_endpoints.c.id == public_endpoint_runs.c.endpoint_id)
                    .join(public_sources, public_sources.c.id == public_source_endpoints.c.source_id)
                )
                .where(public_endpoint_runs.c.run_id == run["id"])
            ).mappings().all()
        finished_at = self._parse_timestamp(run["finished_at"])
        is_fresh = bool(finished_at and self._now().astimezone(UTC) - finished_at <= timedelta(hours=freshness_hours))
        return {
            "id": str(run["id"]),
            "status": str(run["status"]),
            "trigger": str(run["trigger"]),
            "selected_source_slugs": json.loads(str(run["selected_source_slugs_json"])),
            "finished_at": run["finished_at"],
            "planned_sources": int(run["planned_endpoint_count"]),
            "successful_sources": int(run["success_endpoint_count"]),
            "new_article_count": int(run["new_article_count"]),
            "is_fresh": is_fresh,
            "degraded_endpoints": [
                {
                    "status": str(row["status"]),
                    "reason_code": str(row["reason_code"]),
                    "source_slug": str(row["source_slug"]),
                    "source_name": str(row["source_name"]),
                }
                for row in endpoint_rows
                if row["status"] not in {"completed"}
            ],
        }

    def latest_scheduled_batch(self, freshness_hours: int = 36) -> dict[str, object] | None:
        return self.collection_batch(freshness_hours=freshness_hours)

    def pipeline_accounts(self) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(public_sources).where(public_sources.c.enabled == 1).order_by(public_sources.c.name)).mappings().all()
        return [{"id": str(row["id"]), "name": str(row["name"]), "slug": str(row["slug"])} for row in rows]

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

    @staticmethod
    def _encode_cursor(published_at: str, article_id: str) -> str:
        raw = json.dumps({"p": published_at, "i": article_id}, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw)
            published_at, article_id = str(payload["p"]), str(payload["i"])
            if not published_at or not article_id:
                raise ValueError
            return published_at, article_id
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise InvalidCursor("invalid cursor") from error
