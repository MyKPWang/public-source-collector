from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, and_, or_, select

from ..infrastructure.db.schema import articles, collection_run_accounts, collection_runs, official_accounts


class InvalidCursor(ValueError):
    pass


class ReadModel:
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
    ) -> tuple[list[dict[str, object]], str | None]:
        if published_from.tzinfo is None or published_before.tzinfo is None:
            raise ValueError("published range must include a timezone")
        if published_from >= published_before:
            raise ValueError("published_from must be before published_before")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        lower = self._timestamp(published_from)
        upper = self._timestamp(published_before)
        statement = (
            select(
                articles.c.id,
                articles.c.source_article_id,
                articles.c.title,
                articles.c.digest,
                articles.c.url,
                articles.c.cover_url,
                articles.c.published_at,
                articles.c.first_seen_at,
                official_accounts.c.id.label("account_id"),
                official_accounts.c.name.label("account_name"),
            )
            .join(official_accounts, official_accounts.c.id == articles.c.account_id)
            .where(
                official_accounts.c.archived_at.is_(None),
                articles.c.published_at >= lower,
                articles.c.published_at < upper,
            )
        )
        if cursor:
            published_at, article_id = self._decode_cursor(cursor)
            statement = statement.where(
                or_(
                    articles.c.published_at < published_at,
                    and_(articles.c.published_at == published_at, articles.c.id < article_id),
                )
            )
        with self.engine.connect() as connection:
            rows = connection.execute(statement.order_by(articles.c.published_at.desc(), articles.c.id.desc()).limit(limit + 1)).mappings().all()
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = None
        if has_more and visible:
            tail = visible[-1]
            next_cursor = self._encode_cursor(str(tail["published_at"]), str(tail["id"]))
        return [
            {
                "id": str(row["id"]),
                "source_article_id": str(row["source_article_id"]),
                "account": {"id": str(row["account_id"]), "name": str(row["account_name"])},
                "title": str(row["title"]),
                "digest": str(row["digest"]),
                "url": str(row["url"]),
                "cover_url": str(row["cover_url"]),
                "published_at": str(row["published_at"]),
                "first_seen_at": str(row["first_seen_at"]),
            }
            for row in visible
        ], next_cursor

    def latest_scheduled_batch(self, freshness_hours: int = 36) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            run = connection.execute(
                select(collection_runs)
                .where(collection_runs.c.trigger == "scheduled")
                .order_by(collection_runs.c.finished_at.desc(), collection_runs.c.id.desc())
                .limit(1)
            ).mappings().one_or_none()
            if run is None:
                return None
            missing_rows = connection.execute(
                select(official_accounts.c.id, official_accounts.c.name, collection_run_accounts.c.status, collection_run_accounts.c.reason_code)
                .join(official_accounts, official_accounts.c.id == collection_run_accounts.c.account_id)
                .where(
                    collection_run_accounts.c.run_id == run["id"],
                    collection_run_accounts.c.status.not_in(("completed", "empty")),
                )
                .order_by(collection_run_accounts.c.plan_position)
            ).mappings().all()
        finished_at = self._parse_timestamp(run["finished_at"])
        now = self._now().astimezone(UTC)
        is_fresh = bool(finished_at and now - finished_at <= timedelta(hours=freshness_hours))
        return {
            "id": str(run["id"]),
            "status": str(run["status"]),
            "trigger": str(run["trigger"]),
            "finished_at": run["finished_at"],
            "planned_accounts": int(run["planned_count"]),
            "successful_accounts": int(run["success_count"]),
            "coverage_ratio": float(run["coverage_ratio"]),
            "is_fresh": is_fresh,
            "missing_accounts": [
                {"id": str(row["id"]), "name": str(row["name"]), "status": str(row["status"]), "reason_code": str(row["reason_code"])}
                for row in missing_rows
            ],
        }

    def pipeline_accounts(self) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(official_accounts)
                .where(official_accounts.c.archived_at.is_(None), official_accounts.c.enabled == 1)
                .order_by(official_accounts.c.display_order, official_accounts.c.name)
            ).mappings()
            return [
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "last_success_at": row["last_success_at"],
                    "consecutive_failure_count": int(row["consecutive_failure_count"]),
                }
                for row in rows
            ]

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
            published_at = str(payload["p"])
            article_id = str(payload["i"])
            if not published_at or not article_id:
                raise ValueError
            return published_at, article_id
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise InvalidCursor("invalid cursor") from error
