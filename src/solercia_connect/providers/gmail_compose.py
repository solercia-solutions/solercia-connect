"""Gmail drafts over the official API, on the separate ``gmail-compose`` connection.

Decision: docs/decisions/2026-10-25-envio-correo-gmail-dos-pasos.md. The adapter proves the
identity, then emits at most one ``POST`` per invocation and never retries it. A failure after the
``POST`` that does not prove the write was refused is reported as ``write_unverified``, because
Gmail may already have applied it.

``gmail.compose`` cannot list or read messages, so the duplicate check before a send reads Sent
through the account's read-only ``gmail`` connection, handed in as ``sent_reader``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any
from urllib.parse import quote

from solercia_connect.auth import AuthorizationProvider
from solercia_connect.domain import (
    DRAFT_PREVIEW_CHARS,
    MAX_DRAFT_RECIPIENTS,
    Capability,
    ConnectorRequest,
    DomainRecord,
    EmailAddress,
    HealthReport,
    HealthState,
    MailDraft,
    MailDraftRequest,
    MailDraftSendRequest,
    MailSendResult,
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
    require_list,
    require_mapping,
    require_string,
    unique_canonical_account_identity,
)
from solercia_connect.providers.google import (
    GMAIL_BASE_URL,
    addresses_from_header,
    gmail_headers,
    gmail_internal_date,
    google_policy_failure,
)
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
_SEND_ENDPOINT = "/users/me/drafts/send"
# The duplicate check looks back this far and reads at most this many candidates. More candidates
# than the bound block the send: the check cannot clear what it did not read.
_DUPLICATE_LOOKBACK = timedelta(days=30)
_DUPLICATE_PAGE_SIZE = 50
_DUPLICATE_MAX_PAGES = 2
_DUPLICATE_HEADERS = ("Subject", "To", "Cc", "Bcc")


class GmailComposeAdapter:
    descriptor = ProviderDescriptor(
        "google.gmail",
        ProviderFamily.GOOGLE,
        RouteKind.OFFICIAL,
        frozenset({Capability.MAIL_DRAFT_CREATE, Capability.MAIL_DRAFT_SEND}),
    )

    def __init__(
        self,
        transport: HTTPTransport,
        auth: AuthorizationProvider,
        *,
        expected_account: str,
        grant_can_write: bool = True,
        sent_reader: AuthorizationProvider | None = None,
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
        self._sent_reader = sent_reader
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
        if request.capability is Capability.MAIL_DRAFT_SEND and isinstance(
            request.payload, MailDraftSendRequest
        ):
            return self.send_draft(request.payload)
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

    def send_draft(self, request: MailDraftSendRequest) -> tuple[MailSendResult]:
        """Send one existing draft whose sender is the connected account.

        The order is fixed: the authorization flag, the stored grant, the identity, the draft's
        headers, the duplicate check unless the user approved a duplicate, and one ``POST`` with
        the exact body ``{"id": <draft id>}``. Every refusal happens before the ``POST``.
        """
        self._require_write(request.write_authorized)
        account = self._identity(request.account_id)
        draft = self._read_draft(request.draft_id, account)
        if not request.allow_duplicate:
            self._refuse_duplicate(account, draft)
        consequence = "the message may have been sent; check Gmail > Sent before any new attempt"
        payload = self._post(
            f"{self._base_url}{_SEND_ENDPOINT}",
            {"id": request.draft_id},
            "send",
            consequence,
            refiner=_draft_failure,
        )
        try:
            data = require_mapping(payload, "gmail.message")
            message_id = require_string(data.get("id"), "gmail.message.id")
            sent_at = gmail_internal_date(data) if data.get("internalDate") else self._clock()
        except ConnectorFailure:
            raise _unverified("send", ReasonCode.MALFORMED_PROVIDER_DATA, consequence) from None
        return (
            MailSendResult(
                request.draft_id,
                message_id,
                as_string(data.get("threadId")),
                EmailAddress(account),
                draft.to,
                draft.cc,
                draft.bcc,
                draft.subject,
                sent_at,
                self._provenance(message_id, account, _SEND_ENDPOINT),
            ),
        )

    def _read_draft(self, draft_id: str, account: str) -> _DraftHeaders:
        """Read the draft's headers and refuse any draft this capability must not send."""
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}{_DRAFTS_ENDPOINT}/{quote(draft_id, safe='')}",
            headers=self._headers(),
            params={"format": "metadata"},
            failure_refiner=_draft_failure,
        )
        data = require_mapping(payload, "gmail.draft")
        if require_string(data.get("id"), "gmail.draft.id") != draft_id:
            raise ConnectorFailure(
                ReasonCode.MALFORMED_PROVIDER_DATA,
                "Gmail answered with a draft other than the one requested",
            )
        message = require_mapping(data.get("message"), "gmail.draft.message")
        headers = gmail_headers(
            require_mapping(message.get("payload"), "gmail.draft.payload").get("headers")
        )
        senders = addresses_from_header(headers.get("from"))
        if len(senders) != 1 or senders[0].address != account:
            raise ConnectorFailure(
                ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION,
                "The draft's From is not the connected account; only its own drafts are sent",
            )
        draft = _DraftHeaders(
            addresses_from_header(headers.get("to")),
            addresses_from_header(headers.get("cc")),
            addresses_from_header(headers.get("bcc")),
            (headers.get("subject") or "").strip(),
        )
        recipients = len(draft.recipients())
        if not 1 <= recipients <= MAX_DRAFT_RECIPIENTS:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                f"The draft must have between 1 and {MAX_DRAFT_RECIPIENTS} recipients; "
                f"it has {recipients}",
            )
        if not draft.subject:
            raise ConnectorFailure(
                ReasonCode.INVALID_INPUT,
                "The draft has no subject; add one in Gmail before sending it",
            )
        return draft

    def _refuse_duplicate(self, account: str, draft: _DraftHeaders) -> None:
        """Refuse the send when Sent already holds the same subject to the same recipients."""
        unavailable = (
            "The duplicate check reads Sent through the read-only gmail connection of this "
            f"account; run solercia-connect connect gmail {account}, or pass --allow-duplicate "
            "only if the user approves sending without the check"
        )
        if self._sent_reader is None:
            raise ConnectorFailure(ReasonCode.DUPLICATE_CHECK_UNAVAILABLE, unavailable)
        try:
            for candidate in self._sent_candidates(draft.subject):
                sent_at = self._duplicate_sent_at(candidate, draft)
                if sent_at is not None:
                    raise ConnectorFailure(
                        ReasonCode.DUPLICATE_SUSPECTED,
                        "Sent already holds a message with the same subject and recipients, sent "
                        f"at {sent_at.isoformat()}; nothing was sent. Pass --allow-duplicate only "
                        "if the user approves sending it again",
                    )
        except ConnectorFailure as failure:
            if failure.reason is ReasonCode.DUPLICATE_SUSPECTED:
                raise
            raise ConnectorFailure(
                ReasonCode.DUPLICATE_CHECK_UNAVAILABLE,
                f"{unavailable} ({failure.reason.value})",
            ) from None

    def _sent_candidates(self, subject: str) -> list[str]:
        """List, through the read connection, the Sent ids whose subject may match."""
        reader = self._reader()
        self._identity(None, auth=reader)
        # Gmail search has no escape for a quote inside a phrase, so quotes become spaces; the
        # exact comparison happens on the headers read afterwards, not on the search.
        phrase = " ".join(subject.replace('"', " ").split())
        after = int((self._clock() - _DUPLICATE_LOOKBACK).timestamp())
        query = f'in:sent subject:"{phrase}" after:{after}' if phrase else f"in:sent after:{after}"
        params: dict[str, Any] = {"q": query, "maxResults": _DUPLICATE_PAGE_SIZE}
        ids: list[str] = []
        for _ in range(_DUPLICATE_MAX_PAGES):
            payload = request_json(
                self._transport,
                "GET",
                f"{self._base_url}/users/me/messages",
                headers=self._headers(auth=reader),
                params=params,
                failure_refiner=google_policy_failure,
            )
            data = require_mapping(payload, "gmail.messages")
            for raw in require_list(data.get("messages") or [], "gmail.messages.items"):
                item = require_mapping(raw, "gmail.message.reference")
                ids.append(require_string(item.get("id"), "gmail.message.id"))
            token = data.get("nextPageToken")
            if not isinstance(token, str) or not token:
                return ids
            params = {**params, "pageToken": token}
        raise ConnectorFailure(
            ReasonCode.DUPLICATE_SUSPECTED,
            f"More than {_DUPLICATE_PAGE_SIZE * _DUPLICATE_MAX_PAGES} sent messages of the last "
            f"{_DUPLICATE_LOOKBACK.days} days match this subject; nothing was sent. Review Sent "
            "with the user and pass --allow-duplicate only if the user approves",
        )

    def _duplicate_sent_at(self, message_id: str, draft: _DraftHeaders) -> datetime | None:
        """Return the sent instant when this Sent message repeats the draft, else ``None``."""
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/users/me/messages/{quote(message_id, safe='')}",
            headers=self._headers(auth=self._reader()),
            params={"format": "metadata", "metadataHeaders": list(_DUPLICATE_HEADERS)},
            failure_refiner=google_policy_failure,
        )
        data = require_mapping(payload, "gmail.message")
        headers = gmail_headers(
            require_mapping(data.get("payload"), "gmail.message.payload").get("headers")
        )
        sent = _DraftHeaders(
            addresses_from_header(headers.get("to")),
            addresses_from_header(headers.get("cc")),
            addresses_from_header(headers.get("bcc")),
            headers.get("subject") or "",
        )
        same_subject = _normalized_subject(sent.subject) == _normalized_subject(draft.subject)
        if same_subject and sent.recipients() == draft.recipients():
            return gmail_internal_date(data)
        return None

    def _reader(self) -> AuthorizationProvider:
        if self._sent_reader is None:
            raise ConnectorFailure(
                ReasonCode.DUPLICATE_CHECK_UNAVAILABLE,
                "The read-only gmail connection is not available",
            )
        return self._sent_reader

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

    def _identity(self, requested: str | None, *, auth: AuthorizationProvider | None = None) -> str:
        payload = request_json(
            self._transport,
            "GET",
            f"{self._base_url}/users/me/profile",
            headers=self._headers(auth=auth),
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

    def _post(
        self,
        url: str,
        body: Mapping[str, Any],
        what: str,
        consequence: str,
        *,
        refiner: Callable[[int, bytes], ConnectorFailure | None] = google_policy_failure,
    ) -> JsonPayload:
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
                failure_refiner=refiner,
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

    def _headers(
        self, *, json_body: bool = False, auth: AuthorizationProvider | None = None
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": (auth or self._auth).authorization_header(),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers


@dataclass(frozen=True, slots=True)
class _DraftHeaders:
    to: tuple[EmailAddress, ...]
    cc: tuple[EmailAddress, ...]
    bcc: tuple[EmailAddress, ...]
    subject: str

    def recipients(self) -> frozenset[str]:
        return frozenset(item.address for item in (*self.to, *self.cc, *self.bcc))


def _normalized_subject(value: str) -> str:
    return " ".join(value.split()).casefold()


def _draft_failure(status_code: int, content: bytes) -> ConnectorFailure | None:
    """Gmail answers 404 for a draft that does not exist, was sent, or was deleted."""
    if status_code == 404:
        return ConnectorFailure(
            ReasonCode.DRAFT_NOT_FOUND,
            "The Gmail draft was not found; it may have been sent or deleted already",
        )
    return google_policy_failure(status_code, content)


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
