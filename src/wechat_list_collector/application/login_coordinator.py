from __future__ import annotations

from typing import Protocol


class LoginCoordinatorError(RuntimeError):
    """A login session cannot be created, read, or cancelled safely."""


class LoginCoordinator(Protocol):
    @property
    def delivery_mode(self) -> str: ...

    def start(self, *, created_by: str, channel: str = "local_web") -> dict[str, object]: ...

    def status(self, session_id: str) -> dict[str, object] | None: ...

    def latest_status(self) -> dict[str, object] | None: ...

    def qr_png(self, session_id: str) -> bytes: ...

    def cancel(self, session_id: str) -> dict[str, object]: ...


class FakeLoginCoordinator:
    """Deterministic login-session double used by Admin API tests."""

    def __init__(self) -> None:
        self.session: dict[str, object] | None = None
        self.image = b"fake-png"
        self.delivery_mode = "web_qr"

    def start(self, *, created_by: str, channel: str = "local_web") -> dict[str, object]:
        if self.session is None:
            self.session = {
                "id": "test-login-session",
                "channel": channel,
                "status": "waiting_scan",
                "created_by": created_by,
                "created_at": "2026-07-30T12:00:00Z",
                "expires_at": "2026-07-30T12:05:00Z",
                "completed_at": None,
                "reason_code": "",
                "message": "请使用微信扫码",
            }
        return dict(self.session)

    def status(self, session_id: str) -> dict[str, object] | None:
        if self.session is None or self.session["id"] != session_id:
            return None
        return dict(self.session)

    def latest_status(self) -> dict[str, object] | None:
        return dict(self.session) if self.session is not None else None

    def qr_png(self, session_id: str) -> bytes:
        if self.session is None or self.session["id"] != session_id:
            raise LoginCoordinatorError("login session does not exist")
        return self.image

    def cancel(self, session_id: str) -> dict[str, object]:
        session = self.status(session_id)
        if session is None:
            raise LoginCoordinatorError("login session does not exist")
        session.update(status="cancelled", completed_at="2026-07-30T12:00:00Z", message="已取消")
        self.session = session
        return dict(session)
