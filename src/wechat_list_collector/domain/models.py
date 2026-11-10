from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ArticleInput:
    source_article_id: str
    title: str
    digest: str
    url: str
    cover_url: str
    published_at: datetime
    source_payload: dict[str, object]


@dataclass(frozen=True)
class SubmittedCommand:
    command_id: str
    run_id: str
    reused: bool


@dataclass(frozen=True)
class PageStoreResult:
    fetched_count: int
    new_count: int
    updated_count: int
    observed_count: int


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    status: str
    planned_count: int
    success_count: int
    failed_count: int
    not_started_count: int
    coverage_ratio: float


@dataclass(frozen=True)
class CommandItemResult:
    account_id: str
    outcome: str
    job_id: str | None = None
    active_run_id: str | None = None
    reason_code: str = ""


@dataclass(frozen=True)
class AccountCollectionJob:
    id: str
    account_id: str
    command_id: str
    status: str
    priority: int
    fakeid_ciphertext: bytes
    stop_watermark_source_article_id: str


@dataclass(frozen=True)
class RequestIntent:
    command_id: str
    operation: str
    job_id: str | None = None
    run_id: str | None = None
    account_id: str | None = None
    page_number: int | None = None
    begin: int | None = None
    count: int | None = None


@dataclass(frozen=True)
class RequestPermit:
    request_id: str
    scheduled_at: datetime
    gate_wait_ms: int
