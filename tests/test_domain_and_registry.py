from __future__ import annotations

from datetime import UTC, datetime

import pytest

from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    MailQuery,
    ProviderFamily,
    RouteKind,
)
from solercia_connect.registry import ProviderRegistry
from tests.fakes import FakeAdapter, mail_record


def test_date_window_requires_timezone_and_end_exclusive_order() -> None:
    with pytest.raises(ValueError):
        DateWindow(datetime(2026, 8, 1), datetime(2026, 9, 1))
    with pytest.raises(ValueError):
        DateWindow(
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_request_id_is_stable_for_same_typed_input() -> None:
    payload = MailQuery(
        DateWindow(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
        ),
        "user@example.com",
    )

    first = ConnectorRequest.create("microsoft.graph", Capability.MAIL_SENT_LIST, payload)
    second = ConnectorRequest.create("microsoft.graph", Capability.MAIL_SENT_LIST, payload)

    assert first.request_id == second.request_id
    assert first.request_id.startswith("req-")


def test_registry_rejects_duplicate_official_provider() -> None:
    first = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(),),
    )
    second = FakeAdapter(
        "microsoft.graph",
        ProviderFamily.MICROSOFT,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_SENT_LIST}),
        (mail_record(),),
    )
    registry = ProviderRegistry()
    registry.register(first)

    with pytest.raises(ValueError):
        registry.register(second)


def test_query_limits_are_bounded() -> None:
    window = DateWindow(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 9, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError):
        MailQuery(window, limit=1001)
