from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import solercia_connect.application as application_module
import solercia_connect.bootstrap as bootstrap_module
import solercia_connect.output as output_module
import solercia_connect.service as service_module
from solercia_connect.cli import app
from solercia_connect.config import ConnectionProfile, ProfileStore, load_settings
from solercia_connect.connections import ConnectionService
from solercia_connect.domain import Capability, HealthReport, HealthState, ReasonCode
from solercia_connect.errors import ConnectorFailure
from solercia_connect.output import SecureOutputError, write_secure_json
from solercia_connect.providers.m365_browser.driver import (
    PlaywrightBrowserDriver,
    validate_dedicated_profile,
)
from solercia_connect.service import create_app
from tests.fakes import NOW
from tests.test_connections import MemoryTokenStore

runner = CliRunner()


def _settings(tmp_path: Path, **extra: str):
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_API_KEY": "local-api-key",
        **extra,
    }
    return load_settings(environ=values)


def _run_body(*, allow_browser_fallback: bool = False) -> dict[str, Any]:
    return {
        "type": "outlook",
        "account": "user@example.com",
        "capability": "mail.sent.list",
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-09-01T00:00:00Z",
        "allow_browser_fallback": allow_browser_fallback,
    }


class _BrowserCallSentinelConnections:
    def __init__(self) -> None:
        self.calls = 0

    def test_connection(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> Any:
        del type_value, account, allow_browser_fallback
        self.calls += 1
        raise AssertionError("HTTP must reject browser routing before the service call")


def test_http_rejects_browser_fallback_before_connection_service_invocation(
    tmp_path: Path,
) -> None:
    sentinel = _BrowserCallSentinelConnections()
    client = TestClient(create_app(settings=_settings(tmp_path), connection_service=sentinel))

    response = client.post(
        "/v1/connections/test",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "outlook",
            "account": "user@example.com",
            "allow_browser_fallback": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["status"] == "action_required"
    assert response.json()["error"]["remediation"] == [
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
        service_module._HTTP_INTERACTIVE_REMEDIATION,
    ]
    assert sentinel.calls == 0


def test_http_rejects_browser_fallback_before_run_application_invocation(tmp_path: Path) -> None:
    calls = 0

    def browser_call_sentinel(_: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP must reject browser routing before the run service call")

    client = TestClient(create_app(settings=_settings(tmp_path), run_handler=browser_call_sentinel))
    response = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json=_run_body(allow_browser_fallback=True),
    )

    assert response.status_code == 409
    assert response.json()["status"] == "action_required"
    assert response.json()["error"]["remediation"] == [
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
        service_module._HTTP_INTERACTIVE_REMEDIATION,
    ]
    assert calls == 0


def test_noninteractive_application_rejects_browser_route_before_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_calls = 0

    def browser_registry_sentinel(**_: object) -> Any:
        nonlocal registry_calls
        registry_calls += 1
        raise AssertionError("No registry or browser adapter may be built")

    monkeypatch.setattr(application_module, "registry_from_environment", browser_registry_sentinel)
    result = application_module.run_application(
        _run_body(allow_browser_fallback=True),
        interactive=False,
        settings=_settings(
            tmp_path,
            SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
        ),
    )

    assert result.status == "action_required"
    assert result.error is not None
    assert result.error.remediation == (
        "solercia-connect connect outlook user@example.com --allow-browser-fallback",
    )
    assert registry_calls == 0


def test_http_missing_oauth_profile_is_stable_action_required_before_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_calls = 0

    def registry_sentinel(**_: object) -> Any:
        nonlocal registry_calls
        registry_calls += 1
        raise AssertionError("Missing local OAuth state must be handled before runtime")

    monkeypatch.setattr(application_module, "registry_from_environment", registry_sentinel)
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
    )
    client = TestClient(create_app(settings=settings))

    run_response = client.post(
        "/v1/run",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json=_run_body(),
    )
    test_response = client.post(
        "/v1/connections/test",
        headers={"X-Solercia-Connect-Key": "local-api-key"},
        json={
            "type": "outlook",
            "account": "user@example.com",
            "allow_browser_fallback": False,
        },
    )

    for response in (run_response, test_response):
        assert response.status_code == 409
        assert response.json()["schema"] == "solercia.connect/v1"
        assert response.json()["status"] == "action_required"
        assert response.json()["error"]["remediation"] == [
            "solercia-connect connect outlook user@example.com"
        ]
    assert registry_calls == 0


def test_noninteractive_browser_profile_returns_action_before_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
    )
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("outlook", "user@example.com", route="browser")
    )
    calls = 0

    def registry_sentinel(**_: object) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("HTTP browser profile must stop before registry construction")

    monkeypatch.setattr(application_module, "registry_from_environment", registry_sentinel)
    result = application_module.run_application(
        _run_body(),
        interactive=False,
        settings=settings,
        token_store=MemoryTokenStore(),
    )

    assert result.status == "action_required"
    assert result.data["interactive_flow_started"] is False
    assert calls == 0


