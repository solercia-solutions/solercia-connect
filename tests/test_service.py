from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import solercia_connect.service as service_module
from solercia_connect.config import ConnectionType, load_settings
from solercia_connect.doctor import DoctorCheck, DoctorReport
from solercia_connect.envelope import ErrorDetail, ResultEnvelope
from solercia_connect.interactive_sessions import InteractiveSessionRegistry
from solercia_connect.service import ConnectionConnectRequest, RunRequest, create_app

HEADERS = {"X-Solercia-Connect-Key": "local-api-key"}
CLI_BROWSER_CONNECT = "solercia-connect connect outlook user@example.com --allow-browser-fallback"
HTTP_SWITCH_REMEDIATION = (
    "Set SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true (with SOLERCIA_CONNECT_BROWSER_FALLBACK=true "
    "and an API key) to allow the loopback service to start the supervised browser."
)


class FakeConnections:
    def __init__(self, *, release: threading.Event | None = None) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.browser_calls: list[tuple[str, str]] = []
        self.browser_started = threading.Event()
        self._release = release

    def test_connection(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> ResultEnvelope:
        self.calls.append((type_value, account, allow_browser_fallback))
        return ResultEnvelope.failure(
            operation="connections.test",
            status="action_required",
            provider=type_value,
            account=account,
            error=ErrorDetail(
                "action_required",
                "Interactive login is required",
                True,
                remediation=(f"Run: solercia-connect connect {type_value} {account}",),
                cause_category="interactive_authorization",
            ),
        )

    def connect_browser(self, type_value: str, account: str) -> ResultEnvelope:
        self.browser_calls.append((type_value, account))
        self.browser_started.set()
        if self._release is not None:
            self._release.wait(timeout=5)
        return ResultEnvelope.success(
            operation="connections.connect",
            status="connected_browser",
            provider=type_value,
            account=account,
            route="browser",
            data={"identity_verified": True, "session_status": "connected_browser"},
        )


def _settings(
    tmp_path: Path,
    *,
    api_key: bool = True,
    browser_fallback: bool = False,
    http_interactive: bool = False,
):
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if api_key:
        values["SOLERCIA_CONNECT_API_KEY"] = "local-api-key"
    if browser_fallback:
        values["SOLERCIA_CONNECT_BROWSER_FALLBACK"] = "true"
    if http_interactive:
        values["SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN"] = "true"
    return load_settings(environ=values)


def _interactive_settings(tmp_path: Path):
    return _settings(tmp_path, browser_fallback=True, http_interactive=True)


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        time.sleep(0.005)


def test_health_providers_and_validation_errors_use_the_shared_envelope(tmp_path: Path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))

    health = client.get("/v1/health")
    providers = client.get("/v1/providers")
    invalid = client.post(
        "/v1/connections/test",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={"type": "outlook"},
    )

    assert health.status_code == 200
    assert providers.status_code == 200
    for response in (health, providers, invalid):
        assert response.json()["schema"] == "solercia.connect/v1"
        assert list(response.json()) == [
            "schema",
            "ok",
            "operation",
            "status",
            "provider",
            "account",
            "route",
            "data",
            "receipt",
            "error",
        ]
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_input"


def test_post_endpoints_require_configured_constant_time_local_api_key(tmp_path: Path) -> None:
    configured = TestClient(create_app(settings=_settings(tmp_path)))
    unavailable = TestClient(create_app(settings=_settings(tmp_path, api_key=False)))
    body = {
        "type": "outlook",
        "account": "user@example.com",
        "allow_browser_fallback": False,
    }

    missing = configured.post("/v1/connections/test", json=body)
    wrong = configured.post(
        "/v1/connections/test",
        headers={"X-Solercia-Connect-Key": "wrong"},
        json=body,
    )
    not_configured = unavailable.post("/v1/connections/test", json=body)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert not_configured.status_code == 503
    assert missing.json()["error"]["code"] == "api_authentication_required"
    assert not_configured.json()["error"]["missing_configuration"] == ["SOLERCIA_CONNECT_API_KEY"]
    assert "wrong" not in json.dumps(wrong.json())


