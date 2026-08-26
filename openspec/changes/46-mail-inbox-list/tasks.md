# Tasks: 46 · `mail.inbox.list` para Outlook (rutas oficial y navegador)

25 requisitos ADDED / 31 escenarios (specs #3701) sobre 7 puntos de declaración + 4 sitios de
despacho (design #3702). Matriz de amenazas: todas las filas `N/A` (sin clasificación/ejecución de
archivos, sin automatización de Git) — los cinco límites reales quedan cubiertos por las pruebas de
las Fases 1-2 (identidad de cuenta, `nextLink`, proyección incompleta, secretos, construcción de
endpoint). Fuera de alcance: bandeja de Gmail, carpetas Archive/Drafts, consumo del lado de Command
(cambio 47), subir `_OUTLOOK_PAGE_SIZE`/`_OUTLOOK_MAX_PAGES`, cambiar el orden de `mail.sent.list`.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ≈ 900-990 (`additions + deletions`), repartidas en 3 rebanadas |
| 400-line budget risk | Medium (PR3 es la más cercana al tope, con contingencia de partición) |
| Chained PRs recommended | Yes |
| Suggested split | PR1 (Graph) → PR2 (navegador) → PR3 (enrutamiento + documentación) |
| Delivery strategy | auto-chain |
| Chain strategy | feature-branch-chain |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: Medium

| Rebanada | Contenido | ≈ líneas |
|---|---|---|
| PR1 | `domain.py`, `ports.py`, `providers/_microsoft.py`, `providers/microsoft_graph.py`, `providers/google.py`, `tests/fakes.py` + `tests/test_route_contract_parity.py` + `tests/test_provider_contracts.py` | 280-320 |
| PR2 | `providers/m365_browser/{selectors,driver,adapter}.py` + `tests/test_browser_mail_extraction.py` + `tests/test_browser_foundation.py` + `tests/test_outlook_browser_identity.py` + `tests/test_connections.py` | 260-300 |
| PR3 | `runtime.py`, `config.py`, `application.py`, `cli.py`, `matrix.py`, docs (5) + `tests/test_application.py` + `tests/test_cli_lifecycle.py` + `tests/test_runtime_policy.py` + `tests/test_service.py` + `tests/test_cli_and_docs.py` | 330-420 |

PR3 es la rebanada de mayor riesgo: si el diff real durante `apply` supera 400 líneas, dividir en
**PR3a** (`runtime.py` + `config.py` + `application.py` + `cli.py` + `test_application.py` +
`test_cli_lifecycle.py` + `test_runtime_policy.py` + `test_service.py` — enrutamiento y política) y
**PR3b** (`matrix.py` + documentación + `test_cli_and_docs.py`), ambas encadenadas sobre PR2.
Ninguna capa queda anunciada a medias entre rebanadas: mientras `TARGET_SPECS[OUTLOOK]` (PR3) no
incluya `"mail.inbox.list"`, la validación existente de `application.py:53`
(`capability.value not in spec.capabilities`) sigue rechazando la capacidad con `invalid_input`
aunque el código de PR1/PR2 ya exista — cadena segura por diseño (declaración en siete puntos).

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Bandeja por Graph con paridad de contrato y tope 1000 en `MailQuery` | PR 1 (base: tracker `feat/46-mail-inbox-list`) | `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_route_contract_parity.py tests/test_provider_contracts.py -m 'not live' -v` | N/A — sin cuenta Graph dedicada en esta sesión (validación viva diferida, riesgo de diseño 2) | Revertir `domain.py`/`ports.py`/`providers/_microsoft.py`/`providers/microsoft_graph.py`/`providers/google.py`/`tests/fakes.py` + las dos pruebas; ruta navegador y enrutamiento intactos |
| 2 | Bandeja por navegador con la misma proyección y paridad de registro | PR 2 (base: rama de PR 1) | `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_browser_mail_extraction.py tests/test_browser_foundation.py tests/test_outlook_browser_identity.py tests/test_connections.py -m 'not live' -v` | N/A — el fallback requiere `--allow-browser-fallback` y una sesión supervisada interactiva; capacidad aún no anunciada en `TARGET_SPECS` (PR3), por lo que no es alcanzable end-to-end todavía | Revertir `providers/m365_browser/{selectors,driver,adapter}.py` + las cuatro pruebas; ruta Graph de PR1 sigue intacta |
| 3 | Enrutamiento, política de fallback, límites y documentación conectan la capacidad de punta a punta | PR 3 (base: rama de PR 2) | `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_application.py tests/test_cli_lifecycle.py tests/test_runtime_policy.py tests/test_service.py tests/test_cli_and_docs.py -m 'not live' -v` | [MANUAL, diferido] `solercia-connect run --provider microsoft.graph --capability mail.inbox.list --account cccalderon@sdmujer.gov.co --start 2026-06-01T00:00:00-05:00 --end 2026-07-01T00:00:00-05:00 --limit 1000 --allow-browser-fallback --output bandeja-junio.json` — requiere sesión de navegador supervisada real, fuera del alcance automatizado de `apply` | Revertir `runtime.py`/`config.py`/`application.py`/`cli.py`/`matrix.py`/docs (5) + sus pruebas; la capacidad vuelve a `invalid_input` en Gmail y a no anunciarse en Outlook; PR1/PR2 quedan dormidos pero intactos |

## Fase 0: Verificación previa (gate)

- [ ] 0.1 Confirmar en disco: `domain.py` — `class Capability(StrEnum)` (:11) con `MAIL_SENT_LIST = "mail.sent.list"` (:12); `_CAPABILITY_RECORD_TYPES` (:447) con `Capability.MAIL_SENT_LIST: MailMessage` (:448); `MailQuery` (:102) con `limit: int = 50` (:105) y rango `1 <= self.limit <= 100` (:110-111); `MailMessage` (:258) termina en `attachments: tuple[MailAttachment, ...] = ()` (:270), sin campos posteriores.
- [ ] 0.2 Confirmar en disco: `ports.py` — `MailProviderPort(Protocol)` (:64) con único método `list_sent_mail` (:65); ningún puerto de bandeja existe todavía.
- [ ] 0.3 Confirmar en disco: `config.py` — `TARGET_SPECS` (:80), entrada Outlook (:81-88) con `("mail.sent.list", "calendar.events.list")` (:84).
- [ ] 0.4 Confirmar en disco: `runtime.py` — `class RoutePolicy` (:33), `browser_fallback_capabilities` (:54-63) con seis capacidades, sin `MAIL_INBOX_LIST`.
- [ ] 0.5 Confirmar en disco: `providers/microsoft_graph.py` — `MicrosoftGraphAdapter.descriptor` (:57-71) sin `MAIL_INBOX_LIST`; `execute()` (:103-133) sin rama de bandeja; `list_sent_mail` (:135).
- [ ] 0.6 Confirmar en disco: `providers/m365_browser/adapter.py` — `_BROWSER_EXTRACTION_CAPABILITIES[ConnectionType.OUTLOOK]` (:40) con dos capacidades; `Microsoft365BrowserAdapter.descriptor` (:78-90); `_bind_provenance` (:372-388) sobrescribe `identity_evidence` con `"outlook-rest-v2-me"`.
- [ ] 0.7 Confirmar en disco: `providers/m365_browser/driver.py` — `_OUTLOOK_PAGE_SIZE = 50` (:63), `_OUTLOOK_MAX_PAGES = 20` (:64); `_SENT_MAIL_SELECT` (:73-75, 9 campos); `read_records` (:401) con una sola rama de correo (:410-411); `_read_sent_mail` (:436); `_normalize_outlook_message` (:1344).
- [ ] 0.8 Confirmar en disco: `providers/m365_browser/selectors.py` — `Microsoft365Endpoints` (:11) con `outlook_sent_messages` (:17-19); sin `outlook_inbox_messages`.
- [ ] 0.9 Confirmar en disco: `providers/_microsoft.py` — `normalize_mail_message` (:86-123) sin parámetro `folder` ni lectura de `internetMessageId`/`receivedDateTime`; `providers/google.py` — `_normalize_message` construye `MailMessage(...)` posicional (:187-198) sin `folder`.
- [ ] 0.10 Confirmar en disco: `application.py` — `_query` (:260), rama `MAIL_SENT_LIST` (:282) con `min(limit, 100)` (:296); `cli.py` — `_query_payload` (:638), rama `MAIL_SENT_LIST` (:661) con `min(limit, 100)` (:662), cola final retorna `TicketQuery` sin fallo tipado (:703).
- [ ] 0.11 Confirmar en disco: `matrix.py` — `CAPABILITY_MATRIX` (:15) con tres filas de `mail.sent.list` (:19, :54, :89), sin filas de `mail.inbox.list`.
- [ ] 0.12 Confirmar en disco las pruebas con lista exacta a actualizar: `tests/test_application.py:207` (`mail.limit == 100` con payload `limit=500`); `tests/test_cli_lifecycle.py:362-364` (bucle sin mapa capacidad→tipo); `tests/test_outlook_browser_identity.py:102` y `tests/test_connections.py:593` (lista de dos capacidades); `tests/test_route_contract_parity.py` — `_GRAPH_MESSAGE` (:68), `_BROWSER_MESSAGE` (:80), prueba de paridad (:126); `tests/fakes.py` — `mail_record()` (:226-244) sin los tres campos nuevos.
- [ ] 0.13 Confirmar línea base: `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'` reporta 402 pruebas en verde; `make check` en verde; `pyproject.toml:49,53` — `branch = true`, `fail_under = 80`.

## Fase 1: Bandeja por Graph con paridad de contrato [PR1]

> Spec: `mail-inbox-list` (ventana de recepción, proyección/orden, validación por registro); `mail-record-contract` (todos los requisitos); `mail-query-limits` (`MailQuery.limit` 1..1000). Design: D1 (`ports.py`), D2/A3 (helper único parametrizado), D3/B2 (campos aditivos), D5 (`internetMessageId`), D6 parcial (rango de `limit`, `$orderby` ascendente en Graph).

- [ ] 1.1 RED — `tests/test_route_contract_parity.py`: modificar `_GRAPH_MESSAGE` (+`internetMessageId`, `receivedDateTime`) y `_BROWSER_MESSAGE` (+`InternetMessageId`, `ReceivedDateTime`) con valores concretos; modificar `test_a_mail_record_is_identical_whichever_route_produced_it` para pasar `folder="inbox"` y asertar `internet_message_id`/`received_at` iguales y no `None` en ambos registros.
- [ ] 1.2 RED — mismo archivo: agregar `test_a_sent_mail_record_declares_its_folder_and_no_received_instant` (`folder == "sent"`, `received_at is None`). Spec: mail-record-contract, escenario «Registro de enviados sin instante de recepción».
- [ ] 1.3 RED — `tests/test_provider_contracts.py`: agregar `test_microsoft_graph_inbox_reads_the_declared_inbox_folder_collection` (URL `.../me/mailFolders/inbox/messages`, `$filter` fin exclusivo sobre `receivedDateTime`, `$orderby == "receivedDateTime asc"`, `$top == 100`, `$select` con 12 nombres exactos).
- [ ] 1.4 RED — mismo archivo: `test_microsoft_graph_inbox_record_carries_folder_message_id_and_received_instant` (`folder=="inbox"`, `received_at != sent_at`, `internet_message_id`, `provenance.endpoint`).
- [ ] 1.5 RED — mismo archivo: `test_microsoft_graph_inbox_refuses_a_mailbox_that_is_not_the_authenticated_account` (`AUTHORIZATION_BOUNDARY_VIOLATION`, `len(requests) == 1`).
- [ ] 1.6 RED (inclusión obligatoria #1 del validador de diseño, ALTO) — mismo archivo: `test_microsoft_graph_inbox_without_sent_instant_fails_closed` — `$select` de bandeja sin `sentDateTime`, la normalización falla con `MALFORMED_PROVIDER_DATA`.
- [ ] 1.7 RED — mismo archivo: modificar `test_microsoft_graph_normalizes_full_sent_message`: `$select` incluye `internetMessageId` y no `receivedDateTime`; `$orderby` sigue `"sentDateTime desc"`; `folder=="sent"`, `received_at is None`.
- [ ] 1.8 GREEN — `domain.py`: agregar `Capability.MAIL_INBOX_LIST = "mail.inbox.list"` tras :12; `_CAPABILITY_RECORD_TYPES[Capability.MAIL_INBOX_LIST] = MailMessage` tras :448; cambiar el rango de `MailQuery.__post_init__` (:110-111) a `1 <= self.limit <= 1000` con mensaje «...between 1 and 1000»; agregar a `MailMessage` (tras :270) `internet_message_id: str | None = None`, `folder: str | None = None`, `received_at: datetime | None = None`.
- [ ] 1.9 GREEN — `ports.py`: agregar `InboxMailProviderPort(Protocol)` con `list_inbox_mail(self, query: MailQuery) -> tuple[MailMessage, ...]: ...` tras `MailProviderPort` (:64-65); `MailProviderPort` no cambia.
- [ ] 1.10 GREEN — `providers/_microsoft.py`: agregar `MailFolderName = Literal["inbox", "sent"]`; `normalize_mail_message` (:86) gana parámetro de palabra clave `folder: MailFolderName`; leer `internet_message_id` con `field(item, "internetMessageId", "InternetMessageId")` (strip, `None` si vacío) y `received_at` con `optional_microsoft_datetime(field(item, "receivedDateTime", "ReceivedDateTime"))`; pasar `folder=folder` al construir `MailMessage`.
- [ ] 1.11 GREEN — `providers/microsoft_graph.py`: agregar `_GRAPH_MAIL_SELECT_BASE`, `_GRAPH_SENT_SELECT` (11 campos), `_GRAPH_INBOX_SELECT` (12 campos), `_GraphMailFolder = Literal["inbox", "sentitems"]`, `_GraphMailTimeField = Literal["receivedDateTime", "sentDateTime"]`, `_GRAPH_MAIL_FOLDERS` (tabla única carpeta→`$select`→`folder`).
- [ ] 1.12 GREEN — mismo archivo: agregar `Capability.MAIL_INBOX_LIST` al `frozenset` del descriptor (:62-69); rama en `execute()` (:106-109) para `MAIL_INBOX_LIST`+`MailQuery` → `self.list_inbox_mail(request.payload)`; refactorizar `list_sent_mail` (:135) en `_list_folder_mail(self, query, *, folder, time_field, order)` que atesta identidad primero (`self._identity()`), resuelve `_GRAPH_MAIL_FOLDERS[folder]` y llama `normalize_mail_message(..., folder=record_folder)`; `list_sent_mail`/`list_inbox_mail` quedan como envoltorios delgados.
- [ ] 1.13 GREEN — `providers/google.py`: en `_normalize_message` (:187), agregar `folder="sent"` como argumento de palabra clave al construir `MailMessage`.
- [ ] 1.14 GREEN — `tests/fakes.py`: extender `mail_record()` (:226) con parámetros opcionales `folder: str | None = None`, `internet_message_id: str | None = None`, `received_at: datetime | None = None`, propagados a `MailMessage`.
- [ ] 1.15 Verificar en verde: `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_route_contract_parity.py tests/test_provider_contracts.py -m 'not live' -v`.
- [ ] 1.16 `make check` en verde sobre los archivos tocados en la Fase 1.

## Fase 2: Bandeja por navegador con la misma paridad [PR2]

> Spec: `mail-inbox-list` (paginación acotada, proyección/orden, vacío permanece vacío); `mail-query-limits` (truncación determinista); `capability-advertisement` (descriptor de navegador, `_BROWSER_EXTRACTION_CAPABILITIES`). Design: D2/A3 aplicado al navegador, D4 (`folder` explícito, nunca derivado de `provenance.endpoint`).

- [ ] 2.1 RED — `tests/test_browser_mail_extraction.py`: agregar helpers `_inbox_message()` y `_read_inbox()` (mismo patrón que `_sent_message()`); agregar `test_inbox_mail_is_normalized_from_the_documented_rest_collection` (URL exacta de `outlook_inbox_messages`, `authorized is True`, `folder=="inbox"`, `received_at` no `None`).
- [ ] 2.2 RED — mismo archivo: `test_inbox_mail_query_is_end_exclusive_and_asks_only_for_needed_fields` (`$filter` sobre `ReceivedDateTime`, `$orderby == "ReceivedDateTime asc"`, `$select` con 11 nombres exactos, incluye `InternetMessageId`/`ReceivedDateTime`).
- [ ] 2.3 RED — mismo archivo: `test_inbox_mail_follows_the_provider_next_link` (paginación vía `@odata.nextLink`, verificada con `_is_allowed_outlook_request_url`).
- [ ] 2.4 RED — mismo archivo: `test_inbox_mail_stops_at_the_requested_limit` (trunca de forma silenciosa al alcanzar `limit`; el orden ascendente entrega el prefijo temporal más antiguo).
- [ ] 2.5 RED (inclusión obligatoria #4 del validador, BAJO) — mismo archivo: `test_inbox_mail_reports_zero_records_without_widening_the_window_when_the_window_is_empty` (ventana sin mensajes → cero registros, estado exitoso, sin ampliar la ventana).
- [ ] 2.6 RED — mismo archivo: modificar el `$select` exacto de enviados (líneas 221-231) para incluir `InternetMessageId` (10 nombres).
- [ ] 2.7 RED (inclusión obligatoria #4 del validador, aplicada a enviados por navegador) — mismo archivo: agregar `test_sent_mail_record_has_no_received_instant_over_the_browser_route` (`received_at is None`, `folder=="sent"`, valores concretos, sin comparar contra `None` de forma implícita).
- [ ] 2.8 RED — `tests/test_browser_foundation.py`: agregar `test_browser_extraction_report_declares_inbox_for_outlook` (`capabilities == ["mail.sent.list", "mail.inbox.list", "calendar.events.list"]`; `browser_surface_for_capability(Capability.MAIL_INBOX_LIST) is ConnectionType.OUTLOOK`).
- [ ] 2.9 RED — modificar `tests/test_outlook_browser_identity.py:102` y `tests/test_connections.py:593`: reemplazar `["mail.sent.list", "calendar.events.list"]` por `["mail.sent.list", "mail.inbox.list", "calendar.events.list"]` (orden declarado en `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]`).
- [ ] 2.10 GREEN — `providers/m365_browser/selectors.py`: agregar `outlook_inbox_messages: str = "https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages"` al `dataclass` `Microsoft365Endpoints` (tras :19).
- [ ] 2.11 GREEN — `providers/m365_browser/driver.py`: agregar `_OUTLOOK_MAIL_SELECT_BASE`, `_SENT_MAIL_SELECT` (10 campos, +`InternetMessageId`), `_INBOX_MAIL_SELECT` (11 campos), `_OutlookTimeField = Literal["SentDateTime", "ReceivedDateTime"]`, `_OUTLOOK_MAIL_READS: dict[_OutlookTimeField, tuple[str, MailFolderName]]` (tabla única).
- [ ] 2.12 GREEN — mismo archivo: refactorizar `_read_sent_mail` (:436) en `_read_folder_mail(self, query, *, url, time_field, timeout_ms)` parametrizado por `_OUTLOOK_MAIL_READS[time_field]`; agregar rama en `read_records` (:401-411) para `Capability.MAIL_INBOX_LIST` → `self._read_folder_mail(payload, url=ENDPOINTS.outlook_inbox_messages, time_field="ReceivedDateTime", timeout_ms=timeout_ms)`; `_read_sent_mail` queda como envoltorio delgado.
- [ ] 2.13 GREEN — mismo archivo: `_normalize_outlook_message` (:1344) gana parámetro `folder: MailFolderName`; lee `InternetMessageId`/`ReceivedDateTime` y los pasa a `MailMessage(..., folder=folder)`.
- [ ] 2.14 GREEN — `providers/m365_browser/adapter.py`: agregar `Capability.MAIL_INBOX_LIST` al `frozenset` del `descriptor` de `Microsoft365BrowserAdapter` (:83-90) y a `_BROWSER_EXTRACTION_CAPABILITIES[ConnectionType.OUTLOOK]` (:40), en el orden `(MAIL_SENT_LIST, MAIL_INBOX_LIST, CALENDAR_EVENTS_LIST)`.
- [ ] 2.15 Verificar en verde: `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_browser_mail_extraction.py tests/test_browser_foundation.py tests/test_outlook_browser_identity.py tests/test_connections.py -m 'not live' -v`.
- [ ] 2.16 `make check` en verde sobre los archivos de la Fase 2.

## Fase 3: Enrutamiento, política, límites y documentación [PR3]

> Spec: `mail-inbox-list` (enrutamiento oficial-primero, fallback opt-in, Gmail fuera de alcance, vacío permanece vacío); `mail-query-limits` (recorte en `application.py`); `capability-advertisement` (todos los requisitos restantes). Design: D6 completo (recorte + wiring), decisión de módulo «`cli._query_payload` falla cerrado en vez de devolver `TicketQuery`».

- [ ] 3.1 RED (inclusión obligatoria #1 del validador, ALTO) — `tests/test_application.py`: agregar `test_inbox_mail_request_for_gmail_is_rejected_before_any_capability_dispatch` — solicitar `mail.inbox.list` sobre `gmail` responde `invalid_input` porque `"mail.inbox.list" not in spec.capabilities` de Gmail (`application.py:53`).
- [ ] 3.2 RED (inclusión obligatoria #2 del validador, ALTO) — mismo archivo: cambiar la aserción de la línea 207 de `mail.limit == 100` a `mail.limit == 500` (el payload ya usa `limit=500`); agregar aserción compañera de que `MailQuery(window, limit=1001)` lanza `ValueError`.
- [ ] 3.3 RED — mismo archivo: agregar `test_mail_query_accepts_the_thousand_record_limit_and_rejects_more` (`limit=1000` construye; `limit=1001` → `ValueError("MailQuery limit must be between 1 and 1000")`; `limit=0` rechazado).
- [ ] 3.4 RED — mismo archivo: agregar `test_inbox_mail_runs_end_to_end_over_the_official_route` (`ok`, `receipt.capability == "mail.inbox.list"`, `folder=="inbox"` en cada registro, recorte a 1000 cuando el payload pide más).
- [ ] 3.5 RED (inclusión obligatoria #4 del validador, BAJO) — mismo archivo: agregar `test_inbox_mail_over_an_empty_window_reports_zero_records_without_widening_it` (variante de aplicación de «vacío permanece vacío», complementa 2.5).
- [ ] 3.6 RED — `tests/test_service.py`: agregar `test_service_runs_the_inbox_capability_and_rejects_the_browser_route` (el servicio HTTP responde `action_required` con el comando de CLI exacto ante una ruta de navegador para `mail.inbox.list`).
- [ ] 3.7 RED — `tests/test_runtime_policy.py`: agregar `test_inbox_mail_is_allowlisted_for_the_microsoft_browser_fallback` (`allows_browser` `True` con `OFFICIAL_CAPABILITY_UNAVAILABLE` + `MICROSOFT`, `False` con `GOOGLE`; capacidad presente en `browser_fallback_capabilities`).
- [ ] 3.8 RED (hallazgo del validador de diseño) — `tests/test_cli_lifecycle.py`: reemplazar el bucle de las líneas 362-364 por un mapa capacidad→tipo esperado y asertar `isinstance(query, expected_type)` por capacidad, no solo `is not None`.
- [ ] 3.9 RED — mismo archivo: agregar `test_legacy_query_helper_fails_closed_on_an_unknown_capability` (`pytest.raises(cli._InputError)` con `cast(Capability, "mail.archive.list")`); agregar aserción `_target_for_provider("microsoft.graph", Capability.MAIL_INBOX_LIST) is ConnectionType.OUTLOOK`.
- [ ] 3.10 RED — `tests/test_cli_and_docs.py`: agregar `test_docs_declare_the_inbox_capability_for_both_routes` (`"mail.inbox.list"` en `README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md` y el `SKILL.md` del agente; dos filas nuevas en `CAPABILITY_MATRIX`, una por canal).
- [ ] 3.11 GREEN — `runtime.py`: agregar `Capability.MAIL_INBOX_LIST` a `RoutePolicy.browser_fallback_capabilities` (:54-63).
- [ ] 3.12 GREEN — `config.py`: agregar `"mail.inbox.list"` a la tupla `capabilities` de la entrada Outlook en `TARGET_SPECS` (:84).
- [ ] 3.13 GREEN — `application.py`: generalizar la rama `if capability is Capability.MAIL_SENT_LIST:` (:282) para cubrir también `Capability.MAIL_INBOX_LIST`; cambiar `min(limit, 100)` (:296) a `min(limit, 1000)`.
- [ ] 3.14 GREEN — `cli.py`: generalizar la rama `if capability is Capability.MAIL_SENT_LIST:` (:661) para cubrir `Capability.MAIL_INBOX_LIST`; cambiar `min(limit, 100)` (:662) a `min(limit, 1000)`; sustituir la cola final (:703) por `raise _InputError(f"Unsupported capability: {capability.value}")` en vez de devolver `TicketQuery` por defecto.
- [ ] 3.15 GREEN — `matrix.py`: agregar dos filas nuevas a `CAPABILITY_MATRIX` (:15) para `mail.inbox.list`: canal `official` y canal `browser`.
- [ ] 3.16 GREEN — Docs: `README.md` — fila `mail.inbox.list` en «What each route can read» (90-99) y actualizar el ejemplo `connected_browser` (41-44) con `["mail.sent.list", "mail.inbox.list"]`; `docs/runbooks/outlook.md` — actualizar «Explicit browser fallback» (39-43) a tres capacidades y agregar la subsección «Leer la bandeja de entrada» con el ejemplo SDMujer y la guía de ventana; `docs/USO-CON-AGENTES.md` — agregar `mail.inbox.list` en «4. Leer» (59-73); `agents/skills/solercia-connect/SKILL.md` — fila `mail.inbox.list` en «What each route reads» (52-58); los symlinks en `.claude/skills/` y `.agents/skills/` no se editan.
- [ ] 3.17 Verificar en verde: `uv run --locked --extra dev --extra oauth --extra service pytest tests/test_application.py tests/test_cli_lifecycle.py tests/test_runtime_policy.py tests/test_service.py tests/test_cli_and_docs.py -m 'not live' -v`.
- [ ] 3.18 `make check` en verde sobre los archivos de la Fase 3.

## Fase 4: Cierre y validación manual diferida

- [ ] 4.1 Ejecutar la suite completa: `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`; confirmar la línea base de 402 pruebas más las nuevas de las Fases 1-3, todas en verde.
- [ ] 4.2 Ejecutar `make check` completo (`ruff format --check .`, `ruff check .`, `mypy src`) sobre todo el árbol.
- [ ] 4.3 Ejecutar cobertura: `uv run --locked --extra dev --extra oauth --extra service pytest --cov=solercia_connect --cov-branch --cov-report=term-missing -m 'not live'`; confirmar `fail_under = 80` (`pyproject.toml:53`) en verde para las ramas nuevas (`_list_folder_mail`, `_read_folder_mail`, rama Gmail fail-closed).
- [ ] 4.4 Confirmar en `tests/test_cli_and_docs.py` (Fase 3.10) que el ejemplo canónico de `README.md`/runbook incluye la advertencia: «si el número de registros devueltos es igual a `--limit`, la lectura pudo quedar truncada: estrecha la ventana y vuelve a leer».
- [ ] 4.5 [MANUAL, diferido] Validación viva de Graph: `make test-live TYPE=outlook ACCOUNT=<cuenta con SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID>` — sin cuenta dedicada disponible en esta sesión (`cccalderon@sdmujer.gov.co` es solo navegador); queda fuera de `apply`/`verify` (riesgo de diseño 2).
- [ ] 4.6 [MANUAL, diferido] Validación operativa por navegador con la cuenta real (ver comando en «Suggested Work Units», unidad 3); requiere sesión de navegador supervisada interactiva, fuera del alcance automatizado de `apply`.
- [ ] 4.7 Commits convencionales en español por unidad de trabajo: `feat(mail): agrega mail.inbox.list por Graph con paridad de contrato`, `feat(mail): agrega mail.inbox.list por navegador supervisado`, `feat(mail): conecta enrutamiento, política y documentación de mail.inbox.list`.
- [ ] 4.8 [orquestador] Ramas hijas sobre el tracker `feat/46-mail-inbox-list` (feature-branch-chain): `…--01-graph-route` (base: tracker) → `…--02-browser-route` (base: 01) → `…--03-wiring-docs` (base: 02). Solo el tracker se integra a la rama de destino.