def test_noninteractive_missing_oauth_token_is_action_required_before_provider_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
    )
    ProfileStore.from_settings(settings).save(
        ConnectionProfile.create("outlook", "user@example.com")
    )
    registry_calls = 0

    def registry_sentinel(**_: object) -> Any:
        nonlocal registry_calls
        registry_calls += 1
        raise AssertionError("Missing OAuth token must stop before provider runtime")

    monkeypatch.setattr(application_module, "registry_from_environment", registry_sentinel)

    result = application_module.run_application(
        _run_body(),
        interactive=False,
        settings=settings,
        token_store=MemoryTokenStore(),
    )

    assert result.status == "action_required"
    assert result.error is not None
    assert result.error.remediation == ("solercia-connect connect outlook user@example.com",)
    assert registry_calls == 0


@pytest.mark.parametrize(
    "supplied", [b"", b"wrong", "caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode(), b"\xff"]
)
def test_api_key_rejections_are_uniform_non_leaking_401(tmp_path: Path, supplied: bytes) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))
    headers = [(b"X-Solercia-Connect-Key", supplied)] if supplied else []

    response = client.post(
        "/v1/connections/test",
        headers=headers,
        json={"type": "outlook", "account": "user@example.com"},
    )

    assert response.status_code == 401
    payload = response.json()
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["error"]["code"] == "api_authentication_required"
    if supplied:
        assert supplied.hex() not in json.dumps(payload)


def test_all_invalid_api_key_shapes_return_the_same_envelope(tmp_path: Path) -> None:
    client = TestClient(create_app(settings=_settings(tmp_path)))
    responses = [
        client.post(
            "/v1/connections/test",
            headers=[] if supplied is None else [(b"X-Solercia-Connect-Key", supplied)],
            json={"type": "outlook", "account": "user@example.com"},
        )
        for supplied in (None, b"", b"wrong", b"caf\xc3\xa9", b"\xff")
    ]

    assert {response.status_code for response in responses} == {401}
    assert all(response.json() == responses[0].json() for response in responses[1:])


class _AllowlistedOAuthFailure:
    def start_device_authorization(self) -> Any:
        raise ConnectorFailure(
            ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
            "Official route cannot proceed",
        )

    def authorization_header(self) -> str:
        raise AssertionError("authorization_header must not be reached")


