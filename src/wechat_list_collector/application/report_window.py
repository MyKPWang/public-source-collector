"""日报窗口：上海时区的"昨天 00:00 ~ 明天 00:00"。

采集端的摘要补全和 pipeline 的 ``filter_recent`` 必须用同一个窗口，否则
会出现"补了摘要但条目被判为 out_of_report_window"这类错配。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def report_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """返回日报窗口的 ``(start, end)``，两端都是带时区的 datetime。"""
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(SHANGHAI)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today + timedelta(days=1)