def test_connection_test_refuses_browser_fallback_by_default_and_names_both_remediations(
    tmp_path: Path,
) -> None:
    connections = FakeConnections()
    client = TestClient(create_app(settings=_settings(tmp_path), connection_service=connections))

    response = client.post(
        "/v1/connections/test",
        headers=HEADERS,
        json={
            "type": "teams",
            "account": "user@example.com",
            "allow_browser_fallback": True,
        },
    )

    assert response.status_code == 409
    assert connections.calls == []
    assert response.json()["data"]["interactive_flow_started"] is False
    assert response.json()["error"]["remediation"] == [
        "solercia-connect connect teams user@example.com --allow-browser-fallback",
        HTTP_SWITCH_REMEDIATION,
    ]


def test_run_uses_typed_post_body_api_key_and_same_application_result(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def run_handler(payload: dict[str, Any]) -> ResultEnvelope:
        calls.append(payload)
        return ResultEnvelope.success(
            operation="run",
            provider=payload["type"],
            account=payload["account"],
            route="official",
            data={"records": []},
        )

    client = TestClient(create_app(settings=_settings(tmp_path), run_handler=run_handler))
    response = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "google-calendar",
            "account": "user@example.com",
            "capability": "calendar.events.list",
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
            "limit": 10,
        },
    )

    assert response.status_code == 200
    assert response.json()["operation"] == "run"
    assert calls[0]["start"] == "2026-08-01T00:00:00+00:00"
    assert calls[0]["end"] == "2026-09-01T00:00:00+00:00"


def test_service_runs_the_inbox_capability_and_rejects_the_browser_route_by_default(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def run_handler(payload: dict[str, Any]) -> ResultEnvelope:
        calls.append(payload)
        return ResultEnvelope.success(
            operation="run",
            provider=payload["type"],
            account=payload["account"],
            route="official",
            data={"records": []},
        )

    client = TestClient(create_app(settings=_settings(tmp_path), run_handler=run_handler))

    official = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "outlook",
            "account": "user@example.com",
            "capability": "mail.inbox.list",
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
            "limit": 1000,
        },
    )
    browser = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "outlook",
            "account": "user@example.com",
            "capability": "mail.inbox.list",
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
            "allow_browser_fallback": True,
        },
    )

    assert official.status_code == 200
    assert calls[0]["capability"] == "mail.inbox.list"
    assert calls[0]["limit"] == 1000
    assert len(calls) == 1
    assert browser.status_code == 409
    assert browser.json()["error"]["remediation"] == [
        CLI_BROWSER_CONNECT,
        HTTP_SWITCH_REMEDIATION,
    ]


