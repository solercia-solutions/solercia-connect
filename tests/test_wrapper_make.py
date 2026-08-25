from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_wrapper_is_executable_shell_safe_and_maps_to_canonical_connect(tmp_path: Path) -> None:
    wrapper = ROOT / "solercia-connect.sh"
    assert wrapper.is_file()
    assert os.access(wrapper, os.X_OK)
    subprocess.run(["/bin/sh", "-n", str(wrapper)], check=True)

    isolated = tmp_path / "wrapper"
    isolated.mkdir()
    copied = isolated / wrapper.name
    shutil.copy2(wrapper, copied)
    copied.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "solercia-connect"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
    fake.chmod(0o755)
    result = subprocess.run(
        [str(copied), "outlook", "user@example.com", "--no-open-browser"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == ("connect outlook user@example.com --no-open-browser")


def test_wrapper_preflight_failure_is_the_versioned_json_envelope(tmp_path: Path) -> None:
    copied = tmp_path / "solercia-connect.sh"
    shutil.copy2(ROOT / "solercia-connect.sh", copied)
    copied.chmod(0o755)

    result = subprocess.run(
        [str(copied), "outlook", "user@example.com"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["schema"] == "solercia.connect/v1"
    assert payload["error"]["code"] == "cli_not_installed"


def test_make_targets_are_deterministic_and_keep_browser_install_opt_in() -> None:
    makefile = (ROOT / "Makefile").read_text()
    assert "setup-browser:" in makefile
    assert "test-live:" in makefile
    assert '-k "$(TYPE)"' not in makefile

    setup = subprocess.run(
        ["make", "-n", "setup"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    browser = subprocess.run(
        ["make", "-n", "setup-browser"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    doctor = subprocess.run(
        [
            "make",
            "-n",
            "doctor",
            "TYPE=outlook",
            "ACCOUNT=user@example.com",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert "uv sync --locked --extra dev --extra oauth --extra service" in setup
    assert "--extra browser" not in setup
    assert "playwright install chromium" in browser
    assert 'doctor "outlook" "user@example.com"' in doctor


def test_make_doctor_and_connect_stdout_is_exactly_one_json_document(tmp_path: Path) -> None:
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' "
        '\'{"schema":"solercia.connect/v1","ok":true,"operation":"fake"}\'\n'
    )
    fake_uv.chmod(0o755)

    for target in ("doctor", "connect"):
        result = subprocess.run(
            [
                "make",
                target,
                f"UV={fake_uv}",
                "TYPE=outlook",
                "ACCOUNT=user@example.com",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["schema"] == "solercia.connect/v1"
        assert result.stdout.count("\n") == 1


def test_make_connect_selects_dot_env_local_and_maps_browser_fallback_authorization(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.local").write_text("SOLERCIA_CONNECT_BROWSER_FALLBACK=true\n")
    fake_uv = tmp_path / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        "printf "
        '\'{"schema":"solercia.connect/v1","ok":true,"env_file":"%s",'
        '"args":"%s"}\\n\' "$SOLERCIA_CONNECT_ENV_FILE" "$*"\n'
    )
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env.pop("SOLERCIA_CONNECT_ENV_FILE", None)

    result = subprocess.run(
        [
            "make",
            "-f",
            str(ROOT / "Makefile"),
            "connect",
            f"UV={fake_uv}",
            "TYPE=outlook",
            "ACCOUNT=user@example.com",
            "ALLOW_BROWSER_FALLBACK=1",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout)

    assert result.returncode == 0
    assert payload["env_file"] == str(tmp_path / ".env.local")
    assert "connect outlook user@example.com --allow-browser-fallback" in payload["args"]


def test_make_doctor_and_connect_usage_failures_are_json_exit_two() -> None:
    for target in ("doctor", "connect"):
        result = subprocess.run(
            ["make", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["schema"] == "solercia.connect/v1"
        assert payload["status"] == "invalid_input"
