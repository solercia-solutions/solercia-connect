# Exploración: `mail.inbox.list` para Outlook (rutas oficial y navegador)

> Fase `sdd-explore` del cambio `46-mail-inbox-list`. Store híbrido: este contenido está también en Engram (`sdd/46-mail-inbox-list/explore`, observación #3695).

## Resumen ejecutivo

Agregar `mail.inbox.list` es un cambio **aditivo y simétrico** que no requiere subir la versión de
`solercia.connect/v1` ni de `solercia.connect/record/v1`, porque la serialización recorre los campos
del dataclass (`serialization.py:19-20`) y los consumidores existentes leen claves que no cambian.
El trabajo real no está en las dos llamadas HTTP, sino en las **siete listas de capacidades** que
deben declararlo de forma explícita para que el diseño fail-closed siga funcionando, y en un
**techo de 100 mensajes por invocación** (`domain.py:109-113`) que hoy hace inviable el caso de uso
real (bandeja institucional de 2026-06-01 a 2026-08-27). La recomendación es una sola tanda:
capacidad nueva + campos aditivos (`internet_message_id`, `folder`, `received_at`) + subir el tope
de `MailQuery.limit` a 1000, con Gmail explícitamente fuera de alcance.

---

## Estado actual con evidencia

### 1. Dónde vive una capacidad, de punta a punta

Una capacidad no existe en un solo lugar. Debe declararse en **siete** puntos independientes; omitir
cualquiera produce un fallo tipado distinto (que es exactamente el diseño buscado):

| # | Lugar | Archivo:línea | Qué pasa si falta |
| --- | --- | --- | --- |
| 1 | Enum `Capability` | `src/solercia_connect/domain.py:11-19` | `ValueError` en `Capability(...)` → `invalid_input` (`cli.py:242-247`) |
| 2 | `_CAPABILITY_RECORD_TYPES` | `src/solercia_connect/domain.py:447-456` | `ValueError: Capability ... declares no record type` (`domain.py:465-467`) al validar registros |
| 3 | `TARGET_SPECS` (Outlook) | `src/solercia_connect/config.py:80-88` | `application.py:53-56` rechaza con `invalid_input` |
| 4 | Descriptor Graph | `src/solercia_connect/providers/microsoft_graph.py:57-71` | `runtime.py:122-135` emite `official_capability_unavailable` |
| 5 | Descriptor navegador | `src/solercia_connect/providers/m365_browser/adapter.py:79-93` | `runtime.py:212-226` emite `browser_capability_not_implemented` |
| 6 | `RoutePolicy.browser_fallback_capabilities` | `src/solercia_connect/runtime.py:54-63` | `runtime.py:194-210` emite `browser_fallback_denied` |
| 7 | `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]` | `src/solercia_connect/providers/m365_browser/adapter.py:39-52` | `bootstrap.py:174-176` no resuelve la superficie de sesión; el sobre de `connect`/`status` no la anuncia (`adapter.py:68-75`) |

Además, el **despacho** vive en dos sitios más: `microsoft_graph.py:103-133` (`execute`) y
`m365_browser/driver.py:401-434` (`read_records`). Sin rama, devuelven
`capability_not_implemented` / `browser_capability_not_implemented`.

### 2. Cómo se anuncia hoy la capacidad

- `doctor` la lee de `spec.capabilities` (`doctor.py:49,357`) — **una sola línea en `TARGET_SPECS`
  la propaga**.
- `providers` la lee de `TARGET_SPECS` y de `CAPABILITY_MATRIX` (`cli.py:104-110`).
- `cli._target_for_provider` resuelve el target desde `TARGET_SPECS` por capacidad
  (`cli.py:728-745`), con el comentario explícito: *"a new capability is routable as soon as it is
  declared"*.
- El perfil de conexión persiste `spec.capabilities` (`config.py:275`).
- `service.RunRequest.capability` es `str` libre (`service.py:62`), validado aguas abajo — **no
  requiere cambio de esquema HTTP**.

### 3. Forma exacta del registro y del sobre

```
MailMessage           domain.py:257-270
  id, subject, sent_at: datetime, sender, to, cc, body, body_format,
  conversation_id, has_attachments, provenance, attachments = ()
Provenance            domain.py:226-234
  provider_id, family, route, source_id, fetched_at, account_id=None, endpoint=None
```

- `ENVELOPE_SCHEMA = "solercia.connect/v1"`, `RECORD_SCHEMA = "solercia.connect/record/v1"`
  (`envelope.py:8-9`); `versioned_record` hace `{"schema": ..., **to_primitive(record)}`
  (`envelope.py:89-93`).
- `to_primitive` recorre `fields(dataclass)` (`serialization.py:19-20`): **cualquier campo nuevo
  aparece automáticamente como clave nueva del JSON**.
- Doctrina de versionado: `docs/ARCHITECTURE.md:138-144` y el ADR
  `docs/decisions/2026-08-23-solercia-platform-decisions.md:126-127` sólo exigen que CLI y HTTP
  compartan el sobre y que ningún adaptador exponga cuerpos crudos. **No hay regla que obligue a
  subir la versión por adición.**

**Veredicto sobre aditivo vs. ruptura:** agregar campos **con valor por defecto** al final del
dataclass es aditivo. El mapeador de Command declara depender de `external_id` para deduplicar y de
`Capability.mail → "mail.sent.list"`; ninguna de esas dos cosas cambia. Sería ruptura eliminar,
renombrar o volver opcional `sent_at`.

**Restricción dura de construcción:** `MailMessage` se construye **posicionalmente** en tres
lugares — `providers/_microsoft.py:111-123`, `providers/google.py:187-207` y `tests/fakes.py:232-244`.
Como `attachments` ya tiene default (`domain.py:270`), todo campo nuevo debe ir **después** y con
default, o los tres call sites rompen.

### 4. Constructores de petición: Graph vs. Outlook REST v2

| Aspecto | Graph oficial (`microsoft_graph.py:142-156`) | Navegador (`driver.py:436-479`) |
| --- | --- | --- |
| Endpoint | `{base}/me/mailFolders/sentitems/messages` | `https://outlook.office.com/api/v2.0/me/MailFolders/SentItems/messages` (`selectors.py:17-19`) |
| Campo de filtro | `sentDateTime ge … and sentDateTime lt …` | `SentDateTime ge … and SentDateTime lt …` |
| Formato de instante | `_graph_datetime` → ISO con `Z` (`microsoft_graph.py:517-518`) | `_outlook_timestamp` → `%Y-%m-%dT%H:%M:%SZ` (`driver.py:1339-1341`) |
| `$select` | `id,subject,sentDateTime,from,sender,toRecipients,ccRecipients,body,conversationId,hasAttachments` | `_SENT_MAIL_SELECT` (`driver.py:73-75`), **sin `Sender`** |
| `$orderby` | `sentDateTime desc` | `SentDateTime asc` |
| `$top` | `min(query.limit, 100)` | `str(min(50, query.limit))` (`_OUTLOOK_PAGE_SIZE`, `driver.py:63`) |
| Tope de páginas | 20 → `malformed_provider_data` (`microsoft_graph.py:440-444`) | 20, silencioso (`_OUTLOOK_MAX_PAGES`, `driver.py:64`; bucle en `driver.py:455`) |
| Seguridad de `nextLink` | `require_same_origin_https_url` antes de enviar credenciales (`microsoft_graph.py:445-449`) | `_is_allowed_outlook_request_url` en cada GET (`driver.py:1084`) |
| Cabeceras | `Prefer: outlook.body-content-type="text", outlook.timezone="UTC"` (`microsoft_graph.py:509-514`) | sin `Prefer` para correo (sí para calendario, `driver.py:498`) |

**Asimetría preexistente relevante:** el orden es `desc` en Graph y `asc` en navegador. Bajo lectura
truncada por `limit`, las dos rutas devuelven **subconjuntos distintos** de la misma ventana. Esto ya
ocurre hoy con `mail.sent.list` y no está cubierto por `test_route_contract_parity.py` (que compara
normalización, no paginación).

**Cambio mínimo simétrico para bandeja de entrada:**

- Graph: `GET {base}/me/mailFolders/inbox/messages`, `$filter` sobre `receivedDateTime ge … and
  receivedDateTime lt …`, `$select` con `receivedDateTime` **y `sentDateTime`** (ver §6),
  `$orderby: receivedDateTime asc`, mismo `_paged_values`.
- Navegador: `https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages`, `$filter` sobre
  `ReceivedDateTime`, mismo `$top`/paginación/allowlist.

### 5. Restricciones de la ruta navegador

- **Allowlist de host para el Bearer** (`driver.py:51-55`): `outlook.cloud.microsoft`,
  `outlook.office.com`, `substrate.office.com`. `_is_allowed_request_url` (`driver.py:1317-1332`)
  exige HTTPS, sin userinfo, puerto `None|443`, host exacto o subdominio.
  **La ruta de Inbox vive en `outlook.office.com` → ya está permitida. No hay que tocar la
  allowlist ni debilitarla.**
- **Selección de carpeta:** hoy es una constante en `ENDPOINTS.outlook_sent_messages`
  (`selectors.py:17-19`), no un parámetro. Añadir `outlook_inbox_messages` como constante hermana
  mantiene el mismo patrón (endpoints declarados, nunca construidos con datos del usuario).
- **Página de arranque:** `ENDPOINTS.home_for(OUTLOOK)` devuelve `outlook_sent`
  (`selectors.py:22-24`) y `_await_outlook_bearer` reentra a esa misma URL para que la sesión emita
  una llamada autorizada (`driver.py:1104-1108`). El token capturado sirve para toda la API de
  correo del mismo origen, así que **no es necesario cambiar la home** para leer Inbox.
- **La procedencia del navegador no nombra la carpeta:** `_normalize_outlook_message` fija
  `endpoint="outlook-rest-v2"` (`driver.py:1356`) y el adaptador lo **sobrescribe** con la evidencia
  de identidad `"outlook-rest-v2-me"` (`adapter.py:372-388`). Por lo tanto **`folder` no se puede
  derivar de `provenance.endpoint`**: exige un campo o parámetro explícito.
- **Fail-closed intacto:** el navegador sigue exigiendo (a) `--allow-browser-fallback` por
  invocación (`application.py:58,112,117`), (b) razón tipada allowlisteada (`runtime.py:44-53`),
  (c) capacidad allowlisteada (`runtime.py:54-63`), (d) capacidad en el descriptor
  (`runtime.py:212-226`) y (e) capacidad en la superficie de extracción (`adapter.py:39-52`).
  Añadir la capacidad a esas listas **no relaja** nada: cada capa sigue siendo opt-in explícito.

### 6. Dónde se asume `sent_at` no nulo

- `MailMessage.sent_at: datetime` es **no opcional** (`domain.py:261`).
- `normalize_mail_message` llama `microsoft_datetime(field(item,"sentDateTime","SentDateTime"),
  "sentDateTime")` (`_microsoft.py:114`), que lanza `MALFORMED_PROVIDER_DATA` si falta
  (`_microsoft.py:43-50`).
  **Consecuencia crítica: si el `$select` de Inbox omite `sentDateTime`/`SentDateTime`, cada
  registro de bandeja falla cerrado.** Un mensaje recibido sí lleva `sentDateTime` en ambas
  colecciones, de modo que basta con no quitarlo de la proyección.
- `google.py:118` filtra la ventana con `sent_at` y `google.py:120` ordena por `sent_at`.
- **No** se usa en recibos: `receipt_fingerprint` sólo mezcla request + steps (`domain.py:487-504`).

Es decir: un registro de bandeja **no rompe** ninguna suposición sobre `sent_at`, siempre que la
proyección lo conserve. Lo que sí falta es el instante de recepción, que es el que Command necesita
para "llegó dentro de la ventana".

### 7. Límites de paginación y de volumen

| Límite | Valor | Evidencia |
| --- | --- | --- |
| `MailQuery.limit` | **1..100**, `ValueError` fuera de rango | `domain.py:109-113` |
| Recorte en la aplicación | `min(limit, 100)` | `application.py:296` |
| Opción de CLI | `min=1, max=1000` | `cli.py:229` |
| Campo HTTP | `ge=1, le=1000` | `service.py:79` |
| Techo estructural Graph | 20 páginas × `$top` 100 = 2000 | `microsoft_graph.py:152,440-444` |
| Techo estructural navegador | 20 páginas × `$top` 50 = **1000** | `driver.py:63-64,452,455` |
| Techo **efectivo** hoy | **100 mensajes por invocación en ambas rutas** | ambos bucles cortan en `len(...) >= limit` (`microsoft_graph.py:438,460`; `driver.py:456,466`) |

**Hallazgo con impacto directo en el caso de uso:** una bandeja institucional entre 2026-06-01 y
2026-08-27 casi con certeza supera 100 mensajes, incluso segmentando por mes. Con el tope actual la
lectura queda truncada **en silencio** (no hay error: simplemente se devuelven 100 registros). La
segmentación mensual recomendada **no resuelve** el problema por sí sola.

- ¿Se puede pasar de 1000? En la ruta navegador **no**, sin subir `_OUTLOOK_PAGE_SIZE` o
  `_OUTLOOK_MAX_PAGES`. 1000 es exactamente su techo estructural, lo que lo convierte en el valor
  natural del nuevo tope (y coincide con `CalendarQuery.limit`, `domain.py:128-129`, con la CLI y
  con el servicio).
- ¿Orden ascendente para recibos deterministas? El recibo no depende del orden
  (`domain.py:487-504`), pero **la truncación sí**. Con `asc`, una ventana truncada siempre devuelve
  el prefijo temporal más antiguo, lo que permite reanudar estrechando la ventana. Recomendado
  `asc` para `mail.inbox.list`. Cambiar el orden de `mail.sent.list` sería un cambio de
  comportamiento observable para consumidores existentes: si se hace, debe declararse aparte.

### 8. Gmail

`GoogleGmailAdapter` declara sólo `MAIL_SENT_LIST` (`google.py:46-51`), despacha sólo esa
(`google.py:83-91`) y `list_sent_mail` **codifica la carpeta en la consulta**:
`q="from:me after:… before:…"` con `labelIds="SENT"` (`google.py:103-104`). Una bandeja de entrada
para Gmail exigiría: rama nueva de despacho, consulta distinta (`labelIds=INBOX`, sin `from:me`),
entrada en `TARGET_SPECS` para `GMAIL` (`config.py:114-121`), fila de `CAPABILITY_MATRIX`
(`matrix.py:51-57`), documentación y pruebas propias. **No es una línea.** Recomendación: **fuera de
alcance**, declarado explícitamente. Como los descriptores son por adaptador, ningún cambio de
puerto hace que Gmail anuncie la capacidad por accidente.

### 9. Cómo se prueban hoy ambas rutas (anclas para TDD estricto)

**Ruta oficial** — `tests/test_provider_contracts.py`
- `utc_window()` (líneas 36-40); `FakeTransport` y `json_response` desde `tests/fakes.py:44-75`.
- Patrón exacto en `test_microsoft_graph_normalizes_full_sent_message` (43-89): primera respuesta =
  identidad `/me`, segunda = colección; se asevera sobre
  `transport.requests[1].params["$filter"]`.
- Seguridad de paginación: `test_microsoft_graph_rejects_untrusted_next_link_before_sending_credentials`
  (448-469) y `test_microsoft_graph_follows_same_origin_next_link` (471-490).

**Ruta navegador** — `tests/test_browser_mail_extraction.py`
- Constructor de mensaje `_message(index, **overrides)` (26-39, casing `Id/Subject/SentDateTime/...`).
- Dobles: `_FakeRequest` (42-46), `_FakeResponse` (48-60), `_FakeRequestContext` (63-88),
  `_FakePage` (91-120), `_FakeContext` (123-134), fábrica `_driver()` (137-162), helper `_read()`
  (165-171).
- Aserciones clave a replicar: normalización completa (174-202), **conjunto exacto de `$select` +
  `$filter` + `$orderby`** (205-231), seguimiento de `@odata.nextLink` (234-253), corte por `limit`
  (256-268).

**Paridad de rutas** — `tests/test_route_contract_parity.py`
- Fixtures gemelos `_GRAPH_MESSAGE` (68-78) y `_BROWSER_MESSAGE` (80-90) y
  `test_a_mail_record_is_identical_whichever_route_produced_it` (126-141).
- **Trampa:** si los campos nuevos se añaden sólo al código y no a **ambos** fixtures, la prueba de
  paridad pasa con los dos registros en `None` y no demuestra nada.

**Otros archivos afectados por pruebas existentes**
- `tests/test_cli_lifecycle.py:362-363` itera **todo** el enum `Capability` contra
  `cli._query_payload` y sólo asevera `query is not None`. `_query_payload` (`cli.py:638-703`) es
  una ruta paralela **usada únicamente por pruebas** (la CLI real construye un dict y llama a
  `run_application`, `cli.py:251-274`): con una capacidad nueva devolvería silenciosamente un
  `TicketQuery`. La producción sí falla cerrado (`application.py:367-369` lanza `ValueError`).
- `tests/test_runtime_policy.py` (15 coincidencias) cubre la allowlist de fallback.
- `tests/test_browser_foundation.py` (5) cubre `browser_extraction_report`.
- `tests/test_cli_and_docs.py:123-158` asevera contenido de `README.md`, `docs/ROADMAP.md` y del
  `SKILL.md` de `agents/skills/`.
- `tests/fakes.py:226-244` `mail_record()` construye `MailMessage` posicionalmente.
- `tests/live/test_outlook_live.py` (marcador `live`, opt-in).

### 10. Invariantes de identidad y seguridad que deben permanecer

1. **Identidad canónica antes de cualquier lectura.** Graph: `_identity()` es la primera línea de
   `list_sent_mail` (`microsoft_graph.py:136`) y compara contra `expected_account`
   (`microsoft_graph.py:426-430`) → `authorization_boundary_violation`. Navegador: `_prepare`
   atesta identidad antes de `read_records` (`adapter.py:191-205`) y `_outlook_account` exige una
   identidad canónica única (`adapter.py:302-316`).
2. **Validación por registro en el runtime.** `_validate_records` (`runtime.py:390-416`) verifica
   tipo de registro, `provider_id`, `family`, `route` y frontera de cuenta en cada registro.
3. **Secretos nunca registrados.** `_TransientBearer` (`driver.py:97-124`) es no imprimible y se
   limpia en `finally` (`driver.py:1087-1088`); `SECURITY.md:60-73`.
4. **`--allow-browser-fallback` por invocación**; el servicio HTTP lo rechaza por diseño
   (`service.py:370-374`, `application.py:78-80` → `action_required` con el comando exacto de CLI).
5. **Códigos de razón deterministas.** El cambio **no necesita** ningún `ReasonCode` nuevo
   (`domain.py:34-66`).
6. **"Vacío permanece vacío"**: cero registros se reporta como cero; jamás se amplía la ventana.

---

## Áreas afectadas

| Archivo | Por qué |
| --- | --- |
| `src/solercia_connect/domain.py` | `Capability.MAIL_INBOX_LIST`; `_CAPABILITY_RECORD_TYPES`; campos nuevos en `MailMessage`; tope de `MailQuery.limit` |
| `src/solercia_connect/config.py` | `TARGET_SPECS` Outlook: agregar `"mail.inbox.list"` (propaga a `doctor`, `providers`, perfiles, resolución de target) |
| `src/solercia_connect/ports.py` | `MailProviderPort`: decisión sobre `list_sent_mail` vs. método/parámetro nuevo |
| `src/solercia_connect/runtime.py` | `RoutePolicy.browser_fallback_capabilities` |
| `src/solercia_connect/application.py` | `_query`: rama de correo debe cubrir ambas capacidades; recorte `min(limit, …)` |
| `src/solercia_connect/providers/_microsoft.py` | `normalize_mail_message`: `internetMessageId`, `receivedDateTime`, parámetro `folder` |
| `src/solercia_connect/providers/microsoft_graph.py` | descriptor, `execute`, lectura de bandeja, `$select` de enviados, `endpoint` de procedencia parametrizado (`microsoft_graph.py:467-480`) |
| `src/solercia_connect/providers/m365_browser/selectors.py` | `outlook_inbox_messages` |
| `src/solercia_connect/providers/m365_browser/driver.py` | `read_records`, lectura de bandeja, `_SENT_MAIL_SELECT` |
| `src/solercia_connect/providers/m365_browser/adapter.py` | descriptor, `_BROWSER_EXTRACTION_CAPABILITIES` |
| `src/solercia_connect/cli.py` | `_query_payload` (ruta paralela de pruebas) |
| `src/solercia_connect/matrix.py` | filas nuevas para ambas rutas |
| `README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md` | matriz de capacidades y ejemplos |
| `agents/skills/solercia-connect/SKILL.md` (+ symlink en `.claude/skills/`) | tabla "What each route reads" (`SKILL.md:54-59`) |
| `tests/` | ver §9 |

**No requieren cambio:** `service.py` (`capability` es `str`), `envelope.py`, `serialization.py`,
`registry.py`, `bootstrap.py` (deriva la superficie desde `_BROWSER_EXTRACTION_CAPABILITIES`),
allowlist de hosts del navegador.

---

## Opciones comparadas

### Eje A — cómo se expresa la carpeta en el puerto y el despacho

| Opción | Descripción | Pros | Contras | Esfuerzo |
| --- | --- | --- | --- | --- |
| **A1. Método nuevo en el puerto** | `MailProviderPort.list_inbox_mail(query)`; cada adaptador implementa una segunda función completa | Explícito; ninguna firma existente cambia; `request_id` de peticiones actuales intacto | Duplica el constructor de petición y el bucle de paginación en dos adaptadores (≈4 copias); dos sitios donde el `$select` puede divergir | Medio |
| **A2. Carpeta en `MailQuery`** | `MailQuery.folder: MailFolder = SENT`; un único `list_mail(query)`; la capacidad sólo elige el folder | Un solo constructor por ruta; imposible que las proyecciones diverjan; extensible a Archive/Drafts | Cambia el `request_id` (y por tanto el `receipt_id`) de peticiones de correo existentes, porque `ConnectorRequest.create` serializa todo el payload (`domain.py:392-402`); toca la firma pública del puerto | Medio-alto |
| **A3. Carpeta derivada de la capacidad dentro de `execute`** | El puerto no cambia; cada adaptador tiene un helper privado parametrizado por carpeta y `execute` elige | Cambio mínimo de contrato público; sin impacto en `request_id`; un solo constructor por ruta | El puerto semántico deja de nombrar la segunda operación (menos legible desde `ports.py`) | **Bajo** |

### Eje B — campos nuevos en `MailMessage`

| Opción | Descripción | Pros | Contras | Esfuerzo |
| --- | --- | --- | --- | --- |
| **B1. Sólo `internet_message_id` + `folder`** | Sin instante de recepción | Mínimo | Command no puede distinguir "enviado el X" de "llegó el X"; la ventana pedida es de recepción | Bajo |
| **B2. `internet_message_id` + `folder` + `received_at: datetime \| None = None`** | Los tres, todos con default, al final del dataclass | Aditivo puro; `sent_at` intacto; responde exactamente lo que Command necesita; `received_at` queda `None` en enviados y en Gmail, que es honesto | Tres campos nuevos que hay que reflejar en los fixtures gemelos de paridad | **Bajo-medio** |
| **B3. Reemplazar `sent_at` por un `timestamp` genérico** | Un único instante con semántica variable | Modelo "limpio" | **Ruptura** del contrato publicado y del mapeador de Command; contradice `docs/ARCHITECTURE.md:138-144` | Alto |

### Eje C — techo de volumen

| Opción | Descripción | Pros | Contras | Esfuerzo |
| --- | --- | --- | --- | --- |
| **C1. Dejar 100 y segmentar por mes** | Sin cambio de código | Cero riesgo | **No resuelve el caso de uso**: una bandeja institucional supera 100 mensajes/mes y la truncación es silenciosa | Nulo |
| **C2. Subir `MailQuery.limit` a 1000** | Alinear con `CalendarQuery` (`domain.py:128-129`), CLI (`cli.py:229`) y servicio (`service.py:79`); ajustar `min(limit, …)` en `application.py:296` | Cierra una incoherencia ya existente entre las cuatro capas; 1000 es exactamente el techo estructural del navegador | Afecta también a `mail.sent.list` (más permisivo que hoy); lecturas más largas | **Bajo** |
| **C3. C2 + `$orderby` ascendente en bandeja** | Además, orden ascendente en Inbox en ambas rutas | Truncación determinista y reanudable estrechando la ventana | Introduce una diferencia de orden documentada frente a `mail.sent.list` en Graph (`desc`) | Bajo |

### Eje D — alcance de rutas

| Opción | Descripción | Pros | Contras | Esfuerzo |
| --- | --- | --- | --- | --- |
| **D1. Ambas rutas a la vez** | Graph + navegador simétricos | Preserva "los registros son idénticos entre rutas" (`README.md:99`, `docs/ARCHITECTURE.md`, `SKILL.md:54-61`); la cuenta puede migrar a OAuth sin volver a tocar Connect | Más superficie por revisar | **Medio** |
| **D2. Sólo navegador primero** | Declarar la capacidad únicamente en el descriptor del navegador | Desbloquea el caso real con menos código; Graph respondería `official_capability_unavailable`, que ya está en `browser_fallback_reasons` (`runtime.py:47`) y activaría el fallback | Rompe la promesa de paridad publicada; deja una capacidad "de segunda"; obliga a un segundo cambio y a documentar una excepción | Bajo |

---

## Recomendación

**A3 + B2 + C2/C3 + D1**, en un solo cambio.

1. **A3 (carpeta dentro del adaptador).** Es el menor cambio de contrato público, no altera los
   `request_id`/`receipt_id` de las peticiones de correo existentes y evita duplicar el constructor
   de petición. Concretamente: en `microsoft_graph.py` un helper privado
   `_list_folder_mail(query, *, folder, time_field)` al que `list_sent_mail` y `list_inbox_mail`
   delegan; en `driver.py`, `_read_folder_mail(query, *, url, time_field, timeout_ms)`. `ports.py`
   documenta la segunda operación sin obligar a firmar un método nuevo.
   *Si el equipo prefiere que `ports.py` nombre explícitamente ambas operaciones, A1 es aceptable
   con el mismo helper compartido por debajo; lo que hay que evitar es duplicar el `$select`.*
2. **B2 (tres campos aditivos con default, después de `attachments`).**
   `internet_message_id: str | None = None`, `folder: str | None = None`,
   `received_at: datetime | None = None`. `normalize_mail_message` recibe `folder` como argumento de
   palabra clave (no se puede derivar de `provenance.endpoint`, ver §5) y lee `internetMessageId` /
   `receivedDateTime` con el helper `field()` insensible a casing (`_microsoft.py:31-40`).
   `internetMessageId` se agrega también al `$select` de `mail.sent.list` en ambas rutas.
   **Sin subida de versión de esquema**, con nota de compatibilidad en la propuesta.
3. **C2 obligatorio, C3 recomendado.** Subir `MailQuery.limit` a 1000 no es cosmético: sin eso la
   funcionalidad no cubre el caso que la motiva. `$orderby` ascendente en bandeja hace la truncación
   determinista.
4. **D1.** La paridad de rutas es un contrato publicado en cuatro documentos y verificado por
   `tests/test_route_contract_parity.py`; romperla por conveniencia genera deuda inmediata.
5. **Gmail fuera de alcance**, declarado explícitamente en la propuesta.

**Orden TDD sugerido (rojo primero, un archivo de prueba por paso):**
1. `tests/test_route_contract_parity.py` — campos nuevos en ambos fixtures gemelos.
2. `tests/test_provider_contracts.py` — Inbox por Graph: endpoint, `$filter` sobre
   `receivedDateTime`, `$select` con ambos instantes, frontera de identidad.
3. `tests/test_browser_mail_extraction.py` — Inbox por navegador: URL, `$filter`, `$select` exacto,
   `nextLink`, corte por `limit`; y actualizar el conjunto exacto de `$select` de enviados
   (líneas 221-231) al incluir `InternetMessageId`.
4. `tests/test_runtime_policy.py` + `tests/test_browser_foundation.py` — allowlist de fallback y
   `browser_extraction_report`.
5. `tests/test_application.py` / `tests/test_service.py` / `tests/test_cli_lifecycle.py` — nueva
   capacidad extremo a extremo y tope de 1000.
6. `tests/test_cli_and_docs.py` — matriz y documentación.

---

## Riesgos

| # | Riesgo | Severidad | Mitigación |
| --- | --- | --- | --- |
| 1 | **Truncación silenciosa a 100 mensajes** si no se sube `MailQuery.limit`: la funcionalidad se entrega y no cubre el caso real | Alta | C2 obligatorio; además considerar que la propuesta declare qué ocurre al alcanzar el tope |
| 2 | **`$select` de bandeja sin `sentDateTime`** → todo registro falla con `malformed_provider_data` (`_microsoft.py:114`) | Alta | Prueba en rojo que fija el conjunto exacto del `$select` en ambas rutas |
| 3 | **`InternetMessageId` en Outlook REST v2**: es propiedad documentada del recurso `Message`, pero este repositorio sostiene que las pruebas offline no demuestran contratos vivos (ADR §4.5, `docs/decisions/...:68-69`) | Media | Validación `live` con `make test-live TYPE=outlook`; si el proveedor lo rechaza, `internet_message_id` queda `None` sin romper el registro |
| 4 | **Paridad falsa**: campos nuevos en el código pero no en los fixtures gemelos → la prueba de paridad pasa con ambos en `None` | Media | Paso 1 del orden TDD; asevere valores concretos, no igualdad de `None` |
| 5 | `tests/test_cli_lifecycle.py:362-363` recorre todo el enum contra `cli._query_payload`, que devolvería un `TicketQuery` para la capacidad nueva sin fallar | Media | Cubrir `_query_payload` o eliminarlo como ruta muerta; producción ya falla cerrado (`application.py:367-369`) |
| 6 | **Asimetría de orden** entre rutas (`desc` en Graph, `asc` en navegador) ya existente en `mail.sent.list`; replicarla en bandeja produce resultados dependientes de la ruta bajo truncación | Media | C3: `asc` en ambas rutas para bandeja; declarar explícitamente que `mail.sent.list` no cambia |
| 7 | **Cuenta `cccalderon@sdmujer.gov.co` sólo tiene ruta navegador** (sin `SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID`): la rama Graph no se puede validar en vivo con esa cuenta | Media | Cobertura hermética completa de Graph; validación viva de Graph diferida o con cuenta dedicada; el runbook ya documenta la restricción (`docs/runbooks/outlook.md:62-64`) |
| 8 | **Consumo desde Command**: hoy mapea `Capability.mail → "mail.sent.list"`; la nueva capacidad no se consume sola | Baja | Trabajo del lado de Command, fuera de alcance; declararlo en la propuesta como dependencia de integración |
| 9 | `request_id`/`receipt_id` cambiarían si se elige A2 (carpeta en `MailQuery`) | Baja | La recomendación A3 lo evita; si se elige A2, declararlo en la propuesta |
| 10 | Volumen del cambio frente al presupuesto de revisión de 400 líneas | Media | `sdd-tasks` debe emitir el pronóstico; con `delivery_strategy: auto-chain`, un corte natural es: (PR1) capacidad + ruta Graph + campos aditivos; (PR2) ruta navegador; (PR3) documentación y matriz |

---

## ¿Listo para propuesta?

**Sí.** El alcance está acotado y las decisiones abiertas son cuatro, todas con evidencia suficiente
para que la propuesta las cierre:

1. Eje A: A3 (recomendado) frente a A1.
2. Eje C: subir `MailQuery.limit` a 1000 — **decisión de producto**, porque afecta también a
   `mail.sent.list`.
3. Eje C3: `$orderby` ascendente en bandeja y si se toca o no el orden de enviados.
4. Confirmar Gmail fuera de alcance.

La propuesta debe además cumplir `openspec/config.yaml` → `rules.proposal`: declarar el impacto de
enrutamiento (oficial **y** fallback navegador Microsoft), el límite de seguridad afectado
(identidad de cuenta: la atestación previa a la lectura no cambia; alcance mínimo: `Mail.Read` ya
cubre la bandeja, **no se requiere scope nuevo**), la compatibilidad con `solercia.connect/v1`
(aditiva, sin subida de versión) y el plan de rollback (revertir las siete declaraciones de
capacidad deja el runtime en el estado actual; los campos aditivos son inertes para consumidores que
no los leen).

---

## Decisiones del orquestador (2026-08-26)

- **Eje A → A3.** Carpeta resuelta dentro de cada adaptador con un helper privado parametrizado; `ports.py` documenta la segunda operación. Sin cambio de `request_id`/`receipt_id`.
- **Eje B → B2.** `internet_message_id`, `folder` (`inbox|sent`) y `received_at` como campos aditivos con default al final de `MailMessage`; `internetMessageId` entra también al `$select` de `mail.sent.list` en ambas rutas. Sin subir la versión del esquema.
- **Eje C → C2 + C3.** `MailQuery.limit` sube a 1000 (afecta también a `mail.sent.list`, aceptado como decisión de producto: sin ello la bandeja institucional de un trimestre queda truncada en silencio); `$orderby` ascendente en bandeja en ambas rutas; el orden de `mail.sent.list` no cambia.
- **Eje D → D1.** Ambas rutas en el mismo cambio.
- **Gmail fuera de alcance**, declarado.
- Consumo desde Command (mapeo de `Capability.mail` a ambas capacidades y transporte por archivo) es trabajo del cambio 47 de Command.
