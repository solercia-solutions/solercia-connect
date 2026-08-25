from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solercia_connect.serialization import to_primitive

ENVELOPE_SCHEMA = "solercia.connect/v1"
RECORD_SCHEMA = "solercia.connect/record/v1"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    code: str
    message: str
    retryable: bool
    missing_configuration: tuple[str, ...] = ()
    remediation: tuple[str, ...] = ()
    cause_category: str = "runtime"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "missing_configuration": list(dict.fromkeys(self.missing_configuration)),
            "remediation": list(self.remediation),
            "cause_category": self.cause_category,
        }


@dataclass(frozen=True, slots=True)
class ResultEnvelope:
    ok: bool
    operation: str
    status: str
    provider: str | None = None
    account: str | None = None
    route: str | None = None
    data: Any = None
    receipt: Any = None
    error: ErrorDetail | None = None
    schema: str = ENVELOPE_SCHEMA

    @classmethod
    def success(
        cls,
        *,
        operation: str,
        status: str = "success",
        provider: str | None = None,
        account: str | None = None,
        route: str | None = None,
        data: Any = None,
        receipt: Any = None,
    ) -> ResultEnvelope:
        return cls(True, operation, status, provider, account, route, data, receipt)

    @classmethod
    def failure(
        cls,
        *,
        operation: str,
        status: str,
        error: ErrorDetail,
        provider: str | None = None,
        account: str | None = None,
        route: str | None = None,
        data: Any = None,
        receipt: Any = None,
    ) -> ResultEnvelope:
        return cls(False, operation, status, provider, account, route, data, receipt, error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ok": self.ok,
            "operation": self.operation,
            "status": self.status,
            "provider": self.provider,
            "account": self.account,
            "route": self.route,
            "data": to_primitive(self.data) if self.data is not None else {},
            "receipt": to_primitive(self.receipt) if self.receipt is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
        }


def versioned_record(value: Any) -> dict[str, Any]:
    primitive = to_primitive(value)
    if not isinstance(primitive, dict):
        raise ValueError("Normalized record must serialize to an object")
    return {"schema": RECORD_SCHEMA, **primitive}
