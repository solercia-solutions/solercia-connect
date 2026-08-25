from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_secure_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the hermetic suite away from the operating-system credential store."""

    if os.getenv("SOLERCIA_CONNECT_LIVE") == "1":
        return
    monkeypatch.setenv(
        "PYTHON_KEYRING_BACKEND",
        "keyring.backends.null.Keyring",
    )
