from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.config import ConnectionProfile, ConnectionType, ProfileStore, load_settings
from solercia_connect.doctor import DoctorCheck, DoctorReport
from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    FileQuery,
    HealthReport,
    HealthState,
    IssueQuery,
    MailQuery,
    ProviderFamily,
    QueryPayload,
    ReasonCode,
    RouteKind,
    TicketQuery,
    TranscriptQuery,
)
from solercia_connect.envelope import ResultEnvelope
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import ProviderDescriptor

_EXPECTED_QUERY_TYPES: dict[Capability, type[QueryPayload]] = {
    Capability.MAIL_SENT_LIST: MailQuery,
    Capability.MAIL_INBOX_LIST: MailQuery,
    Capability.CALENDAR_EVENTS_LIST: CalendarQuery,
    Capability.MEETING_TRANSCRIPTS_LIST: TranscriptQuery,
    Capability.MEETING_TRANSCRIPT_GET: TranscriptQuery,
    Capability.FILES_SEARCH: FileQuery,
    Capability.FILES_GET: FileQuery,
    Capability.EXTERNAL_ISSUES_SEARCH: IssueQuery,
    Capability.TICKETS_SEARCH: TicketQuery,
}

runner = CliRunner()


class FakeConnectionService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def connect(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool,
        open_browser: bool,
    ) -> ResultEnvelope:
        self.calls.append(("connect", type_value, account, allow_browser_fallback, open_browser))
        return ResultEnvelope.success(
            operation="connect",
            status="connected_official",
            provider=type_value,
            account=account,
            route="official",
        )

    def status(
        self,
        type_value: str,
        account: str,
        *,
        allow_browser_fallback: bool = False,
    ) -> ResultEnvelope:
        self.calls.append(("status", type_value, account, allow_browser_fallback))
        return ResultEnvelope.success(
            operation="status",
            status="connected_official",
            provider=type_value,
            account=account,
            route="official",
        )

    def disconnect(self, type_value: str, account: str) -> ResultEnvelope:
        self.calls.append(("disconnect", type_value, account))
        return ResultEnvelope.success(
            operation="disconnect",
            status="disconnected",
            provider=type_value,
            account=account,
        )


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }


def test_canonical_connection_commands_forward_exact_interactive_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeConnectionService()
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)

    connected = runner.invoke(
        cli.app,
        [
            "connect",
            "outlook",
            "user@example.com",
            "--allow-browser-fallback",
            "--no-open-browser",
        ],
    )
    status = runner.invoke(cli.app, ["status", "outlook", "user@example.com"])
    disconnected = runner.invoke(cli.app, ["disconnect", "outlook", "user@example.com"])

    assert [connected.exit_code, status.exit_code, disconnected.exit_code] == [0, 0, 0]
    assert service.calls == [
        ("connect", "outlook", "user@example.com", True, False),
        ("status", "outlook", "user@example.com", False),
        ("disconnect", "outlook", "user@example.com"),
    ]
    assert json.loads(connected.stdout)["status"] == "connected_official"


def test_m365_aliases_use_one_deterministic_teams_compatibility_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[ConnectionType | None] = []
    report = HealthReport(
        "microsoft365.browser",
        HealthState.READY,
        ReasonCode.SUCCESS,
        (Capability.MAIL_SENT_LIST,),
        "user@example.com",
        "ready",
    )
    adapter = SimpleNamespace(interactive_login=lambda: report, health=lambda: report)

    def factory(*, connection_type: ConnectionType | None = None) -> object:
        calls.append(connection_type)
        return adapter

    monkeypatch.setattr(cli, "browser_adapter_from_environment", factory)

    login = runner.invoke(cli.app, ["m365", "login", "--allow-browser-fallback"])
    status = runner.invoke(cli.app, ["m365", "status", "--allow-browser-fallback"])

    assert login.exit_code == status.exit_code == 0
    assert calls == [ConnectionType.TEAMS, ConnectionType.TEAMS]


