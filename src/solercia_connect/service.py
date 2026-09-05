from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from solercia_connect import __version__
from solercia_connect.application import run_application
from solercia_connect.bootstrap import registry_from_environment
from solercia_connect.config import (
    MICROSOFT_TARGETS,
    TARGET_SPECS,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    http_interactive_allowed,
    load_settings,
    normalize_account,
)
from solercia_connect.connections import ConnectionService, connect_command
from solercia_connect.doctor import DoctorService
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.interactive_sessions import InteractiveSessionRegistry
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import KeyringOAuthTokenStore, OAuthTokenStore
from solercia_connect.serialization import to_json, to_primitive

_PROTECTED_POST_PATHS = frozenset(
    {"/v1/connections/test", "/v1/run", "/v1/connections/connect"},
)
_PROTECTED_GET_PREFIXES = ("/v1/connections/sessions/",)
_HTTP_INTERACTIVE_REMEDIATION = (
    "Set SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true (with SOLERCIA_CONNECT_BROWSER_FALLBACK=true "
    "and an API key) to allow the loopback service to start the supervised browser."
)


class ConnectionTester(Protocol):
    def test_connection(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> ResultEnvelope: ...

    def connect_browser(self, type_value: str, account: str) -> ResultEnvelope: ...


class RunHandler(Protocol):
    def __call__(
        self,
        payload: dict[str, Any],
        *,
        browser_via_http: bool = False,
    ) -> ResultEnvelope: ...


class ConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConnectionType
    account: str
    allow_browser_fallback: bool = False

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        return normalize_account(value)


class ConnectionConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConnectionType
    account: str
    allow_browser_fallback: bool = False

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        return normalize_account(value)

    @model_validator(mode="after")
    def validate_browser_target(self) -> ConnectionConnectRequest:
        if self.allow_browser_fallback and self.type not in MICROSOFT_TARGETS:
            raise ValueError("browser fallback is available only for Microsoft targets")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ConnectionType
    account: str | None = None
    capability: str
    start: datetime | None = None
    end: datetime | None = None
    calendar_id: str = "primary"
    assignee: str | None = None
    jql: str | None = None
    meeting_id: str | None = None
    search: str | None = None
    folder_path: str | None = None
    item_id: str | None = None
    drive_id: str | None = None
    site: str | None = None
    include_shared: bool = True
    include_content: bool | None = None
    include_attachments: bool = False
    max_attachment_bytes: int = Field(default=10_485_760, ge=1)
    max_content_bytes: int = Field(default=10_485_760, ge=1)
    limit: int = Field(default=50, ge=1, le=1000)
    allow_browser_fallback: bool = False

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str | None) -> str | None:
        return normalize_account(value) if value is not None else None

    @field_validator("capability", "calendar_id")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value cannot be empty")
        return candidate

    @model_validator(mode="after")
    def validate_window(self) -> RunRequest:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be provided together")
        if self.start is not None and self.end is not None:
            if self.start.tzinfo is None or self.end.tzinfo is None:
                raise ValueError("start and end must be timezone-aware")
            if self.start >= self.end:
                raise ValueError("start must be before end")
        if (
            self.type
            in {
                ConnectionType.OUTLOOK,
                ConnectionType.TEAMS,
                ConnectionType.ONEDRIVE,
                ConnectionType.GMAIL,
                ConnectionType.GOOGLE_CALENDAR,
                ConnectionType.GOOGLE_MEET,
            }
            and self.account is None
        ):
            raise ValueError("account is required for OAuth provider runs")
        if self.allow_browser_fallback and self.type not in {
            ConnectionType.OUTLOOK,
            ConnectionType.TEAMS,
            ConnectionType.ONEDRIVE,
        }:
            raise ValueError("browser fallback is available only for Microsoft targets")
        return self

    def application_payload(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "account": self.account,
            "capability": self.capability,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "calendar_id": self.calendar_id,
            "assignee": self.assignee,
            "jql": self.jql,
            "meeting_id": self.meeting_id,
            "search": self.search,
            "folder_path": self.folder_path,
            "item_id": self.item_id,
            "drive_id": self.drive_id,
            "site": self.site,
            "include_shared": self.include_shared,
            "max_content_bytes": self.max_content_bytes,
            "include_attachments": self.include_attachments,
            "max_attachment_bytes": self.max_attachment_bytes,
            "limit": self.limit,
            "allow_browser_fallback": self.allow_browser_fallback,
            **({} if self.include_content is None else {"include_content": self.include_content}),
        }


