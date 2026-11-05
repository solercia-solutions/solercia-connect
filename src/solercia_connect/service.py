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
    TARGET_SPECS,
    ConnectionType,
    ConnectSettings,
    ProfileStore,
    load_settings,
    normalize_account,
)
from solercia_connect.connections import ConnectionService, connect_command
from solercia_connect.doctor import DoctorService
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.matrix import CAPABILITY_MATRIX
from solercia_connect.oauth import KeyringOAuthTokenStore, OAuthTokenStore
from solercia_connect.serialization import to_json, to_primitive

_PROTECTED_POST_PATHS = frozenset({"/v1/connections/test", "/v1/run"})


class ConnectionTester(Protocol):
    def test_connection(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool = False,
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
    site_url: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    dimensions: list[str] | None = None
    search_type: str | None = None
    data_state: str | None = None
    row_limit: int = Field(default=25_000, ge=1, le=500_000)
    inspection_url: str | None = None
    language_code: str | None = None

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
                ConnectionType.SEARCH_CONSOLE,
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
            **(
                {}
                if self.site_url is None
                else {
                    "site_url": self.site_url,
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                    "dimensions": self.dimensions,
                    "search_type": self.search_type,
                    "data_state": self.data_state,
                    "row_limit": self.row_limit,
                    "inspection_url": self.inspection_url,
                    "language_code": self.language_code,
                }
            ),
            **({} if self.include_content is None else {"include_content": self.include_content}),
        }


def create_app(
    *,
    settings: ConnectSettings | None = None,
    connection_service: ConnectionTester | None = None,
    doctor_service: DoctorService | None = None,
    run_handler: Callable[[dict[str, Any]], ResultEnvelope] | None = None,
    token_store: OAuthTokenStore | None = None,
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
    execute_run = run_handler or (
        lambda payload: run_application(
            payload,
            interactive=False,
            settings=settings,
            token_store=tokens,
        )
    )

    application = FastAPI(
        title="Solercia Connect",
        version=__version__,
        description="Loopback-only, non-interactive adapter over Solercia Connect services.",
    )
    application.state.settings = settings
    application.state.startup_error = startup_error

    @application.middleware("http")
    async def require_local_api_key(request: Request, call_next: Any) -> Any:
        if request.method == "POST" and request.url.path in _PROTECTED_POST_PATHS:
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
                    "interactive_login": False,
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
        if body.allow_browser_fallback:
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
            allow_browser_fallback=False,
        )
        return _response(envelope, _http_status(envelope))

    @application.post("/v1/run")
    def run(body: RunRequest) -> JSONResponse:
        if body.allow_browser_fallback:
            assert body.account is not None
            envelope = _http_action_required(
                "run",
                body.type,
                body.account,
                browser_fallback=True,
            )
            return _response(envelope, 409)
        envelope = execute_run(body.application_payload())
        return _response(envelope, _http_status(envelope))

    return application


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
            ),
            cause_category="interactive_authorization",
        ),
    )


def _http_status(envelope: ResultEnvelope) -> int:
    if envelope.ok:
        return 200
    if envelope.status == "invalid_input":
        return 422
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
