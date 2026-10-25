"""Project mode reads <home>/connect.env as the lowest environment layer and --token-env."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import solercia_connect.cli as cli
from solercia_connect.config import (
    ProfileStore,
    ProjectHomeError,
    effective_environment,
    load_settings,
)
from solercia_connect.connections import ConnectionService
from solercia_connect.oauth import OAuthTokenSet
from solercia_connect.project_home import prepare_project_home
from solercia_connect.secret_store import API_TOKEN_SERVICE, KeyringSecretStore
from tests.fakes import NOW, FakeTransport, json_response

ACCOUNT = "user@example.com"
SITE = "https://example.atlassian.net"
TOKEN = "jira-api-token-from-env"
BASIC = "Basic " + base64.b64encode(f"{ACCOUNT}:{TOKEN}".encode()).decode()
KEY = (API_TOKEN_SERVICE, f"jira:{ACCOUNT}")

runner = CliRunner()


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        del self.values[(service_name, username)]


class UnusedOAuthStore:
    def load(self, key: str) -> OAuthTokenSet | None:
        raise AssertionError(f"OAuth token store must not be read for {key}")

    def save(self, key: str, tokens: OAuthTokenSet) -> None:
        raise AssertionError(f"OAuth token store must not be written for {key}")

    def delete(self, key: str) -> None:
        raise AssertionError(f"OAuth token store must not be deleted for {key}")


def _project(tmp_path: Path, content: str | None = None, *, mode: int = 0o600) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    home = project / ".solercia-connect"
    prepare_project_home(home)
    if content is not None:
        env_file = home / "connect.env"
        env_file.write_text(content, encoding="utf-8")
        env_file.chmod(mode)
    return home


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SOLERCIA_CONNECT_ENV_FILE", "SOLERCIA_CONNECT_HOME", "PANDORA_JIRA_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_connect_env_is_the_lowest_layer_below_the_env_file_and_the_process(
    tmp_path: Path,
) -> None:
    home = _project(
        tmp_path,
        "ONLY_PROJECT=project\n"
        "FILE_WINS=project\n"
        "PROCESS_WINS=project\n"
        "EMPTY_IN_PROCESS=project\n"
        "SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID=project-client\n",
    )
    explicit = tmp_path / "explicit.env"
    explicit.write_text("FILE_WINS=file\nPROCESS_WINS=file\n", encoding="utf-8")
    environ = {
        "SOLERCIA_CONNECT_HOME": str(home),
        "SOLERCIA_CONNECT_ENV_FILE": str(explicit),
        "PROCESS_WINS": "process",
        "EMPTY_IN_PROCESS": "",
    }

    values = effective_environment(environ=environ)
    settings = load_settings(environ=environ)

    assert values["ONLY_PROJECT"] == "project"
    assert values["FILE_WINS"] == "file"
    assert values["PROCESS_WINS"] == "process"
    assert values["EMPTY_IN_PROCESS"] == "project"
    assert settings.microsoft_client_id == "project-client"
    assert "ONLY_PROJECT" in settings.configured_names
    assert settings.env_file == explicit
    assert load_settings(environ=values) == settings


def test_connect_env_follows_the_env_file_parsing_rules(tmp_path: Path) -> None:
    home = _project(
        tmp_path,
        "# comment\n\nexport QUOTED='single value'\nDOUBLE=\"double value\"\n",
    )

    values = effective_environment(environ={"SOLERCIA_CONNECT_HOME": str(home)})

    assert values["QUOTED"] == "single value"
    assert values["DOUBLE"] == "double value"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("GOOD=1\nnot an assignment\n", "line 2"),
        ("lower_case=secret-value\n", "line 1"),
    ],
)
def test_invalid_connect_env_lines_fail_without_echoing_values(
    tmp_path: Path, content: str, expected: str
) -> None:
    home = _project(tmp_path, content)

    with pytest.raises(ProjectHomeError, match=expected) as captured:
        load_settings(environ={"SOLERCIA_CONNECT_HOME": str(home)})

    assert "connect.env" in str(captured.value)
    assert "secret-value" not in str(captured.value)


@pytest.mark.parametrize("name", ["SOLERCIA_CONNECT_HOME", "SOLERCIA_CONNECT_ENV_FILE"])
def test_connect_env_cannot_redirect_the_home_or_the_env_file(tmp_path: Path, name: str) -> None:
    home = _project(tmp_path, f"{name}=/elsewhere\n")

    with pytest.raises(ProjectHomeError, match=name):
        load_settings(environ={"SOLERCIA_CONNECT_HOME": str(home)})


def test_a_readable_connect_env_fails_closed_with_a_chmod_remediation(tmp_path: Path) -> None:
    home = _project(tmp_path, "API_TOKEN=secret-value\n", mode=0o644)

    with pytest.raises(ProjectHomeError, match="chmod 600") as captured:
        effective_environment(environ={"SOLERCIA_CONNECT_HOME": str(home)})

    assert "secret-value" not in str(captured.value)
    assert (home / "connect.env").stat().st_mode & 0o777 == 0o644


def test_a_symlinked_or_foreign_connect_env_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _project(tmp_path)
    outside = tmp_path / "outside.env"
    outside.write_text("API_TOKEN=secret-value\n", encoding="utf-8")
    outside.chmod(0o600)
    (home / "connect.env").symlink_to(outside)
    environ = {"SOLERCIA_CONNECT_HOME": str(home)}

    with pytest.raises(ProjectHomeError, match="symbolic link"):
        load_settings(environ=environ)

    (home / "connect.env").unlink()
    (home / "connect.env").mkdir()
    with pytest.raises(ProjectHomeError, match="regular file"):
        load_settings(environ=environ)

    (home / "connect.env").rmdir()
    (home / "connect.env").write_text("API_TOKEN=secret-value\n", encoding="utf-8")
    (home / "connect.env").chmod(0o600)
    owner = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    with pytest.raises(ProjectHomeError, match="owned by the current user"):
        load_settings(environ=environ)


def test_without_project_home_no_connect_env_is_ever_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _project(tmp_path, "ONLY_PROJECT=project\n")
    monkeypatch.chdir(home.parent)

    values = effective_environment(environ={})

    assert "ONLY_PROJECT" not in values
    assert load_settings(environ={}).project_home is None


def _cli_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *responses: Any
) -> tuple[MemoryKeyring, FakeTransport]:
    settings = load_settings(
        environ={
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        }
    )
    keyring = MemoryKeyring()
    transport = FakeTransport(*responses)
    service = ConnectionService(
        settings,
        ProfileStore.from_settings(settings),
        UnusedOAuthStore(),
        secret_store=KeyringSecretStore(backend=keyring),
        transport=transport,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(cli, "connection_service_from_environment", lambda **_: service)
    return keyring, transport


def _myself() -> Any:
    return json_response(200, {"accountId": "acct-1", "emailAddress": ACCOUNT})


def _jira(*options: str) -> list[str]:
    return ["connect", "jira", ACCOUNT, "--site", SITE, *options]


def test_token_env_reads_the_api_token_from_the_process_environment_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv("PANDORA_JIRA_TOKEN", f"  {TOKEN}  ")
    keyring, transport = _cli_service(tmp_path, monkeypatch, _myself())

    result = runner.invoke(
        cli.app, _jira("--token-env", "PANDORA_JIRA_TOKEN"), input="prompt-token\n"
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "connected_official"
    assert TOKEN not in result.output
    assert "Jira API token" not in result.output
    assert keyring.values == {KEY: TOKEN}
    assert transport.requests[0].headers is not None
    assert transport.requests[0].headers["Authorization"] == BASIC


def test_token_env_reads_a_variable_defined_only_in_connect_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    home = _project(tmp_path, f"PANDORA_JIRA_TOKEN={TOKEN}\n")
    monkeypatch.setenv("SOLERCIA_CONNECT_HOME", str(home))
    keyring, _ = _cli_service(tmp_path, monkeypatch, _myself())

    result = runner.invoke(cli.app, _jira("--token-env", "PANDORA_JIRA_TOKEN"))

    assert json.loads(result.stdout)["status"] == "connected_official"
    assert TOKEN not in result.output
    assert keyring.values == {KEY: TOKEN}


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_or_empty_token_variable_is_an_error_that_names_only_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    _isolate(monkeypatch)
    if value is not None:
        monkeypatch.setenv("PANDORA_JIRA_TOKEN", value)
    keyring, transport = _cli_service(tmp_path, monkeypatch)

    result = runner.invoke(
        cli.app, _jira("--token-env", "PANDORA_JIRA_TOKEN"), input="prompt-token\n"
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_input"
    assert "PANDORA_JIRA_TOKEN" in payload["error"]["message"]
    assert "prompt-token" not in result.output
    assert transport.requests == [] and keyring.values == {}


def test_token_env_rejects_a_name_that_is_not_a_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    keyring, transport = _cli_service(tmp_path, monkeypatch)

    result = runner.invoke(cli.app, _jira("--token-env", "not a=name"))

    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_input"
    assert "--token-env" in payload["error"]["message"]
    assert transport.requests == [] and keyring.values == {}


def test_token_env_is_exclusive_with_stdin_oauth_and_non_jira_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch)
    monkeypatch.setenv("PANDORA_JIRA_TOKEN", TOKEN)
    keyring, transport = _cli_service(tmp_path, monkeypatch)
    token_env = ("--token-env", "PANDORA_JIRA_TOKEN")
    cases = {
        "--token-stdin": _jira(*token_env, "--token-stdin"),
        "--token-env applies only to API tokens": _jira(*token_env, "--token-kind", "oauth"),
        "apply only to jira": ["connect", "gmail", ACCOUNT, *token_env],
    }

    for expected, arguments in cases.items():
        result = runner.invoke(cli.app, arguments, input=f"{TOKEN}\n")
        payload = json.loads(result.stdout)
        assert payload["status"] == "invalid_input", expected
        assert expected in payload["error"]["message"]
        assert TOKEN not in result.output
    assert transport.requests == [] and keyring.values == {}