def create_app(
    *,
    settings: ConnectSettings | None = None,
    connection_service: ConnectionTester | None = None,
    doctor_service: DoctorService | None = None,
    run_handler: RunHandler | None = None,
    token_store: OAuthTokenStore | None = None,
    browser_connector: Callable[[str, str], ResultEnvelope] | None = None,
    session_registry: InteractiveSessionRegistry | None = None,
) -> FastAPI:
    startup_error: ValueError | None = None
    if settings is None:
        try:
            settings = load_settings()
        except ValueError as failure:
            startup_error = failure
            settings = load_settings(environ={})
    profiles = ProfileStore.from_settings(settings)
    tokens = token_store or KeyringOAuthTokenStore()
    connections = connection_service or ConnectionService(
        settings,
        profiles,
        tokens,
    )
    doctor = doctor_service or DoctorService(settings, profiles)
    sessions = session_registry or InteractiveSessionRegistry()

    def default_run(payload: dict[str, Any], *, browser_via_http: bool = False) -> ResultEnvelope:
        return run_application(
            payload,
            interactive=False,
            settings=settings,
            token_store=tokens,
            browser_via_http=browser_via_http,
        )

    execute_run: RunHandler = run_handler or default_run

    def start_browser_login(type_value: str, account: str) -> ResultEnvelope:
        connector = browser_connector or connections.connect_browser
        return connector(type_value, account)

    application = FastAPI(
        title="Solercia Connect",
        version=__version__,
        description=(
            "Loopback-only adapter over Solercia Connect services. Interactive browser login is "
            "available only behind an explicit local switch."
        ),
    )
    application.state.settings = settings
    application.state.startup_error = startup_error

    @application.middleware("http")
    async def require_local_api_key(request: Request, call_next: Any) -> Any:
        if _requires_api_key(request.method, request.url.path):
            error_response = _api_key_error(request, settings)
            if error_response is not None:
                return error_response
        try:
            return await call_next(request)
        except Exception:
            envelope = ResultEnvelope.failure(
                operation="http",
                status="failed",
                error=ErrorDetail(
                    "unknown_error",
                    "Service operation failed with an unknown error",
                    False,
                    remediation=("Run the CLI doctor command and retry.",),
                    cause_category="unknown",
                ),
            )
            return _response(envelope, 500)

    @application.exception_handler(RequestValidationError)
    async def validation_error(_: Request, failure: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "type": str(error.get("type", "validation_error")),
                "message": str(error.get("msg", "Invalid request value")),
            }
            for error in failure.errors()
        ]
        envelope = ResultEnvelope.failure(
            operation="http.validation",
            status="invalid_input",
            data={"issues": issues},
            error=ErrorDetail(
                "invalid_input",
                "HTTP request validation failed",
                False,
                remediation=("Correct the typed request body or path parameter.",),
                cause_category="input",
            ),
        )
        return _response(envelope, 422)

    @application.exception_handler(Exception)
    async def unknown_error(_: Request, failure: Exception) -> JSONResponse:
        del failure
        envelope = ResultEnvelope.failure(
            operation="http",
            status="failed",
            error=ErrorDetail(
                "unknown_error",
                "Service operation failed with an unknown error",
                False,
                remediation=("Run the CLI doctor command and retry.",),
                cause_category="unknown",
            ),
        )
        return _response(envelope, 500)

    @application.get("/v1/health")
    def health() -> JSONResponse:
        if startup_error is not None:
            envelope = ResultEnvelope.failure(
                operation="health",
                status="configuration_error",
                error=ErrorDetail(
                    "configuration_invalid",
                    str(startup_error),
                    False,
                    remediation=("Run solercia-connect config validate.",),
                    cause_category="configuration",
                ),
            )
            return _response(envelope, 503)
        return _response(
            ResultEnvelope.success(
                operation="health",
                status="ready",
                data={
                    "version": __version__,
                    "bind_host": settings.api_host,
                    "interactive_login": http_interactive_allowed(settings),
                },
            ),
            200,
        )

    @application.get("/v1/providers")
    def providers() -> JSONResponse:
        try:
            configured = [
                descriptor.provider_id
                for descriptor in registry_from_environment(include_browser=False).descriptors()
            ]
        except Exception:
            configured = []
        envelope = ResultEnvelope.success(
            operation="providers",
            data={
                "configured": configured,
                "targets": [
                    {
                        "type": spec.type.value,
                        "provider_id": spec.provider_id,
                        "capabilities": list(spec.capabilities),
                    }
                    for spec in TARGET_SPECS
                ],
                "matrix": [to_primitive(item) for item in CAPABILITY_MATRIX],
            },
        )
        return _response(envelope, 200)

    @application.get("/v1/doctor/{type}")
    def inspect_doctor(
        type: ConnectionType,
        account: Annotated[str | None, Query()] = None,
    ) -> JSONResponse:
        try:
            report = doctor.inspect(type, account)
            if report.ready:
                envelope = ResultEnvelope.success(
                    operation="doctor",
                    status="ready",
                    provider=type.value,
                    account=report.account,
                    data=report.to_dict(),
                )
            else:
                envelope = ResultEnvelope.failure(
                    operation="doctor",
                    status="not_ready",
                    provider=type.value,
                    account=report.account,
                    data=report.to_dict(),
                    error=ErrorDetail(
                        "not_ready",
                        "One or more required readiness checks failed",
                        False,
                        report.missing_configuration,
                        ("Apply the remediation steps in the doctor checks.",),
                        "configuration",
                    ),
                )
            return _response(envelope, _http_status(envelope))
        except (TypeError, ValueError) as failure:
            envelope = ResultEnvelope.failure(
                operation="doctor",
                status="invalid_input",
                provider=type.value,
                error=ErrorDetail(
                    "invalid_input",
                    str(failure),
                    False,
                    remediation=("Provide one valid account email address.",),
                    cause_category="input",
                ),
            )
            return _response(envelope, 422)

    @application.post("/v1/connections/test")
    def test_connection(body: ConnectionTestRequest) -> JSONResponse:
        if body.allow_browser_fallback and not http_interactive_allowed(settings):
            envelope = _http_action_required(
                "connections.test",
                body.type,
                body.account,
                browser_fallback=True,
            )
            return _response(envelope, 409)
        envelope = connections.test_connection(
            body.type.value,
            body.account,
            allow_browser_fallback=body.allow_browser_fallback,
        )
        return _response(envelope, _http_status(envelope))

    @application.post("/v1/connections/connect", status_code=202)
    def connect(body: ConnectionConnectRequest) -> JSONResponse:
        if not body.allow_browser_fallback:
            envelope = ResultEnvelope.failure(
                operation="connections.connect",
                status="invalid_input",
                provider=body.type.value,
                account=body.account,
                data={"interactive_flow_started": False},
                error=ErrorDetail(
                    "invalid_input",
                    "The loopback service starts only the supervised browser route",
                    False,
                    remediation=(
                        "Use the CLI for the official device-code route.",
                        connect_command(body.type, body.account),
                    ),
                    cause_category="input",
                ),
            )
            return _response(envelope, 422)
        if not http_interactive_allowed(settings):
            envelope = _http_action_required(
                "connections.connect",
                body.type,
                body.account,
                browser_fallback=True,
            )
            return _response(envelope, 409)
        type_value = body.type.value
        account = body.account
        session_id = sessions.start(
            type_value,
            account,
            lambda: start_browser_login(type_value, account),
        )
        if session_id is None:
            running = sessions.active()
            envelope = ResultEnvelope.failure(
                operation="connections.connect",
                status="busy",
                provider=type_value,
                account=account,
                route="browser",
                data={
                    "session_id": running.session_id if running is not None else None,
                    "interactive_flow_started": False,
                },
                error=ErrorDetail(
                    "interactive_session_busy",
                    "Another interactive browser session is still running",
                    True,
                    remediation=(
                        "Poll GET /v1/connections/sessions/{session_id} until it finishes, "
                        "then retry.",
                    ),
                    cause_category="interactive_authorization",
                ),
            )
            return _response(envelope, 409)
        envelope = ResultEnvelope.success(
            operation="connections.connect",
            status="started",
            provider=type_value,
            account=account,
            route="browser",
            data={
                "session_id": session_id,
                "interactive_flow_started": True,
                "expires_in_seconds": sessions.retention_seconds,
            },
        )
        return _response(envelope, 202)

    @application.get("/v1/connections/sessions/{session_id}")
    def inspect_session(session_id: str) -> JSONResponse:
        session = sessions.get(session_id)
        if session is None:
            envelope = ResultEnvelope.failure(
                operation="connections.session",
                status="not_found",
                error=ErrorDetail(
                    "session_not_found",
                    "No interactive session with that id is known to this service process",
                    False,
                    remediation=("Start a new login with POST /v1/connections/connect.",),
                    cause_category="input",
                ),
            )
            return _response(envelope, 404)
        timestamps = {
            "session_id": session.session_id,
            "started_at": session.started_at.isoformat(),
        }
        if session.result is None:
            envelope = ResultEnvelope.success(
                operation="connections.session",
                status="running",
                provider=session.type_value,
                account=session.account,
                route="browser",
                data={**timestamps, "interactive_flow_started": True},
            )
            return _response(envelope, 200)
        result = session.result
        finished_at = session.finished_at.isoformat() if session.finished_at is not None else None
        data = result.data if isinstance(result.data, dict) else {}
        envelope = ResultEnvelope(
            result.ok,
            "connections.session",
            result.status,
            result.provider,
            result.account,
            result.route,
            {**data, **timestamps, "finished_at": finished_at},
            result.receipt,
            result.error,
        )
        return _response(envelope, 200)

    @application.post("/v1/run")
    def run(body: RunRequest) -> JSONResponse:
        if body.allow_browser_fallback:
            assert body.account is not None
            if not http_interactive_allowed(settings):
                envelope = _http_action_required(
                    "run",
                    body.type,
                    body.account,
                    browser_fallback=True,
                )
                return _response(envelope, 409)
            envelope = execute_run(body.application_payload(), browser_via_http=True)
        else:
            envelope = execute_run(body.application_payload())
        return _response(envelope, _http_status(envelope))

    return application


