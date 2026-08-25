"""Official-first, deterministic provider connectors."""

from solercia_connect.domain import (
    CalendarEvent,
    Capability,
    ConnectorRequest,
    ExecutionOutcome,
    ExternalIssue,
    MailMessage,
    MeetingTranscript,
    TicketCase,
)
from solercia_connect.registry import ProviderRegistry
from solercia_connect.runtime import ConnectorRuntime, RoutePolicy

__all__ = [
    "CalendarEvent",
    "Capability",
    "ConnectorRequest",
    "ConnectorRuntime",
    "ExecutionOutcome",
    "ExternalIssue",
    "MailMessage",
    "MeetingTranscript",
    "ProviderRegistry",
    "RoutePolicy",
    "TicketCase",
]

__version__ = "0.1.0"
