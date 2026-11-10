from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Protocol

from ..domain.errors import CooldownActive, RequestBudgetExhausted, WeChatNetworkFailed, WeChatRateLimited, WeChatSessionExpired
from ..domain.models import RequestIntent
from ..infrastructure.credential_vault import EncryptedFileCredentialVault, VaultError
from ..infrastructure.db.repositories import CollectionRepository, RepositoryError
from ..infrastructure.request_gate import RequestGate


class AccountSearchError(RuntimeError):
    """An account-name search could not be started, completed, or used safely."""


class AccountSearchGateway(Protocol):
    def search_accounts(self, *, keyword: str, limit: int = 5) -> list[dict[str, object]]: ...


class AccountSearchCoordinator:
    """Durable, rate-controlled WeChat account-name lookup for the admin site."""

    def __init__(
        self,
        repository: CollectionRepository,
        vault: EncryptedFileCredentialVault,
        gate: RequestGate,
        gateway_factory: Callable[[], AccountSearchGateway],
        minimum_interval_seconds: int = 15,
    ) -> None:
        if minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must be non-negative")
        self._repository = repository
        self._vault = vault
        self._gate = gate
        self._gateway_factory = gateway_factory
        self._minimum_interval_seconds = minimum_interval_seconds
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, *, query: str, created_by: str) -> dict[str, object]:
        normalized_query = query.strip()
        if not normalized_query:
            raise AccountSearchError("请输入公众号名称")
        try:
            self._vault.load_wechat()
        except VaultError as error:
            raise AccountSearchError("请先完成微信授权") from error
        session, reused = self._repository.create_account_search_session(query=normalized_query, created_by=created_by)
        session_id = str(session["id"])
        with self._lock:
            if not reused:
                thread = threading.Thread(target=self._run, args=(session_id,), name=f"account-search-{session_id[:8]}", daemon=True)
                self._threads[session_id] = thread
                thread.start()
        return self.public_status(session_id) or {}

    def status(self, session_id: str) -> dict[str, object] | None:
        return self.public_status(session_id)

    def latest_status(self) -> dict[str, object] | None:
        session = self._repository.latest_account_search_session()
        return self._public_session(session) if session is not None else None

    def subscribe(self, *, session_id: str, candidate_id: str, display_order: int = 0) -> dict[str, object]:
        session = self._repository.get_account_search_session(session_id)
        if session is None:
            raise AccountSearchError("搜索会话不存在")
        if str(session["status"]) != "succeeded":
            raise AccountSearchError("搜索尚未完成")
        candidates = self._decrypt_candidates(session)
        candidate = next((item for item in candidates if item["id"] == candidate_id), None)
        if candidate is None:
            raise AccountSearchError("搜索结果已失效，请重新搜索")
        account_id = self._repository.add_account(
            source_account_id=str(candidate["source_account_id"]),
            fakeid_ciphertext=self._vault.encrypt_text(str(candidate["fakeid"])),
            name=str(candidate["name"]),
            avatar_url=str(candidate["avatar_url"]),
            display_order=display_order,
        )
        return {"account_id": account_id, "name": candidate["name"]}

    def public_status(self, session_id: str) -> dict[str, object] | None:
        session = self._repository.get_account_search_session(session_id)
        return self._public_session(session) if session is not None else None

    def _run(self, session_id: str) -> None:
        permit = None
        try:
            session = self._repository.get_account_search_session(session_id)
            if session is None:
                return
            self._repository.update_account_search_session(session_id, status="waiting", message="等待采集请求间隔")
            permit = self._gate.acquire(
                RequestIntent(command_id=str(session["command_id"]), operation="account_search", page_number=1, begin=0, count=5),
                operation_minimum_interval_seconds=self._minimum_interval_seconds,
                updates_global_interval=False,
            )
            self._repository.update_account_search_session(session_id, status="running", message="正在搜索公众号")
            started = time.monotonic()
            results = self._gateway_factory().search_accounts(keyword=str(session["query"]), limit=5)
            candidates = self._normalize_candidates(results)
            payload = self._vault.encrypt_text(json.dumps(candidates, ensure_ascii=False, separators=(",", ":")))
            self._gate.succeed(
                permit,
                publish_record_count=0,
                article_count=len(candidates),
                duration_ms=round((time.monotonic() - started) * 1000),
                response_schema="searchbiz-v1",
            )
            self._repository.update_account_search_session(
                session_id,
                status="succeeded",
                candidate_payload_ciphertext=payload,
                candidate_count=len(candidates),
                message=f"搜索完成，找到 {len(candidates)} 个候选公众号；请选择一个添加订阅",
            )
        except WeChatRateLimited:
            self._fail(session_id, permit, status="rate_limited", reason_code="WECHAT_200013", message="微信限制了请求频率，已进入冷却")
        except WeChatSessionExpired:
            self._fail(session_id, permit, status="session_expired", reason_code="WECHAT_200003", message="微信授权已失效，请重新授权")
        except (WeChatNetworkFailed, CooldownActive, RequestBudgetExhausted, VaultError, RepositoryError) as error:
            reason = "COLLECTION_COOLDOWN_ACTIVE" if isinstance(error, CooldownActive) else "WECHAT_REQUEST_FAILED"
            message = "当前处于请求冷却期，请稍后再试" if isinstance(error, CooldownActive) else "搜索失败，请稍后再试"
            self._fail(session_id, permit, status="network_failed", reason_code=reason, message=message)
        except Exception:
            self._fail(session_id, permit, status="unknown", reason_code="ACCOUNT_SEARCH_FAILED", message="搜索失败，请稍后再试")
        finally:
            with self._lock:
                self._threads.pop(session_id, None)

    def _fail(self, session_id: str, permit, *, status: str, reason_code: str, message: str) -> None:
        if permit is not None:
            self._gate.fail(permit, status=status, reason_code=reason_code, wechat_ret=200013 if status == "rate_limited" else None)
        try:
            self._repository.update_account_search_session(session_id, status="failed", reason_code=reason_code, message=message)
        except RepositoryError:
            return

    def _public_session(self, session: dict[str, object]) -> dict[str, object]:
        return {
            "id": session["id"], "query": session["query"], "status": session["status"],
            "candidate_count": session["candidate_count"], "created_at": session["created_at"],
            "started_at": session["started_at"], "finished_at": session["finished_at"],
            "reason_code": session["reason_code"], "message": session["message"],
            "candidates": self._sanitized_candidates(session),
        }

    def _sanitized_candidates(self, session: dict[str, object]) -> list[dict[str, object]]:
        if str(session["status"]) != "succeeded" or not session.get("candidate_payload_ciphertext"):
            return []
        return [{"id": item["id"], "name": item["name"], "alias": item["alias"], "avatar_url": item["avatar_url"]} for item in self._decrypt_candidates(session)]

    def _decrypt_candidates(self, session: dict[str, object]) -> list[dict[str, str]]:
        ciphertext = session.get("candidate_payload_ciphertext")
        if not isinstance(ciphertext, bytes):
            raise AccountSearchError("搜索结果已失效，请重新搜索")
        try:
            candidates = json.loads(self._vault.decrypt_text(ciphertext))
        except (ValueError, VaultError) as error:
            raise AccountSearchError("搜索结果已失效，请重新搜索") from error
        if not isinstance(candidates, list):
            raise AccountSearchError("搜索结果已失效，请重新搜索")
        return [item for item in candidates if isinstance(item, dict)]

    @staticmethod
    def _normalize_candidates(results: list[dict[str, object]]) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        seen_fakeids: set[str] = set()
        for raw in results:
            fakeid = str(raw.get("fakeid", "")).strip()
            name = str(raw.get("nickname") or raw.get("alias") or "").strip()
            if not fakeid or not name or fakeid in seen_fakeids:
                continue
            seen_fakeids.add(fakeid)
            alias = str(raw.get("alias") or "").strip()
            candidates.append({
                "id": f"candidate-{len(candidates) + 1}", "fakeid": fakeid,
                "source_account_id": alias or f"mp-{hashlib.sha256(fakeid.encode('utf-8')).hexdigest()[:24]}", "name": name, "alias": alias,
                "avatar_url": str(raw.get("round_head_img") or raw.get("head_img_url") or ""),
            })
        return candidates