@pytest.mark.parametrize("exception", [ValueError("bad config"), RuntimeError("private")])
def test_connection_commands_never_expose_factory_exceptions(
    exception: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(**_: object) -> object:
        raise exception

    monkeypatch.setattr(cli, "connection_service_from_environment", broken)
    result = runner.invoke(cli.app, ["status", "outlook", "user@example.com"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] in {"configuration_invalid", "unknown_error"}
    if isinstance(exception, RuntimeError):
        assert "private" not in result.stdout


def test_profile_commands_list_show_delete_without_remote_revocation(tmp_path: Path) -> None:
    env = _env(tmp_path)
    settings = load_settings(environ=env)
    store = ProfileStore.from_settings(settings)
    store.save(
        ConnectionProfile.create(
            "gmail",
            "user@example.com",
            route="official",
        )
    )

    listed = runner.invoke(cli.app, ["profile", "list", "--type", "gmail"], env=env)
    shown = runner.invoke(
        cli.app,
        ["profile", "show", "gmail", "user@example.com"],
        env=env,
    )
    deleted = runner.invoke(
        cli.app,
        ["profile", "delete", "gmail", "user@example.com"],
        env=env,
    )
    missing = runner.invoke(
        cli.app,
        ["profile", "show", "gmail", "user@example.com"],
        env=env,
    )

    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["data"]["profiles"][0]["account"] == "user@example.com"
    assert shown.exit_code == 0 and json.loads(shown.stdout)["route"] == "official"
    assert deleted.exit_code == 0
    assert json.loads(deleted.stdout)["data"]["remote_revocation"] == "not_performed"
    assert missing.exit_code == 1
    assert json.loads(missing.stdout)["error"]["code"] == "profile_not_found"


def test_run_command_maps_targets_flags_and_input_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def run(payload: dict[str, Any]) -> ResultEnvelope:
        calls.append(payload)
        return ResultEnvelope.success(
            operation="run",
            provider=payload["type"],
            account=payload["account"],
            route="official",
            data={"records": []},
        )

    monkeypatch.setattr(cli, "run_application", run)
    teams = runner.invoke(
        cli.app,
        [
            "run",
            "--provider",
            "microsoft.graph",
            "--capability",
            "meeting.transcript.get",
            "--account",
            "user@example.com",
            "--meeting-id",
            "meeting-1",
            "--browser-fallback",
        ],
    )
    bad_capability = runner.invoke(
        cli.app,
        ["run", "--provider", "microsoft.graph", "--capability", "bad"],
    )
    bad_provider = runner.invoke(
        cli.app,
        ["run", "--provider", "unknown", "--capability", "mail.sent.list"],
    )

    assert teams.exit_code == 0
    assert calls == [
        {
            "type": "teams",
            "account": "user@example.com",
            "capability": "meeting.transcript.get",
            "start": None,
            "end": None,
            "calendar_id": "primary",
            "assignee": None,
            "jql": None,
            "meeting_id": "meeting-1",
            "search": None,
            "folder_path": None,
            "item_id": None,
            "drive_id": None,
            "site": None,
            "max_content_bytes": 10_485_760,
            "include_attachments": False,
            "max_attachment_bytes": 10_485_760,
            "limit": 50,
            "allow_browser_fallback": True,
        }
    ]
    assert bad_capability.exit_code == 2
    assert bad_provider.exit_code == 2


def test_health_and_m365_compatibility_commands_use_safe_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = HealthReport(
        "microsoft.graph",
        HealthState.READY,
        ReasonCode.SUCCESS,
        (Capability.MAIL_SENT_LIST,),
        "user@example.com",
        "ready",
    )
    descriptor = ProviderDescriptor(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
    )
    adapter = SimpleNamespace(descriptor=descriptor, health=lambda: report)
    registry = SimpleNamespace(find=lambda provider: adapter)
    browser_report = HealthReport(
        "microsoft365.browser",
        HealthState.READY,
        ReasonCode.SUCCESS,
        (Capability.MAIL_SENT_LIST,),
        "user@example.com",
        "browser ready",
    )
    browser = SimpleNamespace(
        health=lambda: browser_report,
        interactive_login=lambda: browser_report,
    )
    monkeypatch.setattr(cli, "registry_from_environment", lambda **_: registry)
    monkeypatch.setattr(cli, "browser_adapter_from_environment", lambda **_: browser)

    health = runner.invoke(
        cli.app,
        ["health", "microsoft.graph", "--account", "user@example.com"],
    )
    login = runner.invoke(cli.app, ["m365", "login", "--allow-browser-fallback"])
    status = runner.invoke(cli.app, ["m365", "status", "--allow-browser-fallback"])

    assert health.exit_code == 0 and json.loads(health.stdout)["route"] == "official"
    assert login.exit_code == 0 and json.loads(login.stdout)["status"] == "connected_browser"
    assert json.loads(status.stdout)["data"]["extraction_status"] == "not_implemented"


def test_doctor_ready_branch_and_config_validation_failure_are_uniform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(environ=_env(tmp_path))

    class ReadyDoctor:
        def __init__(self, *_: object) -> None:
            pass

        def inspect(self, type_value: str | ConnectionType, account: str | None = None):
            parsed = ConnectionType.parse(type_value)
            return DoctorReport(
                parsed,
                account,
                True,
                (),
                (),
                (),
                (),
                (DoctorCheck("configuration", "ready", "ready", True),),
                "127.0.0.1",
                8765,
                True,
            )

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "DoctorService", ReadyDoctor)
    ready = runner.invoke(cli.app, ["doctor"])

    assert ready.exit_code == 0
    assert len(json.loads(ready.stdout)["data"]["reports"]) == 7

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: (_ for _ in ()).throw(ValueError("invalid port")),
    )
    invalid = runner.invoke(cli.app, ["config", "validate"])
    assert invalid.exit_code == 1
    assert json.loads(invalid.stdout)["error"]["code"] == "configuration_invalid"


