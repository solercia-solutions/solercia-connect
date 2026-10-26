"""Gmail drafts over the official API, on the separate ``gmail-compose`` connection.

Decision: docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. The adapter proves the
identity, then emits at most one ``POST`` per invocation and never retries it. A failure after the
``POST`` that does not prove the write was refused is reported as ``write_unverified``, because
Gmail may already have applied it.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    DRAFT_PREVIEW_CHARS,
    Capability,
    ConnectorRequest,
    DomainRecord,
    EmailAddress,
    HealthReport,
    HealthState,
    MailDraft,
    MailDraftRequest,
    Provenance,
    ProviderFamily,
    ReasonCode,
    RouteKind,
    utc_now,
)
from solercia_connect.errors import ConnectorFailure
from solercia_connect.ports import HTTPTransport, ProviderDescriptor
from solercia_connect.providers._shared import (
    as_string,
    ensure_official_https_base_url,
    require_mapping,
    require_string,
    unique_canonical_account_identity,
)
from solercia_connect.providers.google import GMAIL_BASE_URL, google_policy_failure
from solercia_connect.transport import JsonPayload, request_json

# Seven-bit transfer encoding keeps the raw message valid whatever characters the body holds; the
# SMTP policy writes CRLF line endings and RFC 2047 encoded headers.
_MESSAGE_POLICY = SMTP.clone(cte_type="7bit")
# Failures after which nothing proves Gmail refused the POST, so the write may have applied.
_UNCONFIRMED_REASONS = frozenset(
    {
        ReasonCode.PROVIDER_UNAVAILABLE,
        ReasonCode.NETWORK_TIMEOUT,
        ReasonCode.NETWORK_UNAVAILABLE,
        ReasonCode.MALFORMED_PROVIDER_DATA,
        ReasonCode.UNKNOWN_ERROR,
    }
)
_DRAFTS_ENDPOINT = "/users/me/drafts"


class GmailComposeAdapter:
    descriptor = ProviderDescriptor(
        "google.gmail",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_DRAFT_CREATE}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str,
        grant_can_write: bool = True,
        base_url: str = GMAIL_BASE_URL,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        account = unique_canonical_account_identity(expected_account, exact=True)
        if account is None:
            raise ValueError("Gmail compose account must be one unambiguous email identity")
        self._transport = transport
        self._auth = auth
        self._account = account
        self._grant_can_write = grant_can_write
        self._base_url = ensure_official_https_base_url(base_url, "Gmail base URL", GMAIL_BASE_URL)
        self._clock = clock

    def health(self) -> HealthReport:
        account = self._identity(None)
        return HealthReport(
            self.descriptor.provider_id,
            HealthState.READY,
            ReasonCode.SUCCESS,
            tuple(sorted(self.descriptor.capabilities)),
            account,
            "Gmail compose identity is reachable",
        )

    def execute(self, request: ConnectorRequest) -> tuple[DomainRecord, ...]:
        if request.capability is Capability.MAIL_DRAFT_CREATE and isinstance(
            request.payload, MailDraftRequest
        ):
            return self.create_draft(request.payload)
        raise ConnectorFailure(
            ReasonCode.CAPABILITY_NOT_IMPLEMENTED,
            "Gmail compose request does not match the draft capability contract",
        )

    def create_draft(self, request: MailDraftRequest) -> tuple[MailDraft]:
        """Create one plain-text draft whose sender is the connected account.

        The order is fixed: the authorization flag, the stored grant, the identity, and one
        ``POST`` of the RFC 822 message. Every refusal happens before the ``POST``.
        """
        self._require_write(request.write_authorized)
        account = self._identity(request.account_id)
        consequence = "the draft may have been created; check Gmail > Drafts before trying again"
        payload = self._post(
            f"{self._base_url}{_DRAFTS_ENDPOINT}",
            {"message": {"raw": _draft_raw(account, request)}},
            "draft",
            consequence,
        )
        try:
            data = require_mapping(payload, "gmail.draft")
            draft_id = require_string(data.get("id"), "gmail.draft.id")
            message = require_mapping(data.get("message"), "gmail.draft.message")
            message_id = require_string(message.get("id"), "gmail.draft.message.id")
        except ConnectorFailure:
            raise _unverified("draft", ReasonCode.MALFORMED_PROVIDER_DATA, consequence) from None
        return (
            MailDraft(
                draft_id,
                message_id,
                as_string(message.get("threadId")),
                EmailAddress(account),
                tuple(EmailAddress(address) for address in request.to),
                tuple(EmailAddress(address) for address in request.cc),
                tuple(EmailAddress(address) for address in request.bcc),
                request.subject,
                request.body[:DRAFT_PREVIEW_CHARS],
                self._provenance(draft_id, account, _DRAFTS_ENDPOINT),
            ),
        )

    def _require_write(self, write_authorized: bool) -> None:
        if not write_authorized:
            raise ConnectorFailure(
                ReasonCode.WRITE_NOT_AUTHORIZED,
                "The Gmail write was not explicitly authorized for this invocation",
            )
        if not self._grant_can_write:
            raise ConnectorFailure(
                ReasonCode.WRITE_SCOPE_MISSING,
                "The stored gmail-compose grant does not include gmail.compose; reconnect it",
            )

    def _identity(self, requested: str | None) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/users/me/profile",
            headers=self._headers(),
            failure_refiner=google_policy_failure,
        )
        data = require_mapping(payload, "gmail.profile")
        observed = unique_canonical_account_identity(data.get("emailAddress"), exact=True)
        requested_boundary = (
            None if requested is None else unique_canonical_account_identity(requested, exact=True)
        )
        if (
            observed is None
            or observed != self._account
            or (requested is not None and observed != requested_boundary)
        ):
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "Authenticated Google account does not match the connected or requested account",
            )
        return observed

    def _post(self, url: str, body: Mapping[str, Any], what: str, consequence: str) -> JsonPayload:
        """Send one POST; a failure that does not prove a refusal becomes ``write_unverified``."""
        # The authorization header is read first: a token refresh that fails happens before the
        # write and keeps its own reason.
        headers = self._headers(json_body=True)
        try:
            return request_json(
                self._transport,
                "POST",
                url,
                headers=headers,
                json_body=body,
                failure_refiner=google_policy_failure,
            )
        except ConnectorFailure as failure:
            if failure.reason in _UNCONFIRMED_REASONS:
                raise _unverified(what, failure.reason, consequence) from None
            raise
        except Exception:
            raise _unverified(what, ReasonCode.UNKNOWN_ERROR, consequence) from None

    def _provenance(self, source_id: str, account: str, endpoint: str) -> Provenance:
        return Provenance(
            self.descriptor.provider_id,
            self.descriptor.family,
            self.descriptor.route,
            source_id,
            self._clock(),
            account,
            endpoint,
        )

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": self._auth.authorization_header(),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers


def _draft_raw(sender: str, request: MailDraftRequest) -> str:
    """Build the RFC 822 message and encode it as Gmail's base64url ``raw`` field."""
    message = EmailMessage(policy=_MESSAGE_POLICY)
    message["From"] = sender
    message["To"] = ", ".join(request.to)
    if request.cc:
        message["Cc"] = ", ".join(request.cc)
    if request.bcc:
        message["Bcc"] = ", ".join(request.bcc)
    message["Subject"] = request.subject
    message.set_content(request.body, subtype="plain", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _unverified(what: str, reason: ReasonCode, consequence: str) -> ConnectorFailure:
    return ConnectorFailure(
        ReasonCode.WRITE_UNVERIFIED,
        f"Gmail did not confirm the {what} ({reason.value}); {consequence}",
    )
