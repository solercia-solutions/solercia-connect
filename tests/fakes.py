from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solercia_connect.config import ConnectionType
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DomainRecord,
    EmailAddress,
    HealthReport,
    HealthState,
    MailCategoriesUpdate,
    MailCategoryAssignment,
    MailMessage,
    Provenance,
    ProviderFamily,
    QueryPayload,
    ReasonCode,
    RouteKind,
)
from solercia_connect.ports import ProviderDescriptor, TransportResponse
from solercia_connect.providers._shared import unique_canonical_account_identity
from solercia_connect.providers.m365_browser.driver import BrowserSnapshot
from solercia_connect.providers.m365_browser.selectors import Microsoft365Selectors

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str] | None
    params: Mapping[str, Any] | None
    json_body: Mapping[str, Any] | None
    form_body: Mapping[str, str] | None


class FakeTransport:
    def __init__(self, *responses: TransportResponse | Exception) -> None:
        self.responses: deque[TransportResponse | Exception] = deque(responses)
        self.requests: list[RecordedRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> TransportResponse:
        del timeout_seconds
        self.requests.append(RecordedRequest(method, url, headers, params, json_body, form_body))
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def json_response(status: int, payload: Any) -> TransportResponse:
    return TransportResponse(
        status,
        {"Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )


class FakeAdapter:
    def __init__(
        self,
        provider_id: str,
        family: ProviderFamily,
        route: RouteKind,
        capabilities: frozenset[Capability],
        *outcomes: tuple[DomainRecord, ...] | Exception,
    ) -> None:
        self.descriptor = ProviderDescriptor(provider_id, family, route, capabilities)
        self.outcomes: deque[tuple[DomainRecord, ...] | Exception] = deque(outcomes)
        self.calls = 0

    def health(self) -> HealthReport:
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
        )

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        del request
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("Fake adapter has no queued outcome")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeBrowserDriver:
    def __init__(
        self,
        snapshots: list[BrowserSnapshot],
        records: tuple[DomainRecord, ...] = (),
        read_error: Exception | None = None,
        *,
        reveal_result: bool = True,
        outlook_identity_outcomes: list[object] | None = None,
    ) -> None:
        self.snapshots = deque(snapshots)
        self.records = records
        self.read_error = read_error
        self.reveal_result = reveal_result
        self.outlook_identity_outcomes = (
            deque(outlook_identity_outcomes) if outlook_identity_outcomes is not None else None
        )
        self.open_calls: list[tuple[Path, bool]] = []
        self.open_connection_types: list[ConnectionType | None] = []
        self.navigate_calls: list[tuple[str, int]] = []
        self.snapshot_connection_types: list[ConnectionType] = []
        self.outlook_identity_calls: list[tuple[int, int, int, int]] = []
        self.reveal_calls: list[int] = []
        self.wait_calls: list[int] = []
        self.read_calls = 0
        self.close_calls = 0
        self.last_snapshot: BrowserSnapshot | None = None
        self.write_result: MailCategoryAssignment | None = None
        self.write_calls: list[MailCategoriesUpdate] = []

    def open_persistent(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        connection_type: ConnectionType | None = None,
    ) -> None:
        self.open_calls.append((profile_dir, headless))
        self.open_connection_types.append(connection_type)

    def navigate(self, url: str, *, timeout_ms: int) -> None:
        self.navigate_calls.append((url, timeout_ms))

    def snapshot(
        self,
        selectors: Microsoft365Selectors,
        *,
        connection_type: ConnectionType,
    ) -> BrowserSnapshot:
        del selectors
        self.snapshot_connection_types.append(connection_type)
        snapshot = self.snapshots.popleft() if len(self.snapshots) > 1 else self.snapshots[0]
        self.last_snapshot = snapshot
        return snapshot

    def outlook_session_identity(
        self,
        *,
        wait_interval_ms: int,
        max_wait_cycles: int,
        max_probe_attempts: int,
        probe_timeout_ms: int,
    ) -> str:
        self.outlook_identity_calls.append(
            (wait_interval_ms, max_wait_cycles, max_probe_attempts, probe_timeout_ms)
        )
        if self.outlook_identity_outcomes is not None:
            if not self.outlook_identity_outcomes:
                raise AssertionError("Fake Outlook identity outcomes are exhausted")
            outcome = self.outlook_identity_outcomes.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome  # type: ignore[return-value]
        if self.last_snapshot is None:
            raise AssertionError("Fake Outlook identity was requested before a browser snapshot")
        identities = {
            identity
            for hint in self.last_snapshot.account_hints
            if (identity := unique_canonical_account_identity(hint, exact=False)) is not None
        }
        if len(identities) != 1:
            raise AssertionError(
                "Fake Outlook identity needs an explicit REST outcome when DOM hints are absent "
                "or ambiguous"
            )
        return next(iter(identities))

    def reveal_identity_menu(
        self,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> bool:
        del selectors
        self.reveal_calls.append(timeout_ms)
        return self.reveal_result

    def wait(self, milliseconds: int) -> None:
        self.wait_calls.append(milliseconds)

    def read_records(
        self,
        capability: Capability,
        payload: QueryPayload,
        selectors: Microsoft365Selectors,
        *,
        timeout_ms: int,
    ) -> tuple[DomainRecord, ...]:
        del capability, payload, selectors, timeout_ms
        self.read_calls += 1
        if self.read_error:
            raise self.read_error
        return self.records

    def set_mail_categories(
        self,
        update: MailCategoriesUpdate,
        *,
        timeout_ms: int,
    ) -> MailCategoryAssignment:
        del timeout_ms
        self.write_calls.append(update)
        if self.read_error:
            raise self.read_error
        if self.write_result is None:
            raise AssertionError("Fake browser driver has no queued category write result")
        return self.write_result

    def close(self) -> None:
        self.close_calls += 1


def mail_record(
    provider_id: str = "microsoft.graph",
    family: ProviderFamily = ProviderFamily.MICROSOFT,
    route: RouteKind = RouteKind.OFFICIAL,
    account_id: str | None = "user@example.com",
    folder: str | None = None,
    internet_message_id: str | None = None,
    received_at: datetime | None = None,
) -> MailMessage:
    return MailMessage(
        "message-1",
        "Status",
        NOW,
        EmailAddress("sender@example.com", "Sender"),
        (EmailAddress("recipient@example.com", "Recipient"),),
        (),
        "Complete body",
        "text",
        "thread-1",
        False,
        Provenance(provider_id, family, route, "message-1", NOW, account_id),
        internet_message_id=internet_message_id,
        folder=folder,
        received_at=received_at,
    )


def authenticated_snapshot(account: str = "user@example.com") -> BrowserSnapshot:
    return BrowserSnapshot(
        "https://teams.microsoft.com/v2/",
        "Microsoft Teams",
        (f"Profile for {account}",),
        False,
        False,
        True,
    )
