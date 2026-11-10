from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from ..application.account_search import AccountSearchCoordinator, AccountSearchError
from ..application.login_coordinator import LoginCoordinator, LoginCoordinatorError
from ..application.read_models import ReadModel
from ..infrastructure.credential_vault import EncryptedFileCredentialVault
from ..infrastructure.db.repositories import CollectionRepository, RepositoryError


class AdminError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        self.code = code
        self.message = message
        self.status_code = status_code


class LoginBody(BaseModel):
    password: str


class AccountBody(BaseModel):
    source_account_id: str = Field(min_length=1, max_length=255)
    fakeid: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=255)
    avatar_url: str = ""
    display_order: int = 0


class AccountPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)
    display_order: int | None = None


class CollectSelection(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=100)


class LoginSessionBody(BaseModel):
    channel: str = "local_web"


class AccountSearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=100)


class SubscribeSearchCandidateBody(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    display_order: int = 0


def create_admin_app(
    repository: CollectionRepository,
    read_model: ReadModel,
    vault: EncryptedFileCredentialVault,
    login_coordinator: LoginCoordinator,
    account_search_coordinator: AccountSearchCoordinator,
    *,
    admin_password: str,
    session_secret: str,
) -> FastAPI:
    if not admin_password or not session_secret:
        raise ValueError("admin_password and session_secret are required")
    app = FastAPI(title="WeChat List Collector Admin", docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=session_secret, https_only=False, same_site="lax")
    web_root = Path(__file__).resolve().parents[1] / "web"
    templates = Jinja2Templates(directory=str(web_root / "templates"))
    app.mount("/static", StaticFiles(directory=str(web_root / "static")), name="static")

    @app.exception_handler(AdminError)
    async def admin_error_handler(_request: Request, error: AdminError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": {"code": error.code, "message": error.message}})

    def require_admin(request: Request) -> dict[str, str]:
        session = request.session
        if not session.get("admin"):
            raise AdminError("UNAUTHORIZED", "请先登录管理站点", 401)
        return {"csrf_token": str(session["csrf_token"])}

    def require_csrf(request: Request, session: dict[str, str]) -> None:
        csrf_token = request.headers.get("X-CSRF-Token")
        if not csrf_token or not secrets.compare_digest(csrf_token, session["csrf_token"]):
            raise AdminError("CSRF_INVALID", "安全校验失败", 403)

    @app.post("/api/v1/admin/session", status_code=204)
    def create_session(body: LoginBody, request: Request) -> Response:
        if not secrets.compare_digest(body.password, admin_password):
            raise AdminError("UNAUTHORIZED", "用户名或密码无效", 401)
        csrf_token = secrets.token_urlsafe(32)
        request.session.clear()
        request.session.update({"admin": True, "csrf_token": csrf_token})
        return Response(status_code=204, headers={"X-CSRF-Token": csrf_token})

    @app.delete("/api/v1/admin/session", status_code=204)
    def delete_session(request: Request, session: dict[str, str] = Depends(require_admin)) -> Response:
        require_csrf(request, session)
        request.session.clear()
        return Response(status_code=204)

    @app.get("/api/v1/admin/overview")
    def overview(_session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        accounts = repository.list_accounts()
        return {
            "accounts": {"total": len(accounts), "enabled": sum(bool(item["enabled"]) for item in accounts)},
            "latest_scheduled_batch": read_model.latest_scheduled_batch(),
            "recent_runs": repository.list_runs(limit=10),
        }

    @app.get("/api/v1/admin/wechat-auth")
    def wechat_auth(_session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        try:
            vault.load_wechat()
            credential_status = "available"
        except Exception:
            credential_status = "missing"
        return {
            "credential_status": credential_status,
            "delivery_mode": login_coordinator.delivery_mode,
            "login_session": login_coordinator.latest_status(),
        }

    @app.post("/api/v1/admin/login-sessions", status_code=202)
    def start_login_session(
        body: LoginSessionBody,
        request: Request,
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return login_coordinator.start(created_by="admin", channel=body.channel)
        except LoginCoordinatorError as error:
            raise AdminError("LOGIN_UNAVAILABLE", str(error), 409) from error

    @app.get("/api/v1/admin/login-sessions/{login_session_id}")
    def login_session_status(login_session_id: str, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        result = login_coordinator.status(login_session_id)
        if result is None:
            raise AdminError("LOGIN_SESSION_NOT_FOUND", "授权会话不存在", 404)
        return result

    @app.get("/api/v1/admin/login-sessions/{login_session_id}/qr")
    def login_session_qr(login_session_id: str, _session: dict[str, str] = Depends(require_admin)) -> Response:
        try:
            image = login_coordinator.qr_png(login_session_id)
        except LoginCoordinatorError as error:
            raise AdminError("QR_UNAVAILABLE", str(error), 409) from error
        return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store, max-age=0"})

    @app.post("/api/v1/admin/login-sessions/{login_session_id}/cancel", status_code=202)
    def cancel_login_session(
        login_session_id: str,
        request: Request,
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return login_coordinator.cancel(login_session_id)
        except LoginCoordinatorError as error:
            raise AdminError("LOGIN_SESSION_NOT_FOUND", str(error), 404) from error

    @app.get("/api/v1/admin/accounts")
    def accounts(include_archived: bool = False, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"accounts": repository.list_accounts(include_archived=include_archived)}

    @app.post("/api/v1/admin/accounts", status_code=201)
    def add_account(body: AccountBody, request: Request, session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        require_csrf(request, session)
        account_id = repository.add_account(
            source_account_id=body.source_account_id,
            fakeid_ciphertext=vault.encrypt_text(body.fakeid),
            name=body.name,
            avatar_url=body.avatar_url,
            display_order=body.display_order,
        )
        return next(item for item in repository.list_accounts(include_archived=True) if item["id"] == account_id)

    @app.get("/api/v1/admin/account-searches/latest")
    def latest_account_search(_session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"search": account_search_coordinator.latest_status()}

    @app.get("/api/v1/admin/account-searches/{search_id}")
    def account_search_status(search_id: str, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        result = account_search_coordinator.status(search_id)
        if result is None:
            raise AdminError("ACCOUNT_SEARCH_NOT_FOUND", "公众号搜索不存在", 404)
        return result

    @app.post("/api/v1/admin/account-searches", status_code=202)
    def start_account_search(
        body: AccountSearchBody,
        request: Request,
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return account_search_coordinator.start(query=body.query, created_by="admin")
        except AccountSearchError as error:
            raise AdminError("ACCOUNT_SEARCH_UNAVAILABLE", str(error), 409) from error

    @app.post("/api/v1/admin/account-searches/{search_id}/subscribe", status_code=201)
    def subscribe_search_candidate(
        search_id: str,
        body: SubscribeSearchCandidateBody,
        request: Request,
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        try:
            result = account_search_coordinator.subscribe(
                session_id=search_id,
                candidate_id=body.candidate_id,
                display_order=body.display_order,
            )
        except AccountSearchError as error:
            raise AdminError("ACCOUNT_SEARCH_RESULT_INVALID", str(error), 409) from error
        return next(item for item in repository.list_accounts(include_archived=True) if item["id"] == result["account_id"])

    @app.patch("/api/v1/admin/accounts/{account_id}")
    def patch_account(account_id: str, body: AccountPatch, request: Request, session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return repository.update_account(account_id, enabled=body.enabled, name=body.name, display_order=body.display_order)
        except RepositoryError as error:
            raise AdminError("ACCOUNT_NOT_FOUND", "公众号不存在", 404) from error

    @app.post("/api/v1/admin/accounts/{account_id}/archive")
    def archive_account(account_id: str, request: Request, session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return repository.archive_account(account_id)
        except RepositoryError as error:
            raise AdminError("ACCOUNT_NOT_FOUND", "公众号不存在或已归档", 404) from error

    @app.post("/api/v1/admin/accounts/{account_id}/restore")
    def restore_account(account_id: str, request: Request, session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        require_csrf(request, session)
        try:
            return repository.restore_account(account_id)
        except RepositoryError as error:
            raise AdminError("ACCOUNT_NOT_FOUND", "已归档公众号不存在", 404) from error

    @app.post("/api/v1/admin/collection-runs", status_code=202)
    def collect_all(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        if not idempotency_key:
            raise AdminError("IDEMPOTENCY_KEY_REQUIRED", "缺少 Idempotency-Key", 400)
        account_ids = repository.enabled_account_ids()
        if not account_ids:
            raise AdminError("NO_ENABLED_ACCOUNTS", "没有启用的公众号", 409)
        submitted = repository.submit_collection_command(
            command_type="collect_all",
            trigger="manual_web",
            account_ids=account_ids,
            idempotency_key=idempotency_key,
            submitted_by="admin",
        )
        return {"command_id": submitted.command_id, "run_id": submitted.run_id, "reused": submitted.reused}

    @app.post("/api/v1/admin/collection-commands", status_code=202)
    def collect_selected(
        body: CollectSelection,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        session: dict[str, str] = Depends(require_admin),
    ) -> dict[str, object]:
        require_csrf(request, session)
        if not idempotency_key:
            raise AdminError("IDEMPOTENCY_KEY_REQUIRED", "缺少 Idempotency-Key", 400)
        try:
            submitted = repository.submit_collection_command(
                command_type="collect_selected",
                trigger="manual_web",
                account_ids=body.account_ids,
                idempotency_key=idempotency_key,
                submitted_by="admin",
            )
        except RepositoryError as error:
            raise AdminError("INVALID_ACCOUNT", "公众号不存在", 400) from error
        return {
            "command_id": submitted.command_id,
            "run_id": submitted.run_id,
            "reused": submitted.reused,
            "items": [item.__dict__ for item in repository.command_items(submitted.command_id)],
        }

    @app.get("/api/v1/admin/collection-runs")
    def runs(limit: int = 50, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"runs": repository.list_runs(limit)}

    @app.get("/api/v1/admin/collection-runs/{run_id}")
    def run_detail(run_id: str, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        run = repository.get_run(run_id)
        if run is None:
            raise AdminError("RUN_NOT_FOUND", "采集批次不存在", 404)
        return run

    @app.get("/api/v1/admin/collection-runs/{run_id}/accounts")
    def run_accounts(run_id: str, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"accounts": repository.list_run_accounts(run_id)}

    @app.get("/api/v1/admin/articles")
    def admin_articles(limit: int = 100, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"articles": repository.list_articles(limit)}

    @app.get("/api/v1/admin/articles/{article_id}")
    def admin_article_detail(article_id: str, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        article = repository.get_article_by_id(article_id)
        if article is None:
            raise AdminError("ARTICLE_NOT_FOUND", "文章不存在", 404)
        return article

    @app.get("/api/v1/admin/events")
    def admin_events(limit: int = 100, _session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"events": repository.list_events(limit)}

    @app.get("/api/v1/admin/diagnostics")
    def diagnostics(_session: dict[str, str] = Depends(require_admin)) -> dict[str, object]:
        return {"requests": repository.recent_requests(), "latest_scheduled_batch": read_model.latest_scheduled_batch()}

    @app.get("/admin/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/admin", response_class=HTMLResponse)
    @app.get("/admin/{page_name}", response_class=HTMLResponse)
    def admin_page(request: Request, page_name: str = "overview") -> HTMLResponse:
        if not request.session.get("admin"):
            return templates.TemplateResponse(request, "login.html", {})
        valid = {"overview", "accounts", "runs", "articles", "diagnostics", "wechat-auth"}
        if page_name not in valid:
            page_name = "overview"
        return templates.TemplateResponse(request, "admin.html", {"page_name": page_name, "csrf_token": request.session["csrf_token"]})

    return app
