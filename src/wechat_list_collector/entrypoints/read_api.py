from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Callable

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..application.read_models import InvalidCursor, ReadModel


class ApiError(Exception):
    def __init__(self, code: str, message: str, status_code: int, details: dict[str, object] | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class PipelineRefreshRequest(BaseModel):
    source_slugs: list[str] = Field(default_factory=list, max_length=100)
    idempotency_key: str | None = Field(default=None, max_length=160)


def create_read_api(
    read_model: ReadModel,
    *,
    bearer_token: str,
    schema_version: str = "1.0",
    refresh_public_sources: Callable[[tuple[str, ...], str | None], dict[str, object]] | None = None,
    wechat_read_model: ReadModel | None = None,
) -> FastAPI:
    app = FastAPI(title="Public Source Collector Read API", docs_url=None, redoc_url=None)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, error: ApiError) -> JSONResponse:
        request_id = request.headers.get("X-Request-ID", "")
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message, "request_id": request_id, "details": error.details}},
        )

    def require_pipeline_token(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {bearer_token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise ApiError("UNAUTHORIZED", "缺少或无效的访问凭据", 401)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        try:
            read_model.pipeline_accounts()
        except Exception as error:
            raise ApiError("DATABASE_UNAVAILABLE", "数据库不可用", 503) from error
        return {"status": "ready"}

    @app.get("/api/v1/pipeline/articles", dependencies=[Depends(require_pipeline_token)])
    def articles_endpoint(
        published_from: str,
        published_before: str,
        limit: int = 100,
        cursor: str | None = None,
        batch_id: str | None = None,
        source_slugs: list[str] | None = None,
    ) -> dict[str, object]:
        try:
            batch_reader = getattr(read_model, "collection_batch", None)
            if callable(batch_reader):
                batch = batch_reader(batch_id)
            else:
                batch = read_model.latest_scheduled_batch()
            if batch_id and batch is None:
                raise ApiError("BATCH_NOT_FOUND", "指定采集批次不存在或不属于 Pipeline", 404)
            if batch is not None and batch["status"] == "running":
                raise ApiError("BATCH_NOT_READY", "指定采集批次尚未完成", 409)
            start = datetime.fromisoformat(published_from.replace("Z", "+00:00")).astimezone(UTC)
            end = datetime.fromisoformat(published_before.replace("Z", "+00:00")).astimezone(UTC)
            selected_source_slugs = tuple(sorted({slug.strip().lower() for slug in source_slugs or [] if slug.strip()}))
            query = {
                "published_from": start,
                "published_before": end,
                "limit": limit,
                "cursor": cursor,
            }
            if selected_source_slugs:
                query["source_slugs"] = selected_source_slugs
            articles, next_cursor = read_model.pipeline_articles(**query)
        except InvalidCursor as error:
            raise ApiError("INVALID_CURSOR", "分页游标无效", 400) from error
        except ValueError as error:
            raise ApiError("INVALID_ARGUMENT", "查询时间范围或分页参数无效", 400) from error
        return {
            "schema_version": schema_version,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "batch": batch,
            "articles": articles,
            "page": {"next_cursor": next_cursor, "has_more": next_cursor is not None},
        }

    @app.post("/api/v1/pipeline/refresh", dependencies=[Depends(require_pipeline_token)])
    def refresh_endpoint(request: PipelineRefreshRequest) -> dict[str, object]:
        if refresh_public_sources is None:
            raise ApiError("REFRESH_UNAVAILABLE", "当前服务未配置公共资讯采集能力", 503)
        source_slugs = tuple(sorted({slug.strip().lower() for slug in request.source_slugs if slug.strip()}))
        try:
            return refresh_public_sources(source_slugs, request.idempotency_key)
        except ValueError as error:
            raise ApiError("INVALID_ARGUMENT", "采集来源或幂等键无效", 400) from error
        except Exception as error:
            raise ApiError("REFRESH_FAILED", "公共资讯采集执行失败", 502) from error

    @app.get("/api/v1/pipeline/batches/latest", dependencies=[Depends(require_pipeline_token)])
    def latest_batch_endpoint() -> dict[str, object]:
        batch = read_model.latest_scheduled_batch()
        if batch is None:
            raise ApiError("BATCH_NOT_FOUND", "尚无定时采集批次", 404)
        return batch

    @app.get("/api/v1/pipeline/accounts", dependencies=[Depends(require_pipeline_token)])
    def accounts_endpoint() -> dict[str, object]:
        return {"accounts": read_model.pipeline_accounts()}

    @app.get("/api/v1/pipeline/wechat/articles", dependencies=[Depends(require_pipeline_token)])
    def wechat_articles_endpoint(
        published_from: str,
        published_before: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """微信公众号文章（微信读书通道抓取，落库在 articles 表）。"""
        if wechat_read_model is None:
            raise ApiError("WECHAT_SOURCE_UNAVAILABLE", "当前服务未配置微信公众号文章读取能力", 503)
        try:
            start = datetime.fromisoformat(published_from.replace("Z", "+00:00")).astimezone(UTC)
            end = datetime.fromisoformat(published_before.replace("Z", "+00:00")).astimezone(UTC)
            articles, next_cursor = wechat_read_model.pipeline_articles(
                published_from=start,
                published_before=end,
                limit=limit,
                cursor=cursor,
            )
        except InvalidCursor as error:
            raise ApiError("INVALID_CURSOR", "分页游标无效", 400) from error
        except ValueError as error:
            raise ApiError("INVALID_ARGUMENT", "查询时间范围或分页参数无效", 400) from error
        return {
            "schema_version": schema_version,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "articles": articles,
            "page": {"next_cursor": next_cursor, "has_more": next_cursor is not None},
        }

    return app