def _requires_api_key(method: str, path: str) -> bool:
    if method == "POST" and path in _PROTECTED_POST_PATHS:
        return True
    return method == "GET" and path.startswith(_PROTECTED_GET_PREFIXES)


def _api_key_error(request: Request, settings: ConnectSettings) -> JSONResponse | None:
    if settings.api_key is None:
        envelope = ResultEnvelope.failure(
            operation="http.auth",
            status="configuration_error",
            error=ErrorDetail(
                "configuration_invalid",
                "Local API authentication is not configured",
                False,
                ("SOLERCIA_CONNECT_API_KEY",),
                ("Set SOLERCIA_CONNECT_API_KEY before using POST endpoints.",),
                "configuration",
            ),
        )
        return _response(envelope, 503)
    supplied = request.headers.get("X-Solercia-Connect-Key", "")
    try:
        supplied_bytes = supplied.encode("ascii")
        expected_bytes = settings.api_key.reveal().encode("ascii")
        accepted = bool(supplied_bytes) and hmac.compare_digest(
            supplied_bytes,
            expected_bytes,
        )
    except UnicodeEncodeError:
        accepted = False
    if not accepted:
        envelope = ResultEnvelope.failure(
            operation="http.auth",
            status="unauthorized",
            error=ErrorDetail(
                "api_authentication_required",
                "A valid local API key is required",
                False,
                remediation=("Send X-Solercia-Connect-Key with the configured local key.",),
                cause_category="authorization",
            ),
        )
        return _response(envelope, 401)
    return None


