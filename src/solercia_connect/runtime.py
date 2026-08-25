from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from solercia_connect.domain import (
    CalendarQuery,
    Capability,
    ConnectorRequest,
    DomainRecord,
    ExecutionOutcome,
    ExecutionReceipt,
    MailQuery,
    ProviderFamily,
    ReasonCode,
    ReceiptStatus,
    RouteKind,
    StepOutcome,
    TraceStep,
    TranscriptQuery,
    receipt_fingerprint,
    record_matches_capability,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import ProviderAdapter, ProviderDescriptor
from solercia_connect.registry import ProviderRegistry


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    official_max_attempts: int = 3
    official_retry_delays_seconds: tuple[float, ...] = (0.0, 0.1)
    retryable_reasons: frozenset[ReasonCode] = frozenset(
        {
            ReasonCode.RATE_LIMITED,
            ReasonCode.NETWORK_TIMEOUT,
            ReasonCode.NETWORK_UNAVAILABLE,
            ReasonCode.PROVIDER_UNAVAILABLE,
        }
    )
    browser_fallback_reasons: frozenset[ReasonCode] = frozenset(
        {
            ReasonCode.ADMIN_CONSENT_REQUIRED,
            ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
        }
    )
    browser_fallback_capabilities: frozenset[Capability] = frozenset(
        {
            Capability.MAIL_SENT_LIST,
            Capability.CALENDAR_EVENTS_LIST,
            Capability.MEETING_TRANSCRIPTS_LIST,
            Capability.MEETING_TRANSCRIPT_GET,
            Capability.FILES_SEARCH,
            Capability.FILES_GET,
        }
    )
    browser_fallback_families: frozenset[ProviderFamily] = frozenset({ProviderFamily.MICROSOFT})

    def __post_init__(self) -> None:
        if self.official_max_attempts < 1:
            raise ValueError("official_max_attempts must be positive")
        expected_delays = self.official_max_attempts - 1
        if len(self.official_retry_delays_seconds) != expected_delays:
            raise ValueError("Retry delays must contain exactly max_attempts - 1 entries")
        if any(delay < 0 for delay in self.official_retry_delays_seconds):
            raise ValueError("Retry delays cannot be negative")

    def allows_browser(
        self,
        request: ConnectorRequest,
        failure: ConnectorFailure,
        family: ProviderFamily,
    ) -> bool:
        return (
            failure.reason in self.browser_fallback_reasons
            and request.capability in self.browser_fallback_capabilities
            and family in self.browser_fallback_families
        )


class ConnectorRuntime:
    def __init__(
        self,
        registry: ProviderRegistry,
        policy: RoutePolicy | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._registry = registry
        self._policy = policy or RoutePolicy()
        self._sleeper = sleeper
        self._clock = clock

    def execute(
        self,
        request: ConnectorRequest,
        *,
        browser_fallback_enabled: bool = False,
    ) -> ExecutionOutcome:
        steps: list[TraceStep] = []
        try:
            official = self._registry.official(request.provider_id)
        except ConnectorFailure as registry_failure:
            steps.append(_step(1, RouteKind.OFFICIAL, request.provider_id, 0, registry_failure))
            return self._failure_outcome(request, steps, registry_failure.reason)

        if request.capability not in official.descriptor.capabilities:
            official_failure = ConnectorFailure(
                ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
                "Official adapter does not expose the requested capability",
            )
            steps.append(
                _step(
                    len(steps) + 1,
                    RouteKind.OFFICIAL,
                    official.descriptor.provider_id,
                    0,
                    official_failure,
                )
            )
        else:
            result, official_failure = self._run_official(official, request, steps)
            if result is not None:
                return self._success_outcome(request, steps, RouteKind.OFFICIAL, result)

        if not browser_fallback_enabled:
            steps.append(
                TraceStep(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    "browser-policy",
                    0,
                    StepOutcome.SKIPPED,
                    ReasonCode.BROWSER_FALLBACK_DISABLED,
                    "Browser fallback was not explicitly enabled",
                )
            )
            return self._failure_outcome(request, steps, official_failure.reason)

        if not self._policy.allows_browser(
            request,
            official_failure,
            official.descriptor.family,
        ):
            steps.append(
                TraceStep(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    "browser-policy",
                    0,
                    StepOutcome.SKIPPED,
                    ReasonCode.BROWSER_FALLBACK_DENIED,
                    "Typed reason or capability is not allowlisted for browser fallback",
                )
            )
            return self._failure_outcome(request, steps, official_failure.reason)

        browser = self._registry.browser(official.descriptor.family)
        if browser is None:
            steps.append(
                TraceStep(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    "browser-policy",
                    0,
                    StepOutcome.SKIPPED,
                    ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
                    "No browser adapter is configured for the provider family",
                )
            )
            return self._failure_outcome(
                request, steps, ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
            )

        if request.capability not in browser.descriptor.capabilities:
            steps.append(
                TraceStep(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    browser.descriptor.provider_id,
                    0,
                    StepOutcome.FAILURE,
                    ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED,
                    "Browser adapter does not expose the requested capability",
                )
            )
            return self._failure_outcome(
                request, steps, ReasonCode.BROWSER_CAPABILITY_NOT_IMPLEMENTED
            )

        try:
            records = browser.execute(request)
            _validate_records(request, records, browser.descriptor)
        except ConnectorFailure as browser_failure:
            steps.append(
                _step(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    browser.descriptor.provider_id,
                    1,
                    browser_failure,
                )
            )
            return self._failure_outcome(request, steps, browser_failure.reason)
        except Exception:
            unknown = ConnectorFailure(
                ReasonCode.UNKNOWN_ERROR, "Browser adapter failed with an unknown error"
            )
            steps.append(
                _step(
                    len(steps) + 1,
                    RouteKind.BROWSER,
                    browser.descriptor.provider_id,
                    1,
                    unknown,
                )
            )
            return self._failure_outcome(request, steps, unknown.reason)

        steps.append(
            TraceStep(
                len(steps) + 1,
                RouteKind.BROWSER,
                browser.descriptor.provider_id,
                1,
                StepOutcome.SUCCESS,
                ReasonCode.SUCCESS,
                "Browser fallback completed",
            )
        )
        return self._success_outcome(request, steps, RouteKind.BROWSER, records)

    def _run_official(
        self,
        adapter: ProviderAdapter,
        request: ConnectorRequest,
        steps: list[TraceStep],
    ) -> tuple[tuple[DomainRecord, ...] | None, ConnectorFailure]:
        last_failure = ConnectorFailure(ReasonCode.UNKNOWN_ERROR, "Official adapter did not run")
        for attempt in range(1, self._policy.official_max_attempts + 1):
            try:
                records = adapter.execute(request)
                _validate_records(request, records, adapter.descriptor)
            except ConnectorFailure as failure:
                last_failure = failure
            except Exception:
                last_failure = ConnectorFailure(
                    ReasonCode.UNKNOWN_ERROR, "Official adapter failed with an unknown error"
                )
            else:
                steps.append(
                    TraceStep(
                        len(steps) + 1,
                        RouteKind.OFFICIAL,
                        adapter.descriptor.provider_id,
                        attempt,
                        StepOutcome.SUCCESS,
                        ReasonCode.SUCCESS,
                        "Official adapter completed",
                    )
                )
                return records, last_failure

            retry = (
                last_failure.reason in self._policy.retryable_reasons
                and attempt < self._policy.official_max_attempts
            )
            steps.append(
                TraceStep(
                    len(steps) + 1,
                    RouteKind.OFFICIAL,
                    adapter.descriptor.provider_id,
                    attempt,
                    StepOutcome.RETRY if retry else StepOutcome.FAILURE,
                    last_failure.reason,
                    (
                        "Official attempt failed; fixed retry scheduled"
                        if retry
                        else last_failure.safe_message
                    ),
                )
            )
            if retry:
                self._sleeper(self._policy.official_retry_delays_seconds[attempt - 1])
                continue
            break
        return None, last_failure

    def _success_outcome(
        self,
        request: ConnectorRequest,
        steps: list[TraceStep],
        route: RouteKind,
        records: tuple[DomainRecord, ...],
    ) -> ExecutionOutcome:
        receipt = self._receipt(
            request,
            tuple(steps),
            ReceiptStatus.SUCCESS,
            route,
            ReasonCode.SUCCESS,
        )
        return ExecutionOutcome(True, records, receipt)

    def _failure_outcome(
        self,
        request: ConnectorRequest,
        steps: list[TraceStep],
        reason: ReasonCode,
    ) -> ExecutionOutcome:
        receipt = self._receipt(request, tuple(steps), ReceiptStatus.FAILURE, None, reason)
        return ExecutionOutcome(False, (), receipt)

    def _receipt(
        self,
        request: ConnectorRequest,
        steps: tuple[TraceStep, ...],
        status: ReceiptStatus,
        selected_route: RouteKind | None,
        final_reason: ReasonCode,
    ) -> ExecutionReceipt:
        return ExecutionReceipt(
            receipt_fingerprint(request, status, selected_route, final_reason, steps),
            request.request_id,
            request.provider_id,
            request.capability,
            status,
            selected_route,
            final_reason,
            self._clock(),
            steps,
        )


def _step(
    index: int,
    route: RouteKind,
    provider_id: str,
    attempt: int,
    failure: ConnectorFailure,
) -> TraceStep:
    return TraceStep(
        index,
        route,
        provider_id,
        attempt,
        StepOutcome.FAILURE,
        failure.reason,
        failure.safe_message,
    )


def _validate_records(
    request: ConnectorRequest,
    records: tuple[DomainRecord, ...],
    descriptor: ProviderDescriptor,
) -> None:
    account_boundary = _request_account_boundary(request)
    for record in records:
        provenance = record.provenance
        if (
            request.capability not in descriptor.capabilities
            or not record_matches_capability(record, request.capability)
            or provenance.provider_id != descriptor.provider_id
            or provenance.family is not descriptor.family
            or provenance.route is not descriptor.route
        ):
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider returned a record outside the selected provenance contract",
            )
        if account_boundary is not None and (
            provenance.account_id is None
            or _canonical_account(provenance.account_id) != account_boundary
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Provider returned a record outside the requested account boundary",
            )


def _request_account_boundary(request: ConnectorRequest) -> str | None:
    payload = request.payload
    if isinstance(payload, (MailQuery, CalendarQuery, TranscriptQuery)) and payload.account_id:
        return _canonical_account(payload.account_id)
    return None


def _canonical_account(value: str) -> str:
    return value.strip().casefold()
