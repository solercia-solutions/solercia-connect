from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import solercia_connect.doctor as doctor_module
from solercia_connect.browser_profile import BrowserProfilePathAudit
from solercia_connect.config import ProfileStore, load_settings
from solercia_connect.doctor import DoctorService


def test_doctor_reports_missing_names_and_external_prerequisites_without_values(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: name == "keyring",
        keyring_probe=lambda: (False, "unavailable"),
        browser_probe=lambda: (False, "Chromium executable is not installed"),
    )

    report = service.inspect("teams", "user@example.com")
    payload = report.to_dict()

    assert payload["ready"] is False
    assert payload["missing_configuration"] == ["SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"]
    assert "OnlineMeetingTranscript.Read.All" in payload["oauth_scopes"]
    assert any("personal accounts" in item for item in payload["external_prerequisites"])
    assert "user@example.com" not in str(payload["checks"])


def test_doctor_default_browser_profile_is_safe_but_route_needs_browser_runtime(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "client-id",
        }
    )
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not installed"),
    )

    report = service.inspect("outlook", "user@example.com").to_dict()
    profile = next(check for check in report["checks"] if check["name"] == "browser_profile")
    browser = next(
        check for check in report["checks"] if check["name"] == "microsoft_browser_route"
    )

    assert profile["status"] == "ready"
    assert browser["status"] == "disabled"
    assert report["ready"] is True


def test_doctor_is_ready_for_browser_only_outlook_and_marks_official_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
        }
    )
    audited: list[tuple[Path, Path | None]] = []

    def audit(profile_dir: Path, *, private_root: Path | None = None):
        audited.append((profile_dir, private_root))
        return BrowserProfilePathAudit(profile_dir, False)

    def module_available(name: str) -> bool:
        if name == "keyring":
            raise AssertionError("Browser-only readiness must not require keyring")
        return True

    monkeypatch.setattr(doctor_module, "audit_browser_profile_path", audit)
    report = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=module_available,
        keyring_probe=lambda: pytest.fail("Browser-only readiness must not probe keyring"),
        browser_probe=lambda: (True, "Chromium ready"),
    ).inspect("outlook", "user@example.com")
    payload = report.to_dict()
    checks = {check["name"]: check for check in payload["checks"]}
    root = settings.data_dir / "m365"
    digest = hashlib.sha256(b"user@example.com").hexdigest()[:16]

    assert report.ready is True
    assert payload["missing_configuration"] == ["SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID"]
    assert checks["microsoft_official_route"]["status"] == "unavailable"
    assert checks["microsoft_browser_route"]["status"] == "ready"
    assert checks["keyring_package"]["status"] == "not_required"
    assert checks["keyring_backend"]["status"] == "not_required"
    assert audited == [(root / digest, root)]


def test_doctor_is_not_ready_when_neither_microsoft_route_is_usable(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
    )
    report = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: name != "keyring",
        keyring_probe=lambda: pytest.fail("Unavailable official route must not probe keyring"),
        browser_probe=lambda: pytest.fail("Disabled browser route must not probe Chromium"),
    ).inspect("outlook", "user@example.com")
    checks = {check.name: check for check in report.checks}

    assert report.ready is False
    assert checks["microsoft_official_route"].status == "unavailable"
    assert checks["microsoft_browser_route"].status == "disabled"


def test_doctor_rejects_unsafe_explicit_browser_profile_before_runtime_probe(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "m365"
    profile_root.mkdir(mode=0o755)
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_BROWSER_FALLBACK": "true",
            "SOLERCIA_CONNECT_M365_PROFILE_DIR": str(profile_root),
        }
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
        keyring_probe=lambda: pytest.fail("Browser-only readiness must not probe keyring"),
        browser_probe=browser_probe,
    ).inspect("outlook", "user@example.com")
    checks = {check.name: check for check in report.checks}

    assert report.ready is False
    assert checks["browser_profile"].status == "unsafe"
    assert checks["microsoft_browser_route"].status == "unsafe"
    assert browser_probe_calls == 0


def test_doctor_reports_loopback_api_security_and_profile_state(tmp_path: Path) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_GOOGLE_CLIENT_ID": "client-id",
            "SOLERCIA_CONNECT_API_KEY": "local-test-key",
        }
    )
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not applicable"),
    )

    report = service.inspect("gmail", "user@example.com").to_dict()
    api_security = next(check for check in report["checks"] if check["name"] == "api_security")

    assert report["ready"] is True
    assert api_security["status"] == "ready"
    assert report["service"]["host"] == "127.0.0.1"
    assert report["service"]["api_key_configured"] is True
    assert "local-test-key" not in str(report)


def test_doctor_advertises_the_inbox_capability_for_both_outlook_routes(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID": "client-id",
        }
    )
    service = DoctorService(
        settings,
        ProfileStore.from_settings(settings),
        module_available=lambda name: True,
        keyring_probe=lambda: (True, "test-backend"),
        browser_probe=lambda: (False, "not installed"),
    )

    payload = service.inspect("outlook", "user@example.com").to_dict()

    assert payload["capabilities"] == [
        "mail.sent.list",
        "mail.inbox.list",
        "calendar.events.list",
    ]
