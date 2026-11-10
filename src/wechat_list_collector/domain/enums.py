from __future__ import annotations

from enum import StrEnum


class CollectionCommandStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CollectionRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SESSION_EXPIRED = "session_expired"
    PROTOCOL_CHANGED = "protocol_changed"
    ACCESS_BLOCKED = "access_blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    MISSED = "missed"


class RunAccountStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    EMPTY = "empty"
    PAGE_LIMIT_REACHED = "page_limit_reached"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    SESSION_EXPIRED = "session_expired"
    PROTOCOL_CHANGED = "protocol_changed"
    ACCESS_BLOCKED = "access_blocked"
    NOT_STARTED = "not_started"


SUCCESSFUL_ACCOUNT_STATUSES = frozenset({RunAccountStatus.COMPLETED, RunAccountStatus.EMPTY})
GLOBAL_FAILURE_ACCOUNT_STATUSES = (
    (RunAccountStatus.RATE_LIMITED, CollectionRunStatus.RATE_LIMITED),
    (RunAccountStatus.SESSION_EXPIRED, CollectionRunStatus.SESSION_EXPIRED),
    (RunAccountStatus.PROTOCOL_CHANGED, CollectionRunStatus.PROTOCOL_CHANGED),
    (RunAccountStatus.ACCESS_BLOCKED, CollectionRunStatus.ACCESS_BLOCKED),
)
