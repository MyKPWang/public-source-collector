from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...application.login_coordinator import LoginCoordinatorError
from ...models import Credentials
from ..credential_vault import EncryptedFileCredentialVault
from ..db.repositories import CollectionRepository, RepositoryError


_ACTIVE_STATUSES = {"creating", "waiting_scan", "scanned", "confirming", "validating"}
_TERMINAL_STATUSES = {"succeeded", "expired", "failed", "cancelled"}
_QR_SELECTORS = (
    ".login__type__container__scan__qrcode",
    ".login__scan__qrcode img",
    "img[src*='loginqrcode']",
)


@dataclass
class _LiveLogin:
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    qr_png: bytes | None = None


class PlaywrightLoginCoordinator:
    """A dedicated browser login session that turns WeChat authorization into Vault credentials."""

    def __init__(
        self,
        repository: CollectionRepository,
        vault: EncryptedFileCredentialVault,
        *,
        timeout_seconds: int = 300,
        browser: str = "chrome",
        delivery_mode: str = "desktop",
        profile_dir: Path | None = None,
    ) -> None:
        self._repository = repository
        self._vault = vault
        self._timeout_seconds = timeout_seconds
        self._browser = browser
        self._delivery_mode = delivery_mode
        self._profile_dir = profile_dir or vault.credentials_path.parent / "wechat-login-profile"
        self._lock = threading.RLock()
        self._live: dict[str, _LiveLogin] = {}
        self._repository.expire_login_sessions_after_restart()

    def start(self, *, created_by: str, channel: str = "local_web") -> dict[str, object]:
        if channel != "local_web":
            raise LoginCoordinatorError("the requested QR delivery channel is not configured")
        if self._repository.has_running_collection_jobs():
            raise LoginCoordinatorError("collection is running; try authorizing again after it finishes")
        session, reused = self._repository.create_login_session(
            channel=channel,
            created_by=created_by,
            expires_in_seconds=self._timeout_seconds,
        )
        session_id = str(session["id"])
        with self._lock:
            if reused and session_id in self._live:
                return self._with_delivery_mode(session)
            if reused:
                # A live DB state with no browser cannot be recovered after a process crash.
                self._repository.update_login_session(
                    session_id,
                    status="expired",
                    reason_code="LOGIN_PROCESS_UNAVAILABLE",
                    message="授权进程不可用，请重新生成二维码",
                )
                session, _ = self._repository.create_login_session(
                    channel=channel,
                    created_by=created_by,
                    expires_in_seconds=self._timeout_seconds,
                )
                session_id = str(session["id"])
            self._live[session_id] = _LiveLogin()
            thread = threading.Thread(target=self._run, args=(session_id,), name=f"wechat-login-{session_id[:8]}", daemon=True)
            thread.start()
        return self._with_delivery_mode(session)

    def status(self, session_id: str) -> dict[str, object] | None:
        session = self._repository.get_login_session(session_id)
        return self._with_delivery_mode(session) if session is not None else None

    def latest_status(self) -> dict[str, object] | None:
        session = self._repository.latest_login_session()
        return self._with_delivery_mode(session) if session is not None else None

    @property
    def delivery_mode(self) -> str:
        return self._delivery_mode

    def qr_png(self, session_id: str) -> bytes:
        if self._delivery_mode != "web_qr":
            raise LoginCoordinatorError("二维码显示在部署机打开的 Chrome 中")
        session = self._repository.get_login_session(session_id)
        if session is None:
            raise LoginCoordinatorError("login session does not exist")
        if str(session["channel"]) != "local_web" or str(session["status"]) not in _ACTIVE_STATUSES:
            raise LoginCoordinatorError("QR code is no longer available")
        with self._lock:
            live = self._live.get(session_id)
            if live is None or live.qr_png is None:
                raise LoginCoordinatorError("QR code is still being created")
            return live.qr_png

    def cancel(self, session_id: str) -> dict[str, object]:
        session = self._repository.get_login_session(session_id)
        if session is None:
            raise LoginCoordinatorError("login session does not exist")
        if str(session["status"]) not in _TERMINAL_STATUSES:
            with self._lock:
                live = self._live.get(session_id)
                if live is not None:
                    live.cancel_requested.set()
            return self._repository.update_login_session(session_id, status="cancelled", message="授权已取消")
        return session

    def _run(self, session_id: str) -> None:
        browser = None
        context = None
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                launch_options: dict[str, object] = {"headless": self._delivery_mode == "web_qr"}
                if self._browser == "chrome":
                    launch_options["channel"] = "chrome"
                if self._delivery_mode == "desktop":
                    self._profile_dir.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        str(self._profile_dir),
                        viewport={"width": 1280, "height": 900},
                        locale="zh-CN",
                        **launch_options,
                    )
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    browser = playwright.chromium.launch(**launch_options)
                    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                    page = context.new_page()
                page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded", timeout=45_000)
                if self._authorized(page, context):
                    self._persist_credentials(session_id, page, context)
                    return
                locator = self._wait_for_qr(page, PlaywrightTimeoutError)
                if self._delivery_mode == "web_qr":
                    qr = locator.screenshot(type="png")
                    if len(qr) <= 512:
                        raise LoginCoordinatorError("WeChat login page did not render a QR code")
                    with self._lock:
                        live = self._live.get(session_id)
                        if live is None or live.cancel_requested.is_set():
                            return
                        live.qr_png = qr
                message = "请在部署机打开的 Chrome 中扫码" if self._delivery_mode == "desktop" else "请使用微信扫码"
                self._transition(session_id, "waiting_scan", message=message)
                deadline = time.monotonic() + self._timeout_seconds
                scanned_reported = False
                while time.monotonic() < deadline:
                    if self._cancelled(session_id):
                        return
                    if self._authorized(page, context):
                        self._persist_credentials(session_id, page, context)
                        return
                    if not scanned_reported and self._looks_scanned(page):
                        scanned_reported = True
                        self._transition(session_id, "scanned", message="已扫码，请在手机上确认")
                    time.sleep(1)
                self._transition(session_id, "expired", reason_code="QR_EXPIRED", message="二维码已过期")
        except Exception as error:
            if not self._cancelled(session_id):
                reason = "BROWSER_UNAVAILABLE" if "Executable doesn't exist" in str(error) else "LOGIN_FAILED"
                message = "浏览器登录启动失败，请检查 Chrome 或 Playwright Chromium 是否可用" if reason == "BROWSER_UNAVAILABLE" else "微信授权未完成，请重新发起授权"
                self._transition(session_id, "failed", reason_code=reason, message=message)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            with self._lock:
                self._live.pop(session_id, None)

    @staticmethod
    def _wait_for_qr(page: Any, timeout_error: type[Exception]) -> Any:
        last_error: Exception | None = None
        for selector in _QR_SELECTORS:
            try:
                locator = page.locator(selector).first
                locator.wait_for(state="visible", timeout=15_000)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if locator.evaluate("(image) => image.complete && image.naturalWidth > 0 && image.naturalHeight > 0"):
                        return locator
                    page.wait_for_timeout(250)
                raise LoginCoordinatorError("WeChat login QR code image did not finish loading")
            except timeout_error as error:
                last_error = error
        raise LoginCoordinatorError("WeChat login QR code was not found") from last_error

    @staticmethod
    def _extract_token(page: Any, context: Any) -> str:
        query_token = parse_qs(urlsplit(page.url).query).get("token", [""])[0]
        if query_token:
            return str(query_token)
        for expression in ("() => localStorage.getItem('token')", "() => sessionStorage.getItem('token')"):
            try:
                value = page.evaluate(expression)
                if value:
                    return str(value)
            except Exception:
                pass
        for cookie in context.cookies():
            if "token" in str(cookie.get("name", "")).lower() and cookie.get("value"):
                return str(cookie["value"])
        return ""

    def _authorized(self, page: Any, context: Any) -> bool:
        return bool(self._extract_token(page, context)) and self._looks_authenticated(page)

    def _persist_credentials(self, session_id: str, page: Any, context: Any) -> None:
        self._transition(session_id, "validating", message="正在确认授权")
        credentials = Credentials(token=self._extract_token(page, context), cookie=self._cookie_header(context.cookies()))
        if not credentials.cookie:
            raise LoginCoordinatorError("WeChat login completed without session cookies")
        self._vault.replace_wechat(credentials)
        self._transition(session_id, "succeeded", message="授权成功")

    def _with_delivery_mode(self, session: dict[str, object]) -> dict[str, object]:
        return {**session, "delivery_mode": self._delivery_mode}

    @staticmethod
    def _cookie_header(cookies: list[dict[str, object]]) -> str:
        pairs = [f"{cookie['name']}={cookie['value']}" for cookie in cookies if cookie.get("name") and cookie.get("value")]
        return "; ".join(pairs)

    @staticmethod
    def _looks_authenticated(page: Any) -> bool:
        path = urlsplit(page.url).path
        return "/cgi-bin/" in path and not any(page.locator(selector).count() for selector in _QR_SELECTORS)

    @staticmethod
    def _looks_scanned(page: Any) -> bool:
        try:
            body = page.locator("body").inner_text(timeout=1_000)
        except Exception:
            return False
        return "确认登录" in body or "已扫码" in body

    def _cancelled(self, session_id: str) -> bool:
        session = self._repository.get_login_session(session_id)
        if session is None or str(session["status"]) == "cancelled":
            return True
        with self._lock:
            live = self._live.get(session_id)
            return live is None or live.cancel_requested.is_set()

    def _transition(self, session_id: str, status: str, *, reason_code: str = "", message: str = "") -> None:
        session = self._repository.get_login_session(session_id)
        if session is None or str(session["status"]) in _TERMINAL_STATUSES:
            return
        try:
            self._repository.update_login_session(session_id, status=status, reason_code=reason_code, message=message)
        except RepositoryError:
            return