def _http_action_required(
    operation: str,
    type_value: ConnectionType,
    account: str,
    *,
    browser_fallback: bool,
) -> ResultEnvelope:
    return ResultEnvelope.failure(
        operation=operation,
        status="action_required",
        provider=type_value.value,
        account=account,
        data={"interactive_flow_started": False},
        error=ErrorDetail(
            "action_required",
            "Interactive authorization must run in the canonical CLI",
            True,
            remediation=(
                connect_command(
                    type_value,
                    account,
                    allow_browser_fallback=browser_fallback,
                ),
                _HTTP_INTERACTIVE_REMEDIATION,
            ),
            cause_category="interactive_authorization",
        ),
    )


def _http_status(envelope: ResultEnvelope) -> int:
    if envelope.ok:
        return 200
    if envelope.status == "invalid_input":
        return 422
    if envelope.status == "not_found":
        return 404
    if envelope.status == "action_required":
        return 409
    if envelope.status in {"configuration_error", "not_ready"}:
        return 503
    if envelope.error is not None:
        if envelope.error.code == "api_authentication_required":
            return 401
        if envelope.error.code in {
            "authorization_denied",
            "authorization_boundary_violation",
            "tenant_policy_blocked",
        }:
            return 403
        if envelope.error.retryable:
            return 503
    return 502


def _response(envelope: ResultEnvelope, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=envelope.to_dict())


def main() -> None:
    try:
        settings = load_settings()
    except ValueError as failure:
        envelope = ResultEnvelope.failure(
            operation="service.start",
            status="configuration_error",
            error=ErrorDetail(
                "configuration_invalid",
                str(failure),
                False,
                remediation=("Run solercia-connect config validate.",),
                cause_category="configuration",
            ),
        )
        print(to_json(envelope.to_dict()))
        raise SystemExit(1) from None
    try:
        import uvicorn
    except ImportError:
        envelope = ResultEnvelope.failure(
            operation="service.start",
            status="configuration_error",
            error=ErrorDetail(
                "optional_dependency_missing",
                "Install the service dependency extra",
                False,
                remediation=("Run make setup.",),
                cause_category="configuration",
            ),
        )
        print(to_json(envelope.to_dict()))
        raise SystemExit(1) from None
    uvicorn.run(
        "solercia_connect.service:app",
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
    )


app = create_app()
