# Diseño: `mail.inbox.list` para Outlook (rutas oficial y navegador)

Fase `sdd-design` del cambio `46-mail-inbox-list`. Entradas: propuesta (Engram #3698,
`proposal.md`) y exploración (Engram #3695, `exploration.md`). Decisiones cerradas A3, B2, C2+C3,
D1 y «Gmail fuera de alcance» se implementan, no se reabren.

## Enfoque técnico

La capacidad se declara en **siete puntos** y se despacha en **cuatro**. Cada ruta conserva **un
solo constructor de petición**, parametrizado por carpeta mediante un helper privado, y **una sola
tabla** que asocia carpeta → proyección `$select` → valor del campo `folder` del registro. Esa tabla
es el mecanismo que impide que las dos proyecciones diverjan y que un registro de bandeja pierda
`sentDateTime` (riesgo 2 de la propuesta: cada registro fallaría con `malformed_provider_data`).

`ports.py` gana un puerto semántico propio para la operación nueva; `MailQuery`, `list_sent_mail` y
las firmas existentes no cambian, de modo que `request_id`/`receipt_id` de `mail.sent.list` quedan
idénticos bit a bit.

## Decisiones de arquitectura

### D1 — La operación de bandeja se nombra en `ports.py` como puerto propio

**Elección**: agregar `InboxMailProviderPort` (Protocol) con
`list_inbox_mail(self, query: MailQuery) -> tuple[MailMessage, ...]`. `MailProviderPort` no cambia.

**Alternativas rechazadas**:

| Opción | Por qué se rechaza |
| --- | --- |
| Agregar `list_inbox_mail` a `MailProviderPort` | `GoogleGmailAdapter` no la implementa (Gmail fuera de alcance). El puerto pasaría a describir una operación que un adaptador conforme no ofrece: documentación falsa. Hoy nadie tipa contra el protocolo, así que `mypy` no lo detectaría. |
| Sólo documentar en docstring, despacho puro (A3 literal) | `rules.design` exige mantener los proveedores «detrás de puertos semánticos» y el ADR §5.2/§6.2 exige que los puertos «expresen operaciones semánticas». Una segunda lectura invisible desde `ports.py` contradice ambas. |
| `MailQuery.folder` (A2) | Cambia `request_id`/`receipt_id` de peticiones existentes (`ConnectorRequest.create` serializa el payload completo, `domain.py:392-402`). |

**Justificación**: el puerto nombra la operación (contrato legible) y la segregación por protocolo
dice la verdad sobre qué adaptador ofrece qué. Es compatible con A3: el puerto declara la operación,
el helper privado evita duplicar el constructor de petición.

### D2 — Carpeta parametrizada dentro del adaptador, con tabla única de proyección (A3)

**Elección**: helper privado por ruta + un `dict` con clave `Literal` que resuelve `$select` y el
valor de `folder`. `list_sent_mail` y `list_inbox_mail` delegan en el mismo helper.

**Alternativa rechazada**: dos funciones completas por adaptador (≈4 copias del `$select`).
**Justificación**: una sola proyección por ruta; el `$select` no puede divergir porque no existe dos
veces. La tabla es exhaustiva sobre un `Literal`, así que `mypy --strict` verifica el acceso.

### D3 — Tres campos aditivos al final de `MailMessage` (B2)

**Elección**: `internet_message_id: str | None = None`, `folder: str | None = None`,
`received_at: datetime | None = None`, **después de `attachments`** y con default.

**Alternativa rechazada**: reemplazar `sent_at` por un instante genérico (B3) — ruptura del contrato
publicado. **Justificación**: `MailMessage` se construye posicionalmente en `_microsoft.py:111`,
`google.py:187` y `tests/fakes.py:232`; cualquier campo intercalado rompe los tres. `to_primitive`
recorre `fields(dataclass)` (`serialization.py:19-20`), así que los campos aparecen solos en el JSON
sin subir `solercia.connect/record/v1`.

`folder` se tipa `str | None` en el dataclass (inercia de esquema) pero el **normalizador** recibe
`Literal["inbox", "sent"]`: un `"Inbox"` mal escrito sería un registro silenciosamente incorrecto, y
el `Literal` lo convierte en error de compilación.

### D4 — `folder` es un argumento explícito, nunca derivado de la procedencia

**Elección**: `normalize_mail_message(..., *, folder: MailFolderName)`.
**Alternativa rechazada**: derivar de `provenance.endpoint`.
**Justificación**: el adaptador de navegador **sobrescribe** `endpoint` con la evidencia de identidad
`"outlook-rest-v2-me"` (`adapter.py:372-388`), borrando cualquier rastro de carpeta. Derivarlo sería
correcto en Graph e incorrecto en navegador: exactamente la clase de asimetría que la paridad de
rutas prohíbe.

### D5 — `internet_message_id` es la clave de negocio de deduplicación

**Elección**: incorporar `internetMessageId`/`InternetMessageId` al `$select` de **ambas** carpetas
en **ambas** rutas. **Justificación**: ADR §5.5 exige «claves de negocio o idempotencia que impidan
duplicados ante reintentos o relecturas». El `id` de Outlook es opaco y **distinto** para el mismo
mensaje en Enviados y en Bandeja; el Message-ID RFC 5322 es el único identificador estable que
permite unir ambos lados de una conversación. Si el proveedor lo omite, el campo queda `None` y el
registro no falla: es evidencia ausente, no dato inventado.

### D6 — `limit` hasta 1000 y `$orderby` ascendente en bandeja (C2+C3)

**Elección**: `MailQuery.limit` pasa a `1..1000`; el recorte de `application.py` sube a
`min(limit, 1000)`; bandeja ordena ascendente en las dos rutas; `mail.sent.list` **no cambia de
orden**.

**Alternativa rechazada**: dejar 100 y segmentar por mes (C1) — no resuelve el caso real y la
truncación es silenciosa. **Justificación**: 1000 es el techo estructural del navegador
(`_OUTLOOK_MAX_PAGES` 20 × `_OUTLOOK_PAGE_SIZE` 50) y el valor que ya usan `CalendarQuery`, la CLI
(`cli.py:229`) y el servicio (`service.py:79`): cierra una incoherencia de cuatro capas en lugar de
inventar un número. El orden ascendente hace la truncación **determinista y reanudable**: al tocar
el tope se devuelve el prefijo temporal más antiguo y el operador estrecha la ventana.

### D7 — Ambas rutas en el mismo cambio (D1)

**Elección**: Graph y navegador simétricos. **Alternativa rechazada**: sólo navegador primero.
**Justificación**: «los registros son idénticos entre rutas» es contrato publicado en cuatro
documentos y verificado por `tests/test_route_contract_parity.py`. Romperlo por conveniencia obliga
a un segundo cambio y a documentar una excepción.

## Flujo de datos

```
CLI/Servicio ──► application._query ──► ConnectorRequest(MAIL_INBOX_LIST, MailQuery)
                                              │
                                              ▼
                                   runtime.execute (oficial primero)
                        ┌─────────────────────┴─────────────────────┐
                        ▼                                           ▼
        MicrosoftGraphAdapter.execute              [fallo allowlisteado + --allow-browser-fallback]
                        │                          Microsoft365BrowserAdapter._prepare (identidad)
        list_inbox_mail(query)                                      │
                        │                                  driver.read_records
        _list_folder_mail(folder="inbox",                           │
            time_field="receivedDateTime", order="asc")   _read_folder_mail(url=INBOX,
                        │                                     time_field="ReceivedDateTime")
                        └──────────────► normalize_mail_message(item, provenance=…, ◄────┘
                                                      folder="inbox")
                                                             │
                                    runtime._validate_records (tipo, provider, ruta, cuenta)
                                                             │
                                          envelope.versioned_record ──► JSON (--output)
```

## Cambios por archivo

| Archivo | Acción | Cambio |
| --- | --- | --- |
| `domain.py` | Modificar | `Capability.MAIL_INBOX_LIST = "mail.inbox.list"`; `_CAPABILITY_RECORD_TYPES[MAIL_INBOX_LIST] = MailMessage`; tres campos al final de `MailMessage`; `MailQuery.limit` `1..1000` |
| `providers/_microsoft.py` | Modificar | `MailFolderName = Literal["inbox", "sent"]`; `normalize_mail_message(..., *, folder)`; lectura de `internetMessageId`/`receivedDateTime` con `field()` |
| `ports.py` | Modificar | `InboxMailProviderPort` (Protocol) |
| `providers/microsoft_graph.py` | Modificar | descriptor; rama en `execute`; `_list_folder_mail`; `list_sent_mail`/`list_inbox_mail`; `_normalize_message` con `endpoint`/`folder`; constantes de `$select` |
| `providers/google.py` | Modificar | `folder="sent"` al construir `MailMessage` (una línea) |
| `providers/m365_browser/selectors.py` | Modificar | `outlook_inbox_messages` |
| `providers/m365_browser/driver.py` | Modificar | rama en `read_records`; `_read_folder_mail`; constantes de `$select`; `_normalize_outlook_message(..., folder)` |
| `providers/m365_browser/adapter.py` | Modificar | descriptor; `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]` |
| `runtime.py` | Modificar | `browser_fallback_capabilities` |
| `config.py` | Modificar | `TARGET_SPECS` Outlook: `"mail.inbox.list"` |
| `application.py` | Modificar | rama de correo cubre ambas capacidades; recorte `min(limit, 1000)` |
| `cli.py` | Modificar | `_query_payload`: rama de correo + cola **fail-closed** |
| `matrix.py` | Modificar | dos filas nuevas |
| `tests/fakes.py` | Modificar | `mail_record()` con los tres campos |
| Docs (5 archivos) | Modificar | ver sección «Documentación» |

**Sin cambio**: `service.py` (`capability` es `str` libre), `envelope.py`, `serialization.py`,
`registry.py`, `bootstrap.py`, `doctor.py` (deriva de `TARGET_SPECS`), allowlist de hosts.

## Contratos e interfaces

```python
# ports.py — el puerto nombra la operación; Gmail sigue conforme a MailProviderPort y no a este.
class InboxMailProviderPort(Protocol):
    """Read the mailbox's received collection. Segregated from MailProviderPort because a
    provider can serve sent mail without serving inbox mail."""

    def list_inbox_mail(self, query: MailQuery) -> tuple[MailMessage, ...]: ...


# providers/_microsoft.py
MailFolderName = Literal["inbox", "sent"]


def normalize_mail_message(
    item: Mapping[str, Any], *, provenance: Provenance, folder: MailFolderName
) -> MailMessage:
    ...
    return MailMessage(
        ...,  # posicionales existentes, sin reordenar
        provenance,
        internet_message_id=(
            value.strip()
            if isinstance(value := field(item, "internetMessageId", "InternetMessageId"), str)
            and value.strip()
            else None
        ),
        folder=folder,
        received_at=optional_microsoft_datetime(
            field(item, "receivedDateTime", "ReceivedDateTime")
        ),
    )


# providers/microsoft_graph.py
_GRAPH_MAIL_SELECT_BASE = (
    "id",
    "subject",
    "sentDateTime",
    "from",
    "sender",
    "toRecipients",
    "ccRecipients",
    "body",
    "conversationId",
    "hasAttachments",
    "internetMessageId",
)
_GRAPH_SENT_SELECT = ",".join(_GRAPH_MAIL_SELECT_BASE)  # 11 nombres
_GRAPH_INBOX_SELECT = ",".join((*_GRAPH_MAIL_SELECT_BASE, "receivedDateTime"))  # 12 nombres

_GraphMailFolder = Literal["inbox", "sentitems"]
_GraphMailTimeField = Literal["receivedDateTime", "sentDateTime"]
# Tabla única: carpeta → (proyección, valor del campo `folder` del registro).
_GRAPH_MAIL_FOLDERS: dict[_GraphMailFolder, tuple[str, MailFolderName]] = {
    "sentitems": (_GRAPH_SENT_SELECT, "sent"),
    "inbox": (_GRAPH_INBOX_SELECT, "inbox"),
}


def list_sent_mail(self, query: MailQuery) -> tuple[MailMessage, ...]:
    return self._list_folder_mail(
        query, folder="sentitems", time_field="sentDateTime", order="desc"
    )


def list_inbox_mail(self, query: MailQuery) -> tuple[MailMessage, ...]:
    return self._list_folder_mail(query, folder="inbox", time_field="receivedDateTime", order="asc")


def _list_folder_mail(
    self,
    query: MailQuery,
    *,
    folder: _GraphMailFolder,
    time_field: _GraphMailTimeField,
    order: Literal["asc", "desc"],
) -> tuple[MailMessage, ...]:
    account_id = self._identity()  # identidad canónica ANTES de cualquier lectura
    ...  # frontera de cuenta, idéntica a hoy
    select, record_folder = _GRAPH_MAIL_FOLDERS[folder]
    endpoint = f"/me/mailFolders/{folder}/messages"  # `folder` es Literal, nunca dato de usuario
    params: dict[str, Any] = {
        "$filter": (
            f"{time_field} ge {_graph_datetime(query.window.start)} "
            f"and {time_field} lt {_graph_datetime(query.window.end)}"
        ),
        "$select": select,
        "$orderby": f"{time_field} {order}",
        "$top": min(query.limit, 100),
    }
    items = self._paged_values(f"{self._base_url}{endpoint}", params, query.limit)
    ...


# providers/m365_browser/selectors.py
outlook_inbox_messages: str = "https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages"

# providers/m365_browser/driver.py
_OUTLOOK_MAIL_SELECT_BASE = (
    "Id",
    "Subject",
    "SentDateTime",
    "From",
    "ToRecipients",
    "CcRecipients",
    "Body",
    "ConversationId",
    "HasAttachments",
    "InternetMessageId",
)
_SENT_MAIL_SELECT = ",".join(_OUTLOOK_MAIL_SELECT_BASE)  # 10 nombres
_INBOX_MAIL_SELECT = ",".join((*_OUTLOOK_MAIL_SELECT_BASE, "ReceivedDateTime"))  # 11 nombres

_OutlookTimeField = Literal["SentDateTime", "ReceivedDateTime"]
# En esta ruta el campo temporal ES el discriminador de carpeta: una sola tabla lo declara.
_OUTLOOK_MAIL_READS: dict[_OutlookTimeField, tuple[str, MailFolderName]] = {
    "SentDateTime": (_SENT_MAIL_SELECT, "sent"),
    "ReceivedDateTime": (_INBOX_MAIL_SELECT, "inbox"),
}


def _read_folder_mail(
    self, query: MailQuery, *, url: str, time_field: _OutlookTimeField, timeout_ms: int
) -> tuple[MailMessage, ...]:
    select, folder = _OUTLOOK_MAIL_READS[time_field]
    params: Mapping[str, str] | None = {
        "$filter": (
            f"{time_field} ge {_outlook_timestamp(query.window.start)}"
            f" and {time_field} lt {_outlook_timestamp(query.window.end)}"
        ),
        "$select": select,
        "$orderby": f"{time_field} asc",  # enviados ya era asc: el orden no cambia
        "$top": str(min(_OUTLOOK_PAGE_SIZE, query.limit)),
    }
    ...  # paginación, allowlist y cortes idénticos a hoy
```

`cli._query_payload` cierra la cola: rama explícita `Capability.TICKETS_SEARCH → TicketQuery` y
`raise _InputError(f"Unsupported capability for the legacy query helper: {capability}")` al final.
Hoy cualquier capacidad no contemplada cae **silenciosamente** en `TicketQuery` (riesgo 5).

## Procedencia y endpoints declarados

| Ruta | Endpoint HTTP | `provenance.endpoint` | Por qué |
| --- | --- | --- | --- |
| Graph enviados | `{base}/me/mailFolders/sentitems/messages` | `/me/mailFolders/sentitems/messages` | sin cambio |
| Graph bandeja | `{base}/me/mailFolders/inbox/messages` | `/me/mailFolders/inbox/messages` | mismo patrón, derivado del `Literal` de carpeta |
| Navegador enviados | `ENDPOINTS.outlook_sent_messages` | `outlook-rest-v2` → sobrescrito a `outlook-rest-v2-me` | sin cambio |
| Navegador bandeja | `ENDPOINTS.outlook_inbox_messages` | `outlook-rest-v2` → sobrescrito a `outlook-rest-v2-me` | el adaptador reemplaza la procedencia por **evidencia de identidad** (`adapter.py:372-388`) |

Por eso `folder` es un campo explícito y no una lectura de la procedencia: en la ruta navegador la
procedencia deliberadamente **no** nombra el recurso, sino la identidad verificada.

## Compatibilidad de peticiones y recibos

| Elemento | Estado | Evidencia |
| --- | --- | --- |
| `request_id` de `mail.sent.list` | **sin cambio** | `ConnectorRequest.create` (`domain.py:392-402`) mezcla `provider_id`, `capability.value` y el payload; ni `MailQuery` ni sus valores por defecto cambian |
| `receipt_id` de `mail.sent.list` | **sin cambio** | `receipt_fingerprint` (`domain.py:487-504`) mezcla petición + pasos; nunca registros, orden ni conteo |
| `mail.inbox.list` | ids **nuevos** | `capability.value` distinto ⇒ hash distinto: es una petición distinta, no una colisión |
| `solercia.connect/v1` y `/record/v1` | **sin subir versión** | `to_primitive` recorre `fields()`; los tres campos son claves nuevas e inertes |
| Esquema HTTP | **sin cambio** | `RunRequest.capability` es `str` libre (`service.py:62`); `limit` ya admitía `le=1000` |

`limit=1000` en `mail.sent.list` **sí** cambia el `request_id` de una petición que antes era
imposible de construir (lanzaba `ValueError`): no hay petición previa con la que colisionar.

## Política de runtime y fail-closed

`RoutePolicy.browser_fallback_capabilities` suma `MAIL_INBOX_LIST`; ambos descriptores la declaran;
`_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]` pasa a
`(MAIL_SENT_LIST, MAIL_INBOX_LIST, CALENDAR_EVENTS_LIST)`.

**Sin cambio**: ningún `ReasonCode` nuevo; `browser_fallback_reasons` intacto; allowlist de hosts
intacta (`outlook.office.com` ya permitido); `_await_outlook_bearer` sigue reentrando a
`ENDPOINTS.outlook_sent` — el Bearer capturado sirve para toda la API de correo del mismo origen, así
que la página de arranque no cambia; `oauth_scopes` intacto (`Mail.Read` ya cubre la bandeja).

Las cinco capas de opt-in del fallback siguen exigiéndose íntegras: `--allow-browser-fallback` por
invocación, razón allowlisteada, capacidad allowlisteada, capacidad en el descriptor y capacidad en
la superficie de extracción. Agregar la capacidad a esas listas **no relaja** ninguna.

## Matriz de amenazas

La matriz de `references/threat-matrix.md` cubre fronteras de shell, Git y automatización de PR. Este
cambio no toca ninguna:

| Frontera | Aplicabilidad |
| --- | --- |
| Rutas tipo documentación | N/A — no se clasifican ni ejecutan archivos; sólo se editan `.md` como texto |
| Selección de repositorio Git | N/A — no se invoca Git ni se resuelve `cwd` |
| Estado de commit | N/A — el cambio no automatiza commits |
| Estado de push | N/A — el cambio no automatiza push |
| Comandos de PR | N/A — el cambio no compone comandos de PR |

Las fronteras adversariales **reales** de este cambio son de proveedor y quedan cubiertas así:

| Frontera | Comportamiento seguro | Comportamiento ante fallo | Prueba en rojo |
| --- | --- | --- | --- |
| Construcción de endpoint | Endpoint derivado de un `Literal` cerrado, nunca de datos del usuario | No existe entrada de usuario que lo alcance | `test_microsoft_graph_inbox_reads_the_declared_inbox_folder_collection` fija la URL exacta |
| `nextLink` de paginación | Graph: `require_same_origin_https_url`; navegador: `_is_allowed_outlook_request_url` en cada GET | Rechazo antes de enviar credenciales | Se reutilizan las pruebas existentes (`test_provider_contracts.py:448-490`); la bandeja pasa por el mismo `_paged_values` |
| Frontera de cuenta | `_identity()` es la primera línea de `_list_folder_mail`; `_validate_records` valida cada registro | `authorization_boundary_violation` | `test_microsoft_graph_inbox_refuses_a_mailbox_that_is_not_the_authenticated_account` |
| Proyección incompleta | `$select` de bandeja conserva `sentDateTime`/`SentDateTime` | `malformed_provider_data` por registro | Conjunto exacto de `$select` aseverado en ambas rutas |
| Secretos | `_TransientBearer` no imprimible, limpiado en `finally` | sin cambio | sin cambio |

## Estrategia de pruebas (TDD estricto, hermético)

Orden obligatorio: **rojo primero, un archivo por paso**. Comando de prueba:
`make test` (`uv run … pytest -m 'not live'`); estáticos: `make check`
(`ruff format --check` + `ruff check` + `mypy src`). Cobertura de ramas ≥ 80 %.

| # | Archivo | Pruebas a agregar/modificar | Aserciones exactas |
| --- | --- | --- | --- |
| 1 | `tests/test_route_contract_parity.py` | **Modificar** `_GRAPH_MESSAGE` (+`internetMessageId`, `receivedDateTime`) y `_BROWSER_MESSAGE` (+`InternetMessageId`, `ReceivedDateTime`) con **valores concretos**; **modificar** `test_a_mail_record_is_identical_whichever_route_produced_it` (pasar `folder="inbox"` a ambos) | `official.internet_message_id == browser.internet_message_id == "<abc@contoso.com>"`; `official.received_at == browser.received_at == datetime(2026,8,14,9,32, tzinfo=UTC)`; `official.folder == browser.folder == "inbox"`; **ninguna** comparación contra `None`; `replace(official, provenance=browser.provenance) == browser` sigue en pie |
| 1b | `tests/test_route_contract_parity.py` | **Agregar** `test_a_sent_mail_record_declares_its_folder_and_no_received_instant` | `folder == "sent"`; `received_at is None` |
| 2 | `tests/test_provider_contracts.py` | **Agregar** `test_microsoft_graph_inbox_reads_the_declared_inbox_folder_collection` | `transport.requests[1].url == "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"`; `params["$filter"] == "receivedDateTime ge 2026-08-01T00:00:00Z and receivedDateTime lt 2026-09-01T00:00:00Z"`; `params["$orderby"] == "receivedDateTime asc"`; `params["$top"] == 100`; `set(params["$select"].split(",")) == {id, subject, sentDateTime, receivedDateTime, from, sender, toRecipients, ccRecipients, body, conversationId, hasAttachments, internetMessageId}` (12) |
| 2b | `tests/test_provider_contracts.py` | **Agregar** `test_microsoft_graph_inbox_record_carries_folder_message_id_and_received_instant` | `folder == "inbox"`; `received_at` es el instante de `receivedDateTime`; `sent_at` es el de `sentDateTime` y **son distintos**; `internet_message_id == "<abc@contoso.com>"`; `provenance.endpoint == "/me/mailFolders/inbox/messages"` |
| 2c | `tests/test_provider_contracts.py` | **Agregar** `test_microsoft_graph_inbox_refuses_a_mailbox_that_is_not_the_authenticated_account` | `ConnectorFailure.reason is ReasonCode.AUTHORIZATION_BOUNDARY_VIOLATION`; `len(transport.requests) == 1` (identidad y nada más) |
| 2d | `tests/test_provider_contracts.py` | **Agregar** `test_microsoft_graph_inbox_without_sent_instant_fails_closed` | `ReasonCode.MALFORMED_PROVIDER_DATA` cuando la respuesta omite `sentDateTime` |
| 2e | `tests/test_provider_contracts.py` | **Modificar** `test_microsoft_graph_normalizes_full_sent_message` | `set(params["$select"].split(","))` incluye `internetMessageId` y **no** `receivedDateTime`; `$orderby` sigue `"sentDateTime desc"`; `folder == "sent"`; `received_at is None` |
| 3 | `tests/test_browser_mail_extraction.py` | **Agregar** `_inbox_message(index, **overrides)` y `_read_inbox(driver, limit=50)`; `test_inbox_mail_is_normalized_from_the_documented_rest_collection` | `request.calls[0]["url"] == "https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages"`; `calls[0]["authorized"] is True`; `folder == "inbox"`; `received_at` correcto |
| 3b | `tests/test_browser_mail_extraction.py` | **Agregar** `test_inbox_mail_query_is_end_exclusive_and_asks_only_for_needed_fields` | `params["$filter"] == "ReceivedDateTime ge 2026-08-01T00:00:00Z and ReceivedDateTime lt 2026-09-01T00:00:00Z"`; `params["$orderby"] == "ReceivedDateTime asc"`; `set(params["$select"].split(",")) == {Id, Subject, SentDateTime, ReceivedDateTime, From, ToRecipients, CcRecipients, Body, ConversationId, HasAttachments, InternetMessageId}` (11) |
| 3c | `tests/test_browser_mail_extraction.py` | **Agregar** `test_inbox_mail_follows_the_provider_next_link` y `test_inbox_mail_stops_at_the_requested_limit` | ids en orden `["AAMk1","AAMk2"]`; segunda llamada con `params is None`; corte exacto en `limit=2` |
| 3d | `tests/test_browser_mail_extraction.py` | **Modificar** `test_sent_mail_query_is_end_exclusive_and_asks_only_for_needed_fields` (líneas 221-231) | el conjunto exacto pasa a 10 nombres incluyendo `InternetMessageId`; `$orderby` sigue `"SentDateTime asc"` |
| 4 | `tests/test_runtime_policy.py` | **Agregar** `test_inbox_mail_is_allowlisted_for_the_microsoft_browser_fallback` | `RoutePolicy().allows_browser(request(MAIL_INBOX_LIST), ConnectorFailure(OFFICIAL_CAPABILITY_UNAVAILABLE), ProviderFamily.MICROSOFT) is True`; `allows_browser(..., ProviderFamily.GOOGLE) is False`; `Capability.MAIL_INBOX_LIST in RoutePolicy().browser_fallback_capabilities` |
| 4b | `tests/test_outlook_browser_identity.py` (línea 102) y `tests/test_connections.py` (línea 593) | **Modificar** las listas exactas | `capabilities == ["mail.sent.list", "mail.inbox.list", "calendar.events.list"]` en ambas |
| 4c | `tests/test_browser_foundation.py` | **Agregar** `test_browser_extraction_report_declares_inbox_for_outlook` | `browser_extraction_report("outlook") == {"extraction_status": "available", "capabilities": ["mail.sent.list", "mail.inbox.list", "calendar.events.list"]}`; `browser_surface_for_capability(MAIL_INBOX_LIST) is ConnectionType.OUTLOOK` |
| 5 | `tests/test_application.py` | **Agregar** `test_inbox_mail_runs_end_to_end_over_the_official_route` | envolvente `ok is True`; `receipt.capability is Capability.MAIL_INBOX_LIST`; cada registro `folder == "inbox"`; recorte `min(limit, 1000)` aplicado |
| 5b | `tests/test_application.py` | **Agregar** `test_mail_query_accepts_the_thousand_record_limit_and_rejects_more` | `MailQuery(window, limit=1000)` construye; `MailQuery(window, limit=1001)` lanza `ValueError("MailQuery limit must be between 1 and 1000")`; `limit=0` sigue rechazado |
| 5c | `tests/test_service.py` | **Agregar** `test_service_runs_the_inbox_capability_and_rejects_the_browser_route` | `RunRequest(capability="mail.inbox.list", limit=1000)` responde `200` por ruta oficial; con fallback pedido responde `action_required` con el comando exacto de CLI |
| 5d | `tests/test_cli_lifecycle.py` | **Modificar** `test_legacy_query_helpers_cover_all_capability_shapes_and_provider_mapping` (líneas 362-364): sustituir `assert query is not None` por un mapa capacidad → tipo esperado; **agregar** `test_legacy_query_helper_fails_closed_on_an_unknown_capability` | `isinstance(cli._query_payload(MAIL_INBOX_LIST, **common), MailQuery)`; `pytest.raises(cli._InputError)` al pasar `cast(Capability, "mail.archive.list")`; `_target_for_provider("microsoft.graph", MAIL_INBOX_LIST) is ConnectionType.OUTLOOK` |
| 6 | `tests/test_cli_and_docs.py` | **Agregar** `test_docs_declare_the_inbox_capability_for_both_routes` | `"mail.inbox.list"` presente en `README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md` y `agents/skills/solercia-connect/SKILL.md`; dos filas con `capability == "mail.inbox.list"` en `CAPABILITY_MATRIX`, una por canal (`official`, `browser`) |
| — | `tests/fakes.py` | **Modificar** `mail_record()` | acepta `folder: MailFolderName = "sent"`, `internet_message_id`, `received_at`; los pasa por palabra clave |
| — | `tests/live/test_outlook_live.py` | **Agregar** caso `live` de bandeja | opt-in `make test-live TYPE=outlook ACCOUNT=…`; valida `InternetMessageId` contra el proveedor real |

**Puerta de salida por PR**: `make test` en verde sin regresión sobre la línea base de 402 pruebas y
`make check` sin hallazgos (`mypy src` en modo estricto incluido).

## Documentación

| Archivo | Sección | Cambio |
| --- | --- | --- |
| `README.md` | «What each route can read» (líneas 90-99) | fila `\| `mail.inbox.list` \| yes \| yes \|` tras `mail.sent.list` |
| `README.md` | ejemplo JSON de `connected_browser` (líneas 41-44) | `"capabilities": ["mail.sent.list", "mail.inbox.list"]` |
| `docs/runbooks/outlook.md` | «Explicit browser fallback» (líneas 39-43) | `outlook` reporta `mail.sent.list`, `mail.inbox.list` y `calendar.events.list` |
| `docs/runbooks/outlook.md` | nueva subsección «Leer la bandeja de entrada» | el ejemplo completo de abajo + la guía de ventana |
| `docs/USO-CON-AGENTES.md` | «4. Leer» (líneas 59-73) | agregar `mail.inbox.list` a la lista de capacidades intercambiables y la guía de ventana |
| `agents/skills/solercia-connect/SKILL.md` | tabla «What each route reads» (líneas 54-59) | fila `\| `mail.inbox.list` \| yes \| yes \|` |
| `.claude/skills/solercia-connect` y `.agents/skills/solercia-connect` | — | son **symlinks** a `agents/skills/solercia-connect`; no se editan (verificado por `test_canonical_agent_symlinks_are_valid`) |

Ejemplo canónico (cuenta SDMujer, sólo ruta navegador):

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability mail.inbox.list \
  --account cccalderon@sdmujer.gov.co \
  --start 2026-06-01T00:00:00-05:00 \
  --end 2026-07-01T00:00:00-05:00 \
  --limit 1000 \
  --allow-browser-fallback \
  --output bandeja-junio.json
```

Guía obligatoria junto al ejemplo: **si el número de registros devueltos es igual a `--limit`, la
lectura pudo quedar truncada: estrecha la ventana (por quincena o por semana) y vuelve a leer.** El
orden ascendente garantiza que lo devuelto es el prefijo más antiguo de la ventana, así que la
siguiente lectura arranca en el instante del último registro. «Vacío permanece vacío»: cero
registros se reporta como cero y jamás se amplía la ventana.

## Rollback

1. Revertir las siete declaraciones devuelve el runtime al estado actual: la capacidad deja de ser
   enrutable y cada capa vuelve a fallar cerrado con su razón tipada.
2. Los tres campos aditivos son inertes; revertirlos sólo elimina claves del JSON. Sin migración:
   este cambio no persiste nada más allá de los perfiles de conexión, que se regeneran con `connect`.
3. El tope de `limit` se revierte en una línea de validación más el recorte de `application.py`; las
   lecturas ya emitidas siguen siendo válidas.
4. Autenticación, enrutamiento, allowlist de hosts y `ReasonCode` no se tocan.
5. Con entrega encadenada, `git revert` en orden inverso: PR3 y PR2 son reversibles de forma
   independiente de PR1.

## Riesgos de diseño

| # | Riesgo | Mitigación en el diseño |
| --- | --- | --- |
| 1 | `InternetMessageId` en Outlook REST v2 no está probado en vivo (ADR §3.5: las pruebas offline no demuestran contratos vivos) | El campo se lee con `field()` y queda `None` si falta; **ningún** registro falla por su ausencia. Validación diferida a `make test-live TYPE=outlook` |
| 2 | `cccalderon@sdmujer.gov.co` sólo tiene ruta navegador: la rama Graph no se valida en vivo con esa cuenta | Cobertura hermética completa de Graph; la validación viva de Graph queda diferida o se hace con cuenta dedicada (ya documentado en `docs/runbooks/outlook.md:62-64`) |
| 3 | Paridad falsa si los campos nuevos entran al código pero no a **ambos** fixtures gemelos | El paso 1 del orden TDD es el archivo de paridad y sus aserciones usan valores concretos, nunca `None` |
| 4 | Diferencia de orden documentada: bandeja `asc` en ambas rutas frente a enviados `desc` (Graph) / `asc` (navegador) | Se declara explícitamente en el runbook; la asimetría preexistente de enviados **no** se toca en este cambio |
| 5 | El tope de 1000 hace más permisivo también `mail.sent.list` | Aceptado como decisión de producto en la propuesta; el techo estructural del navegador sigue siendo 1000 y no se toca |

## Carga de revisión

Presupuesto de sesión 2000 líneas; por PR 400. Estimación de líneas escritas (adiciones +
eliminaciones), a confirmar por el pronóstico formal de `sdd-tasks`:

| Slice | Contenido | Estimación | Entregable autónomo |
| --- | --- | --- | --- |
| **PR1** | `domain.py`, `_microsoft.py`, `ports.py`, `microsoft_graph.py`, `google.py`, `application.py`, `tests/fakes.py` + pruebas 1, 1b, 2–2e, 5b | ≈ 260-300 | La bandeja se lee por ruta oficial, con paridad de registro y tope de 1000 |
| **PR2** | `selectors.py`, `driver.py`, `adapter.py`, `runtime.py`, `config.py`, `cli.py` + pruebas 3–3d, 4–4c, 5, 5a, 5c, 5d | ≈ 300-350 | Paridad de rutas completa y fallback allowlisteado |
| **PR3** | `matrix.py` + los cinco documentos + prueba 6 | ≈ 100-120 | Capacidad anunciada y documentada |

- `Decision needed before apply: No` (con `delivery_strategy: auto-chain` el corte ya está resuelto).
- `Chained PRs recommended: Yes`
- `400-line budget risk: Medium` — PR2 es el que más se acerca al tope; si el pronóstico de
  `sdd-tasks` lo sitúa por encima de 400, se subdivide en **PR2a** (driver + selectors + adapter con
  sus pruebas de extracción) y **PR2b** (`runtime.py`, `config.py`, `cli.py` con política, ciclo de
  vida y extremo a extremo).

Cadena de ramas: PR1 → `feat/46-mail-inbox-list`; PR2 → PR1; PR3 → PR2. Si GitHub muestra los slices
previos en el diff de un hijo, hay que rebasar hasta que el diff quede limpio.

## Preguntas abiertas

- [ ] Ninguna que bloquee la implementación. La única incógnita pendiente es empírica y no de
      diseño: si el recurso `Message` de Outlook REST v2 devuelve `InternetMessageId` en la práctica.
      El diseño ya la absorbe (campo opcional, sin fallo) y la resuelve con validación `live`.