def test_legacy_query_helpers_cover_all_capability_shapes_and_provider_mapping() -> None:
    common = {
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
        "account": "user@example.com",
        "calendar_id": "primary",
        "assignee": "user@example.com",
        "jql": "status = Done",
        "meeting_id": "meeting-1",
        "limit": 500,
    }
    for capability in Capability:
        query = cli._query_payload(capability, **common)
        assert isinstance(query, _EXPECTED_QUERY_TYPES[capability])

    # A transcript is selected by meeting, by recording, or by period. Dropping the meeting id
    # while a window stands is still a complete request; dropping every selector is not.
    by_window = cli._query_payload(
        Capability.MEETING_TRANSCRIPT_GET,
        **{**common, "meeting_id": None},
    )
    assert isinstance(by_window, TranscriptQuery)
    assert by_window.meeting_id is None and by_window.window is not None

    by_recording = cli._query_payload(
        Capability.MEETING_TRANSCRIPT_GET,
        **{**common, "meeting_id": None, "start": None, "end": None},
        drive_id="drive-1",
        item_id="item-1",
    )
    assert isinstance(by_recording, TranscriptQuery)
    assert by_recording.drive_id == "drive-1" and by_recording.item_id == "item-1"

    with pytest.raises(cli._InputError, match="together"):
        cli._query_payload(
            Capability.MEETING_TRANSCRIPT_GET,
            **{**common, "meeting_id": None},
            drive_id="drive-1",
        )
    with pytest.raises(cli._InputError, match="need one of"):
        cli._query_payload(
            Capability.MEETING_TRANSCRIPT_GET,
            **{**common, "meeting_id": None, "start": None, "end": None},
        )
    with pytest.raises(cli._InputError, match="required"):
        cli._window(None, None)
    with pytest.raises(cli._InputError, match="Invalid"):
        cli._window("not-a-date", "also-not-a-date")
    with pytest.raises(cli._InputError, match="date window"):
        cli._require_window(None)

    assert (
        cli._target_for_provider("microsoft.graph", Capability.MAIL_SENT_LIST)
        is ConnectionType.OUTLOOK
    )
    assert (
        cli._target_for_provider("microsoft.graph", Capability.MEETING_TRANSCRIPTS_LIST)
        is ConnectionType.TEAMS
    )
    assert (
        cli._target_for_provider("microsoft.graph", Capability.MAIL_INBOX_LIST)
        is ConnectionType.OUTLOOK
    )
    assert cli._target_for_provider("google.gmail", None) is ConnectionType.GMAIL
    assert cli._target_for_provider("google.calendar", None) is ConnectionType.GOOGLE_CALENDAR
    assert cli._target_for_provider("jira.cloud", None) is ConnectionType.JIRA
    assert cli._target_for_provider("glpi.rest", None) is ConnectionType.GLPI
    assert cli._target_for_provider("unknown", None) is None


def test_legacy_query_helper_fails_closed_on_an_unknown_capability() -> None:
    unknown = cast(Capability, "mail.archive.list")

    with pytest.raises(cli._InputError, match="Unsupported capability"):
        cli._query_payload(
            unknown,
            start="2026-08-01T00:00:00+00:00",
            end="2026-09-01T00:00:00+00:00",
            account="user@example.com",
            calendar_id="primary",
            assignee=None,
            jql=None,
            meeting_id=None,
            limit=50,
        )


@pytest.mark.parametrize(
    "reason,category,retryable",
    [
        (ReasonCode.CONFIGURATION_INVALID, "configuration", False),
        (ReasonCode.AUTHORIZATION_DENIED, "authorization", False),
        (ReasonCode.NETWORK_TIMEOUT, "network", True),
        (ReasonCode.REQUEST_REJECTED, "runtime", False),
    ],
)
def test_cli_reason_errors_have_stable_safe_categories(
    reason: ReasonCode,
    category: str,
    retryable: bool,
) -> None:
    error = cli._reason_error(reason, "safe")
    assert error.cause_category == category
    assert error.retryable is retryable


def test_provider_and_m365_commands_map_known_and_unknown_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "registry_from_environment",
        lambda **_: (_ for _ in ()).throw(
            ConnectorFailure(ReasonCode.INVALID_CREDENTIALS, "denied")
        ),
    )
    providers = runner.invoke(cli.app, ["providers"])
    assert providers.exit_code == 1
    assert json.loads(providers.stdout)["error"]["code"] == "invalid_credentials"

    monkeypatch.setattr(
        cli,
        "browser_adapter_from_environment",
        lambda **_: (_ for _ in ()).throw(RuntimeError("private")),
    )
    m365 = runner.invoke(cli.app, ["m365", "status", "--allow-browser-fallback"])
    assert m365.exit_code == 1
    assert json.loads(m365.stdout)["error"]["code"] == "unknown_error"
    assert "private" not in m365.stdout
