from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from solercia_connect.auth import GlpiCredentials
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    HealthReport,
    HealthState,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    TicketCase,
    TicketQuery,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    clean_html_text,
    ensure_https_base_url,
    parse_optional_datetime,
    require_list,
    require_mapping,
    require_string,
)
from solercia_connect.transport import request_json


@dataclass(frozen=True, slots=True)
class GlpiFieldMap:
    ticket_id: str = "2"
    title: str = "1"
    opened_at: str = "15"
    closed_at: str = "16"
    status: str = "12"
    category: str = "7"

    def ordered(self) -> tuple[str, ...]:
        return (
            self.ticket_id,
            self.title,
            self.opened_at,
            self.closed_at,
            self.status,
            self.category,
        )

    def required(self) -> frozenset[str]:
        return frozenset(self.ordered())


class GlpiAdapter:
    descriptor = ProviderDescriptor(
        "glpi.rest",
        ProviderFamily.GLPI,
        RouteKind.OFFICIAL,
        frozenset({Capability.TICKETS_SEARCH}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        credentials: GlpiCredentials,
        *,
        base_url: str,
        fields: GlpiFieldMap | None = None,
        provider_timezone: str = "America/Bogota",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._base_url = ensure_https_base_url(base_url, "GLPI base URL")
        self._fields = fields or GlpiFieldMap()
        self._timezone = ZoneInfo(provider_timezone)
        self._clock = clock

    def health(self) -> HealthReport:
        session = self._open_session()
        try:
            self._validate_search_fields(session)
        except Exception as primary_failure:
            self._close_session_after_failure(session, primary_failure)
            raise
        self._close_session(session)
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(self.descriptor.capabilities),
            None,
            "GLPI session and Ticket search options are reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[TicketCase, ...]:
        if request.capability is Capability.TICKETS_SEARCH and isinstance(
            request.payload, TicketQuery
        ):
            return self.search_tickets(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "GLPI request does not match the ticket capability contract",
        )

    def search_tickets(self, query: TicketQuery) -> tuple[TicketCase, ...]:
        session = self._open_session()
        try:
            self._validate_search_fields(session)
            start_floor = query.window.start.astimezone(self._timezone).date() - timedelta(days=1)
            end_ceiling = query.window.end.astimezone(self._timezone).date()
            params: dict[str, Any] = {
                "criteria[0][field]": self._fields.opened_at,
                "criteria[0][searchtype]": "morethan",
                "criteria[0][value]": start_floor.isoformat(),
                "criteria[1][link]": "AND",
                "criteria[1][field]": self._fields.opened_at,
                "criteria[1][searchtype]": "lessthan",
                "criteria[1][value]": end_ceiling.isoformat(),
                "range": f"0-{query.limit - 1}",
                "sort": self._fields.opened_at,
                "order": "ASC",
            }
            for index, field_id in enumerate(self._fields.ordered()):
                params[f"forcedisplay[{index}]"] = field_id
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/search/Ticket",
                headers=self._credentials.session_headers(session),
                params=params,
            )
            data = require_mapping(payload, "glpi.search")
            raw_tickets = require_list(data.get("data"), "glpi.search.data")
            result: list[TicketCase] = []
            for raw in raw_tickets:
                ticket = self._normalize_ticket(require_mapping(raw, "glpi.ticket"))
                if ticket.opened_at and query.window.start <= ticket.opened_at < query.window.end:
                    result.append(ticket)
            tickets = tuple(result[: query.limit])
        except Exception as primary_failure:
            self._close_session_after_failure(session, primary_failure)
            raise
        self._close_session(session)
        return tickets

    def _open_session(self) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/initSession",
            headers=self._credentials.init_headers(),
        )
        data = require_mapping(payload, "glpi.initSession")
        return require_string(data.get("session_token"), "glpi.session_token")

    def _close_session(self, session: str) -> None:
        try:
            request_json(
                self._transport,
                "GET",
                f"{self._base_url}/killSession",
                headers=self._credentials.session_headers(session),
            )
        except ConnectorFailure:
            raise
        except Exception as exc:
            raise ConnectorFailure(
                ReasonCode.UNKNOWN_ERROR,
                "GLPI session teardown failed with an unknown error",
            ) from exc

    def _close_session_after_failure(self, session: str, primary_failure: Exception) -> None:
        try:
            self._close_session(session)
        except ConnectorFailure as teardown_failure:
            primary_failure.add_note(
                f"GLPI session teardown also failed ({teardown_failure.reason.value})"
            )
            raise primary_failure from teardown_failure

    def _validate_search_fields(self, session: str) -> None:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/listSearchOptions/Ticket",
            headers=self._credentials.session_headers(session),
        )
        options = require_mapping(payload, "glpi.search_options")
        available = {str(key) for key in options}
        if missing := sorted(self._fields.required() - available):
            raise ConnectorFailure(
                ReasonCode.OFFICIAL_CAPABILITY_UNAVAILABLE,
                f"GLPI Ticket search options are missing configured fields: {','.join(missing)}",
            )

    def _normalize_ticket(self, item: dict[str, Any]) -> TicketCase:
        raw_id = item.get(self._fields.ticket_id)
        if raw_id in (None, ""):
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Provider field is not a non-empty string: glpi.ticket.id",
            )
        source_id = str(raw_id)
        opened = parse_optional_datetime(
            item.get(self._fields.opened_at),
            "glpi.ticket.opened_at",
            default_timezone=self._timezone,
        )
        return TicketCase(
            source_id,
            clean_html_text(item.get(self._fields.title)),
            _status_label(item.get(self._fields.status)),
            as_string(item.get(self._fields.category)),
            opened,
            parse_optional_datetime(
                item.get(self._fields.closed_at),
                "glpi.ticket.closed_at",
                default_timezone=self._timezone,
            ),
            Provenance(
                self.descriptor.provider_id,
                self.descriptor.family,
                self.descriptor.route,
                source_id,
                self._clock(),
                None,
                "/search/Ticket",
            ),
        )


def _status_label(value: Any) -> str | None:
    labels = {
        1: "new",
        2: "in_progress_assigned",
        3: "in_progress_planned",
        4: "pending",
        5: "resolved",
        6: "closed",
    }
    try:
        number = int(value)
    except (TypeError, ValueError):
        return as_string(value)
    return labels.get(number, str(number))
