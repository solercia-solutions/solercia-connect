from __future__ import annotations

import json
import sys
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
from solercia_connect.service import RunRequest, create_app


class FakeConnections:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

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


def _settings(tmp_path: Path, *, api_key: bool = True):
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    if api_key:
        values["SOLERCIA_CONNECT_API_KEY"] = "local-api-key"
    return load_settings(environ=values)


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


def test_connection_test_never_starts_interactive_login_and_returns_exact_cli_action(
    tmp_path: Path,
) -> None:
    connections = FakeConnections()
    client = TestClient(create_app(settings=_settings(tmp_path), connection_service=connections))

    response = client.post(
        "/v1/connections/test",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "teams",
            "account": "user@example.com",
            "allow_browser_fallback": True,
        },
    )

    assert response.status_code == 409
    assert connections.calls == []
    assert response.json()["error"]["remediation"] == [
        "solercia-connect connect teams user@example.com --allow-browser-fallback"
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


def test_service_runs_the_inbox_capability_and_rejects_the_browser_route(tmp_path: Path) -> None:
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
    assert browser.status_code == 409
    assert browser.json()["error"]["remediation"] == [
        "solercia-connect connect outlook user@example.com --allow-browser-fallback"
    ]


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