def test_global_browser_fallback_never_authorizes_an_invocation(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
        SOLERCIA_CONNECT_BROWSER_FALLBACK="true",
    )
    browser_calls: list[str] = []
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        MemoryTokenStore(),
        oauth_builder=lambda type_value, account: _AllowlistedOAuthFailure(),
        browser_connector=lambda type_value, account: browser_calls.append(account) or account,
    )

    result = service.connect(
        "outlook",
        "user@example.com",
        allow_browser_fallback=False,
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "official_capability_unavailable"
    assert browser_calls == []


def test_browser_profile_status_requires_explicit_invocation_flag(tmp_path: Path) -> None:
    settings = _settings(tmp_path, SOLERCIA_CONNECT_BROWSER_FALLBACK="true")
    profiles = ProfileStore.from_settings(settings)
    profiles.save(
        ConnectionProfile.create(
            "teams",
            "user@example.com",
            route="browser",
            now=NOW,
        )
    )
    calls: list[str] = []
    service = ConnectionService(
        settings,
        profiles,
        MemoryTokenStore(),
        browser_status=lambda type_value, account: calls.append(account) or account,
        clock=lambda: NOW,
    )

    denied = service.status("teams", "user@example.com")
    allowed = service.status(
        "teams",
        "user@example.com",
        allow_browser_fallback=True,
    )

    assert denied.status == "action_required"
    assert denied.error is not None
    assert denied.error.remediation == (
        "solercia-connect status teams user@example.com --allow-browser-fallback",
    )
    assert allowed.status == "connected_browser"
    assert calls == ["user@example.com"]


def test_m365_compatibility_commands_require_explicit_browser_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    report = HealthReport(
        "microsoft365.browser",
        HealthState.READY,
        ReasonCode.SUCCESS,
        (Capability.MAIL_SENT_LIST,),
        "user@example.com",
        "ready",
    )
    adapter = SimpleNamespace(
        interactive_login=lambda: calls.append("login") or report,
        health=lambda: calls.append("status") or report,
    )
    monkeypatch.setattr(
        "solercia_connect.cli.browser_adapter_from_environment",
        lambda **_: adapter,
    )

    denied_login = runner.invoke(app, ["m365", "login"])
    denied_status = runner.invoke(app, ["m365", "status"])
    allowed_login = runner.invoke(
        app,
        ["m365", "login", "--allow-browser-fallback"],
    )
    allowed_status = runner.invoke(
        app,
        ["m365", "status", "--allow-browser-fallback"],
    )

    assert denied_login.exit_code == denied_status.exit_code == 1
    assert json.loads(denied_login.stdout)["status"] == "action_required"
    assert json.loads(denied_status.stdout)["status"] == "action_required"
    assert allowed_login.exit_code == allowed_status.exit_code == 0
    assert calls == ["login", "status"]


def _install_m365_alias_browser_sentinels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    global_fallback_enabled: bool,
) -> dict[str, int]:
    calls = {"driver": 0, "adapter": 0, "login": 0, "health": 0}
    report = HealthReport(
        "microsoft365.browser",
        HealthState.READY,
        ReasonCode.SUCCESS,
        (Capability.MAIL_SENT_LIST,),
        "user@example.com",
        "ready",
    )

    def construct_driver() -> object:
        calls["driver"] += 1
        return object()

    def run(operation: str) -> HealthReport:
        calls[operation] += 1
        return report

    adapter = SimpleNamespace(
        interactive_login=lambda: run("login"),
        health=lambda: run("health"),
    )

    def construct_adapter(_: object, **__: object) -> object:
        calls["adapter"] += 1
        return adapter

    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "SOLERCIA_CONNECT_BROWSER_FALLBACK": ("true" if global_fallback_enabled else "false"),
    }
    monkeypatch.setattr(bootstrap_module, "effective_environment", lambda **_: values)
    monkeypatch.setattr(bootstrap_module, "PlaywrightBrowserDriver", construct_driver)
    monkeypatch.setattr(bootstrap_module, "Microsoft365BrowserAdapter", construct_adapter)
    return calls