@pytest.mark.parametrize(
    ("api_key", "browser_fallback", "http_interactive", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_health_reports_the_effective_interactive_login_gate(
    tmp_path: Path,
    api_key: bool,
    browser_fallback: bool,
    http_interactive: bool,
    expected: bool,
) -> None:
    settings = _settings(
        tmp_path,
        api_key=api_key,
        browser_fallback=browser_fallback,
        http_interactive=http_interactive,
    )
    client = TestClient(create_app(settings=settings))

    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["data"]["interactive_login"] is expected


def test_connect_endpoint_refuses_when_the_gate_is_off_or_the_body_is_not_a_browser_login(
    tmp_path: Path,
) -> None:
    connections = FakeConnections()
    client = TestClient(
        create_app(
            settings=_settings(tmp_path, browser_fallback=True),
            connection_service=connections,
        )
    )
    body = {"type": "outlook", "account": "user@example.com", "allow_browser_fallback": True}

    gate_off = client.post("/v1/connections/connect", headers=HEADERS, json=body)
    official = client.post(
        "/v1/connections/connect",
        headers=HEADERS,
        json={**body, "allow_browser_fallback": False},
    )
    non_microsoft = client.post(
        "/v1/connections/connect",
        headers=HEADERS,
        json={**body, "type": "gmail"},
    )
    unauthenticated = client.post("/v1/connections/connect", json=body)

    assert gate_off.status_code == 409
    assert gate_off.json()["status"] == "action_required"
    assert gate_off.json()["error"]["code"] == "action_required"
    assert gate_off.json()["error"]["cause_category"] == "interactive_authorization"
    assert gate_off.json()["data"]["interactive_flow_started"] is False
    assert gate_off.json()["error"]["remediation"] == [
        CLI_BROWSER_CONNECT,
        HTTP_SWITCH_REMEDIATION,
    ]
    assert official.status_code == 422
    assert official.json()["error"]["code"] == "invalid_input"
    assert official.json()["error"]["remediation"][0] == (
        "Use the CLI for the official device-code route."
    )
    assert non_microsoft.status_code == 422
    assert non_microsoft.json()["error"]["code"] == "invalid_input"
    assert unauthenticated.status_code == 401
    assert connections.browser_calls == []


def test_connect_endpoint_starts_one_supervised_session_and_reports_its_lifecycle(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    connections = FakeConnections(release=release)
    client = TestClient(
        create_app(
            settings=_interactive_settings(tmp_path),
            connection_service=connections,
            session_registry=InteractiveSessionRegistry(),
        )
    )
    body = {"type": "outlook", "account": "user@example.com", "allow_browser_fallback": True}

    started = client.post("/v1/connections/connect", headers=HEADERS, json=body)

    assert started.status_code == 202
    payload = started.json()
    assert payload["ok"] is True
    assert payload["operation"] == "connections.connect"
    assert payload["status"] == "started"
    assert payload["provider"] == "outlook"
    assert payload["route"] == "browser"
    assert payload["data"]["interactive_flow_started"] is True
    assert payload["data"]["expires_in_seconds"] == 3600
    session_id = payload["data"]["session_id"]
    assert uuid.UUID(session_id).version == 4
    assert connections.browser_started.wait(timeout=5)

    running = client.get(f"/v1/connections/sessions/{session_id}", headers=HEADERS)
    busy = client.post(
        "/v1/connections/connect",
        headers=HEADERS,
        json={**body, "type": "teams"},
    )
    unauthenticated = client.get(f"/v1/connections/sessions/{session_id}")

    assert running.status_code == 200
    assert running.json()["ok"] is True
    assert running.json()["operation"] == "connections.session"
    assert running.json()["status"] == "running"
    assert running.json()["route"] == "browser"
    assert running.json()["data"]["session_id"] == session_id
    assert running.json()["data"]["interactive_flow_started"] is True
    assert "started_at" in running.json()["data"]
    assert busy.status_code == 409
    assert busy.json()["status"] == "busy"
    assert busy.json()["error"]["code"] == "interactive_session_busy"
    assert busy.json()["error"]["retryable"] is True
    assert busy.json()["data"]["session_id"] == session_id
    assert unauthenticated.status_code == 401

    release.set()
    _wait_until(
        lambda: (
            client.get(f"/v1/connections/sessions/{session_id}", headers=HEADERS).json()["status"]
            != "running"
        )
    )
    finished = client.get(f"/v1/connections/sessions/{session_id}", headers=HEADERS)
    unknown = client.get("/v1/connections/sessions/does-not-exist", headers=HEADERS)
    again = client.post("/v1/connections/connect", headers=HEADERS, json=body)

    assert finished.status_code == 200
    assert finished.json()["ok"] is True
    assert finished.json()["operation"] == "connections.session"
    assert finished.json()["status"] == "connected_browser"
    assert finished.json()["route"] == "browser"
    assert finished.json()["data"]["session_id"] == session_id
    assert finished.json()["data"]["identity_verified"] is True
    assert finished.json()["data"]["finished_at"] is not None
    assert finished.json()["error"] is None
    assert unknown.status_code == 404
    assert unknown.json()["status"] == "not_found"
    assert unknown.json()["error"]["code"] == "session_not_found"
    assert again.status_code == 202
    assert again.json()["data"]["session_id"] != session_id
    assert connections.browser_calls[0] == ("outlook", "user@example.com")


def test_finished_session_keeps_the_worker_failure_under_the_session_operation(
    tmp_path: Path,
) -> None:
    def failed_login(type_value: str, account: str) -> ResultEnvelope:
        return ResultEnvelope.failure(
            operation="connections.connect",
            status="failed",
            provider=type_value,
            account=account,
            route="browser",
            error=ErrorDetail(
                "browser_login_required",
                "Browser login did not complete",
                False,
                remediation=(CLI_BROWSER_CONNECT,),
                cause_category="runtime",
            ),
        )

    client = TestClient(
        create_app(
            settings=_interactive_settings(tmp_path),
            connection_service=FakeConnections(),
            browser_connector=failed_login,
        )
    )
    started = client.post(
        "/v1/connections/connect",
        headers=HEADERS,
        json={"type": "outlook", "account": "user@example.com", "allow_browser_fallback": True},
    )
    session_id = started.json()["data"]["session_id"]
    _wait_until(
        lambda: (
            client.get(f"/v1/connections/sessions/{session_id}", headers=HEADERS).json()["status"]
            != "running"
        )
    )

    finished = client.get(f"/v1/connections/sessions/{session_id}", headers=HEADERS)

    assert finished.status_code == 200
    assert finished.json()["ok"] is False
    assert finished.json()["operation"] == "connections.session"
    assert finished.json()["status"] == "failed"
    assert finished.json()["error"]["code"] == "browser_login_required"
    assert finished.json()["data"]["session_id"] == session_id
    assert finished.json()["data"]["finished_at"] is not None


def test_connection_test_with_browser_fallback_follows_the_interactive_gate(tmp_path: Path) -> None:
    off_connections = FakeConnections()
    on_connections = FakeConnections()
    off = TestClient(
        create_app(
            settings=_settings(tmp_path / "off", browser_fallback=True),
            connection_service=off_connections,
        )
    )
    on = TestClient(
        create_app(
            settings=_interactive_settings(tmp_path / "on"),
            connection_service=on_connections,
        )
    )
    body = {"type": "outlook", "account": "user@example.com", "allow_browser_fallback": True}

    refused = off.post("/v1/connections/test", headers=HEADERS, json=body)
    delegated = on.post("/v1/connections/test", headers=HEADERS, json=body)

    assert refused.status_code == 409
    assert refused.json()["error"]["remediation"] == [
        CLI_BROWSER_CONNECT,
        HTTP_SWITCH_REMEDIATION,
    ]
    assert off_connections.calls == []
    assert delegated.status_code == 409
    assert delegated.json()["operation"] == "connections.test"
    assert on_connections.calls == [("outlook", "user@example.com", True)]


def test_run_with_browser_fallback_reaches_the_application_only_when_the_gate_is_on(
    tmp_path: Path,
) -> None:
    calls: list[tuple[dict[str, Any], bool]] = []

    def run_handler(payload: dict[str, Any], *, browser_via_http: bool = False) -> ResultEnvelope:
        calls.append((payload, browser_via_http))
        return ResultEnvelope.success(
            operation="run",
            provider=payload["type"],
            account=payload["account"],
            route="browser",
            data={"records": []},
        )

    off = TestClient(
        create_app(
            settings=_settings(tmp_path / "off", browser_fallback=True),
            run_handler=run_handler,
        )
    )
    on = TestClient(
        create_app(settings=_interactive_settings(tmp_path / "on"), run_handler=run_handler)
    )
    body = {
        "type": "outlook",
        "account": "user@example.com",
        "capability": "mail.inbox.list",
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-09-01T00:00:00Z",
        "allow_browser_fallback": True,
    }

    refused = off.post("/v1/run", headers=HEADERS, json=body)
    calls_after_refusal = list(calls)
    served = on.post("/v1/run", headers=HEADERS, json=body)

    assert refused.status_code == 409
    assert refused.json()["error"]["remediation"] == [
        CLI_BROWSER_CONNECT,
        HTTP_SWITCH_REMEDIATION,
    ]
    assert calls_after_refusal == []
    assert served.status_code == 200
    assert served.json()["route"] == "browser"
    assert len(calls) == 1
    payload, browser_via_http = calls[0]
    assert browser_via_http is True
    assert payload["capability"] == "mail.inbox.list"
    assert payload["allow_browser_fallback"] is True
    assert payload["account"] == "user@example.com"


def test_connect_request_rejects_extra_fields_and_non_microsoft_browser_logins() -> None:
    with pytest.raises(ValidationError, match="Microsoft targets"):
        ConnectionConnectRequest.model_validate(
            {"type": "gmail", "account": "user@example.com", "allow_browser_fallback": True}
        )
    with pytest.raises(ValidationError):
        ConnectionConnectRequest.model_validate(
            {"type": "outlook", "account": "user@example.com", "capability": "mail.inbox.list"}
        )

    request = ConnectionConnectRequest.model_validate(
        {"type": "outlook", "account": "User@Example.com", "allow_browser_fallback": True}
    )
    assert request.account == "user@example.com"


def test_unhandled_service_errors_are_safe_and_openapi_uses_post_bodies(tmp_path: Path) -> None:
    def broken(_: dict[str, Any]) -> ResultEnvelope:
        raise RuntimeError("internal sensitive detail")

    client = TestClient(create_app(settings=_settings(tmp_path), run_handler=broken))
    response = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "gmail",
            "account": "user@example.com",
            "capability": "mail.sent.list",
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
        },
    )
    schema = client.get("/openapi.json").json()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "unknown_error"
    assert "internal sensitive detail" not in response.text
    assert "requestBody" in schema["paths"]["/v1/run"]["post"]
    assert "get" not in schema["paths"]["/v1/run"]


class FakeDoctor:
    def __init__(self, report: DoctorReport | Exception) -> None:
        self.report = report

    def inspect(self, type_value: ConnectionType, account: str | None) -> DoctorReport:
        del type_value, account
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def _doctor_report(*, ready: bool) -> DoctorReport:
    return DoctorReport(
        ConnectionType.OUTLOOK,
        "user@example.com",
        ready,
        () if ready else ("SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID",),
        ("mail.sent.list",),
        ("Mail.Read",),
        ("Tenant consent",),
        (
            DoctorCheck(
                "configuration",
                "ready" if ready else "missing",
                "safe detail",
                True,
            ),
        ),
        "127.0.0.1",
        8765,
        True,
    )


def test_doctor_endpoint_maps_ready_not_ready_invalid_and_path_validation(tmp_path: Path) -> None:
    ready = TestClient(
        create_app(
            settings=_settings(tmp_path),
            doctor_service=FakeDoctor(_doctor_report(ready=True)),  # type: ignore[arg-type]
        )
    )
    not_ready = TestClient(
        create_app(
            settings=_settings(tmp_path),
            doctor_service=FakeDoctor(_doctor_report(ready=False)),  # type: ignore[arg-type]
        )
    )
    invalid = TestClient(
        create_app(
            settings=_settings(tmp_path),
            doctor_service=FakeDoctor(ValueError("invalid account")),  # type: ignore[arg-type]
        )
    )

    ready_response = ready.get("/v1/doctor/outlook?account=user@example.com")
    missing_response = not_ready.get("/v1/doctor/outlook?account=user@example.com")
    invalid_response = invalid.get("/v1/doctor/outlook?account=user@example.com")
    path_response = ready.get("/v1/doctor/unknown")

    assert ready_response.status_code == 200
    assert ready_response.json()["status"] == "ready"
    assert missing_response.status_code == 503
    assert missing_response.json()["error"]["missing_configuration"] == [
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"
    ]
    assert invalid_response.status_code == 422
    assert invalid_response.json()["error"]["code"] == "invalid_input"
    assert path_response.status_code == 422
    assert path_response.json()["operation"] == "http.validation"


@pytest.mark.parametrize(
    "changes",
    [
        {"capability": " "},
        {"calendar_id": " "},
        {"end": None},
        {"start": "2026-08-01T00:00:00", "end": "2026-09-01T00:00:00"},
        {"start": "2026-09-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
    ],
)
def test_run_request_rejects_empty_or_incoherent_typed_values(changes: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "type": "gmail",
        "account": "user@example.com",
        "capability": "mail.sent.list",
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-09-01T00:00:00Z",
        **changes,
    }
    with pytest.raises(ValidationError):
        RunRequest.model_validate(values)


def test_run_request_requires_account_for_google_meet() -> None:
    with pytest.raises(ValidationError, match="account is required"):
        RunRequest.model_validate(
            {
                "type": "google-meet",
                "capability": "meeting.transcript.get",
                "meeting_id": "meeting-1",
            }
        )


def test_run_request_accepts_google_meet_browser_fallback() -> None:
    """Google Meet has a browser route (decision 2026-10-25, D1 as made precise on 2026-10-26)."""
    request = RunRequest.model_validate(
        {
            "type": "google-meet",
            "account": "user@example.com",
            "capability": "meeting.transcripts.list",
            "start": "2026-10-26T00:00:00-05:00",
            "end": "2026-10-27T00:00:00-05:00",
            "allow_browser_fallback": True,
        }
    )

    assert request.allow_browser_fallback is True


def test_run_request_still_rejects_gmail_browser_fallback() -> None:
    with pytest.raises(ValidationError, match="Microsoft targets and Google Meet"):
        RunRequest.model_validate(
            {
                "type": "gmail",
                "account": "user@example.com",
                "capability": "mail.sent.list",
                "start": "2026-10-26T00:00:00-05:00",
                "end": "2026-10-27T00:00:00-05:00",
                "allow_browser_fallback": True,
            }
        )


def test_run_request_payload_preserves_safe_optional_fields() -> None:
    request = RunRequest.model_validate(
        {
            "type": "teams",
            "account": "User@Example.com",
            "capability": "meeting.transcript.get",
            "meeting_id": "meeting-1",
            "allow_browser_fallback": True,
        }
    )

    payload = request.application_payload()
    assert payload["account"] == "user@example.com"
    assert payload["start"] is None and payload["end"] is None
    assert payload["meeting_id"] == "meeting-1"
    assert payload["allow_browser_fallback"] is True

    jira = RunRequest.model_validate(
        {
            "type": "jira",
            "capability": "external.issues.search",
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-09-01T00:00:00Z",
        }
    )
    assert jira.application_payload()["account"] is None


def test_health_reports_safe_startup_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = _settings(tmp_path)
    calls = 0

    def fake_load_settings(*, environ: dict[str, str] | None = None):
        nonlocal calls
        calls += 1
        if environ is None:
            raise ValueError("loopback configuration is invalid")
        return fallback

    monkeypatch.setattr(service_module, "load_settings", fake_load_settings)
    client = TestClient(create_app())
    response = client.get("/v1/health")

    assert calls == 2
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "configuration_invalid"


@pytest.mark.parametrize(
    "envelope,expected",
    [
        (ResultEnvelope.success(operation="x"), 200),
        (
            ResultEnvelope.failure(
                operation="x",
                status="invalid_input",
                error=ErrorDetail("invalid_input", "bad", False),
            ),
            422,
        ),
        (
            ResultEnvelope.failure(
                operation="x",
                status="action_required",
                error=ErrorDetail("action_required", "act", True),
            ),
            409,
        ),
        (
            ResultEnvelope.failure(
                operation="x",
                status="failed",
                error=ErrorDetail("authorization_denied", "denied", False),
            ),
            403,
        ),
        (
            ResultEnvelope.failure(
                operation="x",
                status="failed",
                error=ErrorDetail("network_timeout", "retry", True),
            ),
            503,
        ),
        (
            ResultEnvelope.failure(
                operation="x",
                status="failed",
                error=ErrorDetail("request_rejected", "failed", False),
            ),
            502,
        ),
    ],
)
def test_http_status_mapping_is_stable(envelope: ResultEnvelope, expected: int) -> None:
    assert service_module._http_status(envelope) == expected


def test_service_main_is_safe_for_invalid_config_missing_extra_and_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        service_module,
        "load_settings",
        lambda: (_ for _ in ()).throw(ValueError("invalid host")),
    )
    with pytest.raises(SystemExit) as invalid_exit:
        service_module.main()
    assert invalid_exit.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "configuration_invalid"

    settings = _settings(tmp_path)
    monkeypatch.setattr(service_module, "load_settings", lambda: settings)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    with pytest.raises(SystemExit) as missing_exit:
        service_module.main()
    assert missing_exit.value.code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "optional_dependency_missing"

    calls: list[dict[str, Any]] = []
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    service_module.main()
    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 8765,
            "log_config": None,
        }
    ]