class FakeAccountSearchCoordinator:
    """In-memory account search double for the Admin API contract tests."""

    def __init__(self) -> None:
        self.session: dict[str, object] | None = None
        self.account_id = "test-account"

    def start(self, *, query: str, created_by: str) -> dict[str, object]:
        self.session = {
            "id": "test-account-search", "query": query, "status": "succeeded", "candidate_count": 1,
            "created_at": "2026-07-31T00:00:00Z", "started_at": "2026-07-31T00:00:00Z",
            "finished_at": "2026-07-31T00:00:01Z", "reason_code": "", "message": "搜索完成",
            "candidates": [{"id": "candidate-1", "name": "测试公众号", "alias": "test", "avatar_url": ""}],
        }
        return dict(self.session)

    def status(self, session_id: str) -> dict[str, object] | None:
        if self.session is None or self.session["id"] != session_id:
            return None
        return dict(self.session)

    def latest_status(self) -> dict[str, object] | None:
        return dict(self.session) if self.session is not None else None

    def subscribe(self, *, session_id: str, candidate_id: str, display_order: int = 0) -> dict[str, object]:
        if self.session is None or self.session["id"] != session_id or candidate_id != "candidate-1":
            raise AccountSearchError("搜索结果已失效，请重新搜索")
        return {"account_id": self.account_id, "name": "测试公众号"}