def test_m365_login_with_invocation_gate_and_global_disabled_stops_before_browser_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_m365_alias_browser_sentinels(
        tmp_path,
        monkeypatch,
        global_fallback_enabled=False,
    )

    result = runner.invoke(app, ["m365", "login", "--allow-browser-fallback"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "configuration_invalid"
    assert payload["error"]["remediation"] == [
        "Run solercia-connect doctor for structured remediation."
    ]
    assert calls == {"driver": 0, "adapter": 0, "login": 0, "health": 0}


def test_m365_status_with_invocation_gate_and_global_disabled_stops_before_browser_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_m365_alias_browser_sentinels(
        tmp_path,
        monkeypatch,
        global_fallback_enabled=False,
    )

    result = runner.invoke(app, ["m365", "status", "--allow-browser-fallback"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 1
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "configuration_invalid"
    assert payload["error"]["remediation"] == [
        "Run solercia-connect doctor for structured remediation."
    ]
    assert calls == {"driver": 0, "adapter": 0, "login": 0, "health": 0}


@pytest.mark.parametrize(
    ("operation", "expected_call"),
    [
        pytest.param("login", "login", id="login"),
        pytest.param("status", "health", id="status"),
    ],
)
def test_m365_aliases_with_both_fallback_gates_construct_and_run_browser_route(
    operation: str,
    expected_call: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_m365_alias_browser_sentinels(
        tmp_path,
        monkeypatch,
        global_fallback_enabled=True,
    )

    result = runner.invoke(app, ["m365", operation, "--allow-browser-fallback"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["status"] == "connected_browser"
    assert payload["route"] == "browser"
    assert calls["driver"] == 1
    assert calls["adapter"] == 1
    assert calls[expected_call] == 1
    assert sum(calls.values()) == 3


def test_browser_health_requires_explicit_browser_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def registry_sentinel(**_: object) -> Any:
        nonlocal calls
        calls += 1
        raise AssertionError("Browser registry must not be built without the explicit flag")

    monkeypatch.setattr(
        "solercia_connect.cli.registry_from_environment",
        registry_sentinel,
    )

    denied = runner.invoke(app, ["health", "microsoft365.browser"])

    assert denied.exit_code == 1
    assert json.loads(denied.stdout)["status"] == "action_required"
    assert calls == 0


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path.resolve(strict=True)
    root.chmod(0o700)
    return root


def test_browser_profile_rejects_final_and_ancestor_symlinks(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    final_link = root / "final-link"
    final_link.symlink_to(outside, target_is_directory=True)
    ancestor_link = root / "ancestor-link"
    ancestor_link.symlink_to(outside, target_is_directory=True)

    for candidate in (final_link, ancestor_link / "profile"):
        with pytest.raises(ConnectorFailure) as captured:
            validate_dedicated_profile(candidate)
        assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID


def test_browser_profile_rejects_insecure_controlled_ancestor_and_final_modes(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    insecure_root = root / "profiles"
    insecure_root.mkdir(mode=0o755)
    insecure_final = root / "final"
    insecure_final.mkdir(mode=0o755)

    for candidate in (insecure_root / "account", insecure_final):
        with pytest.raises(ConnectorFailure) as captured:
            validate_dedicated_profile(candidate)
        assert captured.value.reason is ReasonCode.CONFIGURATION_INVALID


def test_browser_driver_never_chmods_a_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    target = root / "target"
    target.mkdir(mode=0o755)
    alias = root / "profile"
    alias.symlink_to(target, target_is_directory=True)
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> Any:
        if name.startswith("playwright"):
            raise AssertionError("Playwright import proves filesystem validation was bypassed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(ConnectorFailure):
        PlaywrightBrowserDriver().open_persistent(alias, headless=True)

    assert target.stat().st_mode & 0o777 == 0o755


def test_browser_profile_ready_path_is_accepted_by_driver_and_doctor_contract(
    tmp_path: Path,
) -> None:
    from solercia_connect.doctor import DoctorService

    root = _private_root(tmp_path)
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    validate_dedicated_profile(profile)
    settings = _settings(
        root,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
        SOLERCIA_CONNECT_M365_PROFILE_DIR=str(profile),
    )
    doctor = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test backend"),
        browser_probe=lambda: (True, "Chromium ready"),
    )

    report = doctor.inspect("outlook", "user@example.com").to_dict()
    check = next(item for item in report["checks"] if item["name"] == "browser_profile")

    assert check["status"] == "ready"


@pytest.mark.parametrize("kind", ["final_symlink", "ancestor_symlink", "public_mode"])
def test_doctor_uses_shared_browser_path_rejections(
    kind: str,
    tmp_path: Path,
) -> None:
    from solercia_connect.doctor import DoctorService

    root = _private_root(tmp_path)
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    if kind == "final_symlink":
        profile = root / "profile"
        profile.symlink_to(outside, target_is_directory=True)
    elif kind == "ancestor_symlink":
        ancestor = root / "alias"
        ancestor.symlink_to(outside, target_is_directory=True)
        profile = ancestor / "profile"
    else:
        profile = root / "profile"
        profile.mkdir(mode=0o755)
    settings = _settings(
        root,
        SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID="microsoft-client",
        SOLERCIA_CONNECT_M365_PROFILE_DIR=str(profile),
    )
    browser_probe_calls = 0

    def browser_probe() -> tuple[bool, str]:
        nonlocal browser_probe_calls
        browser_probe_calls += 1
        return True, "must not run"

    report = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test backend"),
        browser_probe=browser_probe,
    ).inspect("outlook", "user@example.com")
    check = next(item for item in report.checks if item.name == "browser_profile")

    assert check.status == "unsafe"
    assert browser_probe_calls == 0


def test_doctor_and_driver_reference_one_shared_browser_path_auditor() -> None:
    root = Path(__file__).parents[1] / "src/solercia_connect"
    doctor_source = (root / "doctor.py").read_text()
    driver_source = (root / "providers/m365_browser/driver.py").read_text()

    assert "audit_browser_profile_path" in doctor_source
    assert "audit_browser_profile_path" in driver_source


def test_profile_reads_refuse_public_directory_file_and_non_regular_file(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    store = ProfileStore(root / "profiles")
    profile = ConnectionProfile.create("outlook", "user@example.com")
    store.save(profile)
    path = store.path_for("outlook", "user@example.com")

    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.get("outlook", "user@example.com")

    path.chmod(0o600)
    store.root.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        store.get("outlook", "user@example.com")

    store.root.chmod(0o700)
    path.unlink()
    path.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="regular"):
        store.get("outlook", "user@example.com")


def test_profile_reads_refuse_a_symlink_without_following_it(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    store = ProfileStore(root / "profiles")
    profile = ConnectionProfile.create("gmail", "user@example.com")
    store.save(profile)
    path = store.path_for("gmail", "user@example.com")
    outside = root / "outside.json"
    outside.write_text(path.read_text())
    outside.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        store.get("gmail", "user@example.com")

    assert outside.is_file()


def _fake_registry() -> Any:
    return SimpleNamespace(descriptors=lambda: ())


def test_output_os_failures_are_json_exit_one_with_stdout_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("solercia_connect.cli.registry_from_environment", lambda: _fake_registry())
    monkeypatch.setattr(
        output_module.tempfile,
        "mkstemp",
        lambda **kwargs: (_ for _ in ()).throw(PermissionError("private path detail")),
    )
    target = tmp_path / "output" / "result.json"

    result = runner.invoke(app, ["providers", "--output", str(target)], env={})

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["error"]["code"] == "output_error"
    assert "private path detail" not in result.stdout
    assert not target.exists()


def test_unwritable_output_parent_falls_back_to_stdout_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("solercia_connect.cli.registry_from_environment", lambda: _fake_registry())
    parent = _private_root(tmp_path) / "locked"
    parent.mkdir(mode=0o700)
    parent.chmod(0o500)
    try:
        result = runner.invoke(
            app,
            ["providers", "--output", str(parent / "result.json")],
            env={},
        )
    finally:
        parent.chmod(0o700)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "output_error"
    assert not (parent / "result.json").exists()


def test_output_final_and_ancestor_symlinks_return_json_without_partial_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("solercia_connect.cli.registry_from_environment", lambda: _fake_registry())
    root = _private_root(tmp_path)
    outside = root / "outside"
    outside.mkdir(mode=0o700)
    final_target = outside / "existing.json"
    final_target.write_text("keep")
    final_link = root / "result.json"
    final_link.symlink_to(final_target)
    parent_link = root / "parent"
    parent_link.symlink_to(outside, target_is_directory=True)

    for target in (final_link, parent_link / "result.json"):
        result = runner.invoke(
            app,
            ["providers", "--output", str(target), "--force"],
            env={},
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "output_error"

    assert final_target.read_text() == "keep"
    assert not (outside / "result.json").exists()


def test_output_atomic_failure_cleans_temporary_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    target = root / "result.json"
    monkeypatch.setattr(
        output_module.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("disk failure")),
    )

    with pytest.raises(SecureOutputError):
        write_secure_json(target, {"ok": True})

    assert not target.exists()
    assert list(root.glob(".solercia-connect-*")) == []


def test_packaging_contract_locks_build_tool_and_exposes_offline_build_target() -> None:
    root = Path(__file__).parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    makefile = (root / "Makefile").read_text()

    assert '"build>=' in pyproject
    assert '"hatchling>=' in pyproject
    assert "build-offline:" in makefile
    assert "--offline" in makefile
    assert "--no-isolation" in makefile or "--no-build-isolation" in makefile


def test_docs_state_corrected_http_fallback_profile_live_and_packaging_boundaries() -> None:
    root = Path(__file__).parents[1]
    docs = "\n".join(
        (root / path).read_text()
        for path in (
            "README.md",
            "SECURITY.md",
            "docs/ARCHITECTURE.md",
            "docs/runbooks/api.md",
            "docs/ROADMAP.md",
        )
    ).casefold()

    for statement in (
        "http never opens or invokes a browser",
        "every browser attempt requires --allow-browser-fallback",
        "profile reads never repair unsafe permissions",
        "live token-store readiness",
        "subsequent build is offline",
        "vendored wheelhouse",
    ):
        assert statement in docs
