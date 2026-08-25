from __future__ import annotations

from solercia_connect.domain import ReasonCode
from solercia_connect.secrets import redact_text


class ConnectorFailure(RuntimeError):
    """A provider or policy failure with a safe, typed reason."""

    def __init__(self, reason: ReasonCode, safe_message: str) -> None:
        self.reason = reason
        self.safe_message = redact_text(safe_message)
        super().__init__(self.safe_message)


class TransportTimeoutError(RuntimeError):
    pass


class TransportNetworkError(RuntimeError):
    pass
