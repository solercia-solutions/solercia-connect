"""Drive and SharePoint reads over the official Microsoft Graph route."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest

from solercia_connect.auth import BearerTokenProvider
from solercia_connect.domain import (
    Capability,
    ConnectorRequest,
    DateWindow,
    FileQuery,
    ReasonCode,
    RouteKind,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import TransportResponse
from solercia_connect.providers.microsoft_graph import MicrosoftGraphAdapter
from solercia_connect.secrets import SecretValue
from tests.fakes import NOW, FakeTransport, json_response

_ME = json_response(200, {"userPrincipalName": "user@example.com", "mail": "user@example.com"})


def _item(name: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"01{name.upper()}",
        "name": name,
        "size": 1024,
        "lastModifiedDateTime": "2026-08-15T10:00:00Z",
        "createdDateTime": "2026-08-01T10:00:00Z",
        "file": {"mimeType": "application/pdf"},
        "webUrl": f"https://example.sharepoint.com/{name}",
        "parentReference": {"driveId": "b!drive", "path": "/drive/root:/Documentos"},
    }
    payload.update(overrides)
    return payload


def _adapter(*responses: TransportResponse) -> tuple[MicrosoftGraphAdapter, FakeTransport]:
    transport = FakeTransport(*responses)
    adapter = MicrosoftGraphAdapter(
        transport,
        BearerTokenProvider(SecretValue("token")),
        expected_account="user@example.com",
        clock=lambda: NOW,
    )
    return adapter, transport


def test_root_listing_is_normalized_and_carries_official_provenance() -> None:
    adapter, transport = _adapter(_ME, json_response(200, {"value": [_item("acta.pdf")]}))

    files = adapter.list_files(FileQuery())

    assert len(files) == 1
    item = files[0]
    assert item.name == "acta.pdf"
    assert item.size_bytes == 1024
    assert item.mime_type == "application/pdf"
    assert item.path == "/drive/root:/Documentos/acta.pdf"
    assert item.is_folder is False
    assert item.content_base64 is None
    assert item.provenance.route is RouteKind.OFFICIAL
    assert item.provenance.account_id == "user@example.com"
    assert transport.requests[-1].url.endswith("/me/drive/root/children")


def test_folder_listing_targets_the_requested_path() -> None:
    adapter, transport = _adapter(_ME, json_response(200, {"value": []}))

    assert adapter.list_files(FileQuery(folder_path="/Documentos/2026")) == ()
    assert transport.requests[-1].url.endswith("/me/drive/root:/Documentos/2026:/children")


def test_search_uses_the_provider_search_endpoint_and_escapes_quotes() -> None:
    adapter, transport = _adapter(_ME, json_response(200, {"value": []}))

    adapter.list_files(FileQuery(text="acta d'agosto"))

    assert "/me/drive/root/search(q='" in transport.requests[-1].url


def test_a_sharepoint_site_replaces_the_personal_drive() -> None:
    adapter, transport = _adapter(_ME, json_response(200, {"value": []}))

    adapter.list_files(FileQuery(site="contoso.sharepoint.com:/sites/calidad"))

    assert "/sites/contoso.sharepoint.com:/sites/calidad/drive/root/children" in (
        transport.requests[-1].url
    )


def test_folders_are_reported_as_folders() -> None:
    adapter, _ = _adapter(
        _ME,
        json_response(200, {"value": [_item("Actas", file=None, folder={"childCount": 3})]}),
    )

    files = adapter.list_files(FileQuery())

    assert files[0].is_folder is True


def test_a_window_filters_by_last_modification() -> None:
    adapter, _ = _adapter(
        _ME,
        json_response(
            200,
            {
                "value": [
                    _item("dentro.pdf", lastModifiedDateTime="2026-08-15T10:00:00Z"),
                    _item("fuera.pdf", lastModifiedDateTime="2026-07-02T10:00:00Z"),
                ]
            },
        ),
    )

    files = adapter.list_files(
        FileQuery(
            window=DateWindow(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
        )
    )

    assert [item.name for item in files] == ["dentro.pdf"]


def test_files_get_returns_the_item_with_its_content() -> None:
    content = b"%PDF-1.7 minimal"
    adapter, transport = _adapter(
        _ME,
        json_response(200, _item("acta.pdf")),
        TransportResponse(200, {"Content-Type": "application/pdf"}, content),
    )

    files = adapter.list_files(FileQuery(item_id="01ACTA.PDF"), with_content=True)

    assert files[0].content_base64 == base64.b64encode(content).decode("ascii")
    assert transport.requests[-1].url.endswith("/content")


def test_content_larger_than_the_budget_is_refused_before_download() -> None:
    adapter, _ = _adapter(_ME, json_response(200, _item("grande.pdf", size=50_000)))

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_files(
            FileQuery(item_id="01GRANDE.PDF", max_content_bytes=1_000),
            with_content=True,
        )

    assert captured.value.reason is ReasonCode.INVALID_INPUT


def test_a_foreign_account_is_refused_before_any_drive_call() -> None:
    adapter, transport = _adapter(_ME)

    with pytest.raises(ConnectorFailure) as captured:
        adapter.list_files(FileQuery(account_id="otro@example.com"))

    assert captured.value.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION
    assert len(transport.requests) == 1


def test_the_capability_pair_routes_through_execute() -> None:
    adapter, _ = _adapter(_ME, json_response(200, {"value": [_item("acta.pdf")]}))

    records = adapter.execute(
        ConnectorRequest.create("microsoft.graph", Capability.FILES_SEARCH, FileQuery(text="acta"))
    )

    assert [record.name for record in records] == ["acta.pdf"]
