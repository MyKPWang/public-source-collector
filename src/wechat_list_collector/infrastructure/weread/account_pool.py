"""微信读书账号池：多账号轮换 + 频控自动冷却。

背景
----
微信读书对 ``/mp/chapters``、``/mp/cover`` 这类推文接口有**账号级**配额。
连续密集调用会触发 ``errcode=-2014``（"请求频率过高"），实测该状态下
``mobileSync`` / ``shelf/sync`` 仍返回 200，但所有推文接口一律 499，
单账号无法靠等待自愈到秒级可用。

参考实现 ``wechat-mp-tools`` 的做法是维护一个账号池：
某个账号遇到频控就把它标记为 cooldown（30 秒），换下一个账号继续。

本模块把同样的策略搬到本机：

* 凭据以列表形式存在 vault 里（``weread_accounts.enc``），每个元素是一份
  微信读书凭据；
* 取号时跳过处于冷却期的账号；
* 遇到 ``-2014`` 就把当前账号冷却并立即换号重试；
* 全部账号都在冷却时，抛 ``WeReadAllAccountsCooling``，由上层决定何时重试。

这样单账号被限流时，整个采集不会中断，而是退到下一个账号。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .weread_client import (
    WeReadArticle,
    WeReadAuthExpired,
    WeReadClient,
    WeReadRateLimited,
    WeReadSessionInactive,
)


class WeReadAllAccountsCooling(RuntimeError):
    """账号池中所有账号都在冷却窗口内。"""


@dataclass
class PoolAccount:
    """账号池中的一个微信读书账号及其运行时状态。"""

    credentials: dict[str, Any]
    label: str = ""
    cooldown_until: float = 0.0
    last_error: str = ""

    @property
    def vid(self) -> str:
        return str(self.credentials.get("vid", ""))

    def is_cooling(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.cooldown_until

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "vid": self.vid,
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
        }


@dataclass
class PooledWeReadClient:
    """带账号轮换的微信读书客户端。

    对外暴露与 :class:`WeReadClient` 相同的 ``fetch_articles`` /
    ``fetch_cover`` / ``fetch_detail`` 接口，内部负责选号、冷却与重试。
    """

    accounts: list[PoolAccount]
    timeout_seconds: int = 20
    # 频控冷却时长。参考实现的"防水墙"冷却是 300 秒；
    # 30 秒太短，会让同一个账号在被限流期间反复重试、不断延长限流窗口。
    cooldown_seconds: float = 300.0
    max_account_attempts: int = 0  # 0 表示"每个账号都试一遍"
    # 续期回调：收到新的 accessToken / skey 时调用，用于写回 vault。
    on_refresh: Callable[[dict[str, Any]], None] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_account_attempts <= 0:
            self.max_account_attempts = max(len(self.accounts), 1)

    # ---------- 账号池管理 ----------

    @classmethod
    def from_credentials(cls, credentials_list: list[dict[str, Any]], **kwargs: Any) -> "PooledWeReadClient":
        accounts = [
            PoolAccount(credentials=dict(item), label=str(item.get("nickname") or item.get("vid", "")))
            for item in credentials_list
            if item and item.get("vid")
        ]
        return cls(accounts=accounts, **kwargs)

    def active_accounts(self) -> list[PoolAccount]:
        now = time.time()
        with self._lock:
            return [account for account in self.accounts if not account.is_cooling(now)]

    def _mark_cooling(self, account: PoolAccount, reason: str) -> None:
        with self._lock:
            account.cooldown_until = time.time() + self.cooldown_seconds
            account.last_error = reason

    def _clear_cooling(self, account: PoolAccount) -> None:
        with self._lock:
            account.cooldown_until = 0.0
            account.last_error = ""

    def _client_for(self, account: PoolAccount) -> WeReadClient:
        def _on_refresh(refreshed: dict[str, Any]) -> None:
            # 续期后的凭据写回账号对象，再由上层持久化到 vault。
            account.credentials = dict(refreshed)
            if self.on_refresh is not None:
                try:
                    self.on_refresh(refreshed)
                except Exception:  # noqa: BLE001 - 持久化失败不应中断抓取
                    pass

        return WeReadClient(
            account.credentials,
            timeout_seconds=self.timeout_seconds,
            on_refresh=_on_refresh,
        )

    def _rotate(self, failed: PoolAccount | None = None) -> list[PoolAccount]:
        """返回可尝试的账号列表：冷却中的排在最后。"""
        now = time.time()
        with self._lock:
            ready = [account for account in self.accounts if not account.is_cooling(now)]
            cooling = [account for account in self.accounts if account.is_cooling(now)]
        if failed is not None:
            ready = [account for account in ready if account is not failed]
        return ready + cooling

    # ---------- 抓取（带轮换） ----------

    def _run_with_rotation(self, operation, description: str):
        """执行一次请求，频控时换号重试。"""
        attempts = 0
        last_rate_limited = False
        for account in self._rotate():
            if attempts >= self.max_account_attempts:
                break
            attempts += 1
            if account.is_cooling():
                continue
            client = self._client_for(account)
            try:
                result = operation(client)
            except WeReadRateLimited as error:
                self._mark_cooling(account, f"-2014 {description}")
                last_rate_limited = True
                continue
            except WeReadSessionInactive:
                # 会话休眠：先尝试重新激活，同账号再试一次
                client.activate()
                time.sleep(1.0)
                try:
                    result = operation(client)
                except WeReadRateLimited as error:
                    self._mark_cooling(account, f"-2014 {description}")
                    last_rate_limited = True
                    continue
                except WeReadSessionInactive:
                    self._mark_cooling(account, f"-2041 {description}")
                    continue
                except WeReadAuthExpired:
                    self._mark_cooling(account, "401 credentials expired")
                    continue
            except WeReadAuthExpired:
                self._mark_cooling(account, "401 credentials expired")
                continue
            else:
                self._clear_cooling(account)
                return result
        if last_rate_limited:
            raise WeReadRateLimited("微信读书全部账号都被频控 (-2014)")
        raise WeReadAllAccountsCooling("微信读书账号池中暂无可用账号")

    def fetch_articles(
        self,
        fakeid: str,
        *,
        begin: int = 0,
        count: int = 20,
        account_name: str = "",
    ) -> tuple[list[WeReadArticle], int]:
        return self._run_with_rotation(
            lambda client: client.fetch_articles(
                fakeid, begin=begin, count=count, account_name=account_name
            ),
            f"mp/chapters {fakeid[:12]}",
        )

    def fetch_cover(self, fakeid: str, *, account_name: str = "") -> WeReadArticle | None:
        return self._run_with_rotation(
            lambda client: client.fetch_cover(fakeid, account_name=account_name),
            f"mp/cover {fakeid[:12]}",
        )

    def fetch_detail(self, review_id: str) -> dict[str, Any]:
        return self._run_with_rotation(
            lambda client: client.fetch_detail(review_id),
            f"review/single {review_id[-12:]}",
        )

    def fetch_summary(self, review_id: str) -> str:
        return self._run_with_rotation(
            lambda client: client.fetch_summary(review_id),
            f"review/single {review_id[-12:]}",
        )

    def activate(self) -> bool:
        for account in self._rotate():
            if account.is_cooling():
                continue
            try:
                if self._client_for(account).activate():
                    self._clear_cooling(account)
                    return True
            except Exception:  # noqa: BLE001 - 激活失败不应中断整体流程
                continue
        return False

    def status(self) -> list[dict[str, Any]]:
        return [account.as_dict() for account in self.accounts]


def load_accounts(path: str | Path) -> list[dict[str, Any]]:
    """读取微信读书凭据文件（支持单账号或账号列表两种格式）。"""
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    return [dict(payload)]


def save_accounts(path: str | Path, accounts: list[dict[str, Any]]) -> None:
    """写入微信读书凭据文件（原子替换，mode 600）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(accounts, ensure_ascii=False, indent=2))
    os.chmod(temporary, 0o600)
    temporary.replace(target)
