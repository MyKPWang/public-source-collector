from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import SystemRandom

from sqlalchemy import Engine, func, select, update

from ..domain.errors import CooldownActive, RequestBudgetExhausted
from ..domain.ids import new_uuid7
from ..domain.models import RequestIntent, RequestPermit
from .db.schema import request_channel_state, wechat_requests

MP_ADMIN_CHANNEL = "mp_admin"
WEREAD_READER_CHANNEL = "weread_reader"
# 摘要补全（/review/single 的 mpInfo.content）与列表请求共用 weread_reader
# 通道的节奏，但单独计数、单独 per-command 上限和 24 小时滚动预算。
WEREAD_SUMMARY_OPERATION = "article_summary"


@dataclass(frozen=True)
class OperationBudget:
    """某个 operation 独立的请求预算。"""

    rolling_window_limit: int
    batch_limit: int


class RequestGate:
    """Durable permit allocator scoped to one request channel.

    Each channel owns its own cooldown, pacing interval and rolling/batch
    budget.  The gate used to share a single ``request_control_state`` row
    across every route, so a penalty earned by the abandoned WeChat-backend
    article route locked the WeRead reader out for hours.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        channel: str = MP_ADMIN_CHANNEL,
        minimum_interval_seconds: int = 240,
        jitter_seconds: Callable[[], int] | None = None,
        rolling_window_limit: int = 60,
        batch_limit: int = 45,
        cooldown_seconds: int = 21_600,
        max_budget_wait_seconds: int = 21_600,
        operation_budgets: Mapping[str, OperationBudget] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if minimum_interval_seconds < 0 or rolling_window_limit < 1 or batch_limit < 1 or cooldown_seconds < 1:
            raise ValueError("request gate limits must be positive")
        if not channel:
            raise ValueError("request gate channel must not be empty")
        self.engine = engine
        self.channel = channel
        self.minimum_interval_seconds = minimum_interval_seconds
        self.jitter_seconds = jitter_seconds or (lambda: SystemRandom().randint(30, 90))
        self.rolling_window_limit = rolling_window_limit
        self.batch_limit = batch_limit
        self.cooldown_seconds = cooldown_seconds
        self.max_budget_wait_seconds = max_budget_wait_seconds
        # 带独立预算的 operation 不再计入通道级预算，两者互不挤占。
        self.operation_budgets = dict(operation_budgets or {})
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or __import__("time").sleep

    def acquire(
        self,
        intent: RequestIntent,
        *,
        operation_minimum_interval_seconds: int | None = None,
        updates_global_interval: bool = True,
    ) -> RequestPermit:
        if operation_minimum_interval_seconds is not None and operation_minimum_interval_seconds < 0:
            raise ValueError("operation_minimum_interval_seconds must be non-negative")
        now = self._current_time()
        request_id = new_uuid7()
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                state = self._load_state(connection)
                cooldown_until = self._parse_timestamp(state["cooldown_until"])
                if cooldown_until is not None and cooldown_until > now:
                    raise CooldownActive(self._timestamp(cooldown_until))

                rolling_start = now - timedelta(hours=24)
                budget = self.operation_budgets.get(intent.operation)
                if budget is None:
                    # 通道级预算：排除已经单独计预算的 operation。
                    scope = (wechat_requests.c.channel == self.channel,)
                    if self.operation_budgets:
                        scope += (wechat_requests.c.operation.not_in(tuple(self.operation_budgets)),)
                    rolling_limit = self.rolling_window_limit
                    batch_limit = self.batch_limit
                else:
                    scope = (
                        wechat_requests.c.channel == self.channel,
                        wechat_requests.c.operation == intent.operation,
                    )
                    rolling_limit = budget.rolling_window_limit
                    batch_limit = budget.batch_limit
                reserved_count = int(
                    connection.execute(
                        select(func.count())
                        .select_from(wechat_requests)
                        .where(
                            *scope,
                            wechat_requests.c.planned_at >= self._timestamp(rolling_start),
                        )
                    ).scalar_one()
                )
                batch_count = int(
                    connection.execute(
                        select(func.count())
                        .select_from(wechat_requests)
                        .where(
                            *scope,
                            wechat_requests.c.command_id == intent.command_id,
                        )
                    ).scalar_one()
                )
                if batch_count >= batch_limit:
                    raise RequestBudgetExhausted("collection batch request limit is exhausted")
                scheduled_at = now
                if reserved_count >= rolling_limit:
                    earliest = connection.execute(
                        select(func.min(wechat_requests.c.planned_at)).where(
                            *scope,
                            wechat_requests.c.planned_at >= self._timestamp(rolling_start),
                        )
                    ).scalar_one()
                    earliest_time = self._parse_timestamp(earliest)
                    if earliest_time is None:
                        raise RequestBudgetExhausted("rolling request budget is exhausted")
                    scheduled_at = max(scheduled_at, earliest_time + timedelta(hours=24))

                if updates_global_interval:
                    last_started = self._parse_timestamp(state["last_request_started_at"])
                    if last_started is not None:
                        scheduled_at = max(scheduled_at, last_started + timedelta(seconds=self.minimum_interval_seconds))
                    jitter = self.jitter_seconds()
                    if jitter < 0:
                        raise ValueError("jitter_seconds cannot return a negative value")
                    scheduled_at += timedelta(seconds=jitter)
                if operation_minimum_interval_seconds is not None:
                    latest_operation_request = connection.execute(
                        select(func.max(wechat_requests.c.planned_at)).where(
                            wechat_requests.c.channel == self.channel,
                            wechat_requests.c.operation == intent.operation,
                        )
                    ).scalar_one()
                    latest_operation_time = self._parse_timestamp(latest_operation_request)
                    if latest_operation_time is not None:
                        scheduled_at = max(
                            scheduled_at,
                            latest_operation_time + timedelta(seconds=operation_minimum_interval_seconds),
                        )
                wait_seconds = max(0.0, (scheduled_at - now).total_seconds())
                if reserved_count >= rolling_limit and wait_seconds > self.max_budget_wait_seconds:
                    raise RequestBudgetExhausted("rolling request budget is exhausted")

                connection.execute(
                    wechat_requests.insert().values(
                        id=request_id,
                        command_id=intent.command_id,
                        job_id=intent.job_id,
                        run_id=intent.run_id,
                        account_id=intent.account_id,
                        channel=self.channel,
                        operation=intent.operation,
                        page_number=intent.page_number,
                        begin=intent.begin,
                        count=intent.count,
                        status="waiting",
                        gate_wait_ms=round(wait_seconds * 1000),
                        planned_at=self._timestamp(scheduled_at),
                    )
                )
                if updates_global_interval:
                    connection.execute(
                        update(request_channel_state)
                        .where(request_channel_state.c.channel == self.channel)
                        .values(last_request_started_at=self._timestamp(scheduled_at), version=int(state["version"]) + 1)
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        if wait_seconds:
            self._sleep(wait_seconds)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(wechat_requests)
                .where(wechat_requests.c.id == request_id, wechat_requests.c.status == "waiting")
                .values(status="sent", sent_at=self._timestamp(self._current_time()))
            )
            if result.rowcount != 1:
                raise RuntimeError("request permit was not waiting")
        return RequestPermit(request_id=request_id, scheduled_at=scheduled_at, gate_wait_ms=round(wait_seconds * 1000))

    def succeed(
        self,
        permit: RequestPermit,
        *,
        publish_record_count: int,
        article_count: int,
        duration_ms: int,
        response_schema: str = "appmsgpublish-v1",
    ) -> None:
        self._finish_request(
            permit.request_id,
            status="succeeded",
            publish_record_count=publish_record_count,
            article_count=article_count,
            duration_ms=duration_ms,
            response_schema=response_schema,
        )

    def fail(self, permit: RequestPermit, *, status: str, reason_code: str, wechat_ret: int | None = None) -> None:
        valid = {"rate_limited", "session_expired", "network_failed", "protocol_changed", "access_blocked", "unknown"}
        if status not in valid:
            raise ValueError("invalid request terminal status")
        now = self._current_time()
        with self.engine.begin() as connection:
            connection.execute(
                update(wechat_requests)
                .where(wechat_requests.c.id == permit.request_id, wechat_requests.c.status == "sent")
                .values(
                    status=status,
                    finished_at=self._timestamp(now),
                    wechat_ret=wechat_ret,
                    reason_code=reason_code,
                )
            )
            if status == "rate_limited":
                connection.execute(
                    update(request_channel_state)
                    .where(request_channel_state.c.channel == self.channel)
                    .values(
                        cooldown_until=self._timestamp(now + timedelta(seconds=self.cooldown_seconds)),
                        last_rate_limit_at=self._timestamp(now),
                        consecutive_rate_limit_count=request_channel_state.c.consecutive_rate_limit_count + 1,
                        version=request_channel_state.c.version + 1,
                    )
                )

    def recover_unknown_requests(self) -> int:
        """Conservatively mark this channel's in-flight requests unknown."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(wechat_requests)
                .where(wechat_requests.c.status == "sent", wechat_requests.c.channel == self.channel)
                .values(status="unknown", finished_at=self._timestamp(self._current_time()), reason_code="WORKER_RECOVERY")
            )
            return max(result.rowcount, 0)

    def _load_state(self, connection):
        state = connection.execute(
            select(request_channel_state).where(request_channel_state.c.channel == self.channel)
        ).mappings().one_or_none()
        if state is None:
            connection.execute(
                request_channel_state.insert().values(
                    channel=self.channel,
                    rolling_window_limit=self.rolling_window_limit,
                    consecutive_rate_limit_count=0,
                    version=0,
                )
            )
            state = connection.execute(
                select(request_channel_state).where(request_channel_state.c.channel == self.channel)
            ).mappings().one()
        return state

    def _finish_request(
        self,
        request_id: str,
        *,
        status: str,
        publish_record_count: int,
        article_count: int,
        duration_ms: int,
        response_schema: str,
    ) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(wechat_requests)
                .where(wechat_requests.c.id == request_id, wechat_requests.c.status == "sent")
                .values(
                    status=status,
                    publish_record_count=publish_record_count,
                    article_count=article_count,
                    duration_ms=duration_ms,
                    response_schema=response_schema,
                    finished_at=self._timestamp(self._current_time()),
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("request was not in sent state")

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("request gate clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
