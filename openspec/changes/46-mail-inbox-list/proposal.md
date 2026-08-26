# Propuesta: `mail.inbox.list` para Outlook (rutas oficial y navegador)

> Fase `sdd-propose` del cambio `46-mail-inbox-list`. Store híbrido: este contenido está también en
> Engram (`sdd/46-mail-inbox-list/proposal`). Entrada: `exploration.md` (Engram #3695).

## Intención

Solercia Command necesita, para el tablero de SDMujer, **bandeja de entrada y enviados entre
2026-06-01 y 2026-08-27**. Hoy Connect sólo lee `mail.sent.list`: responde qué se envió, nunca qué
llegó, que es justamente el insumo que falta.

**Problema de negocio.** El vacío no es sólo funcional, es de confianza en el dato:

| Síntoma actual | Consecuencia operativa |
| --- | --- |
| No existe capacidad de bandeja | Command no puede reconstruir la conversación entrante; el analista vuelve a Outlook a mano |
| `MailQuery.limit` topa en 100 (`domain.py:105,110-111`) | Una ventana trimestral **se trunca en silencio**: devuelve 100 registros y estado exitoso, sin error ni advertencia |
| `MailMessage` no expone instante de recepción | Aunque hubiera bandeja, no se puede afirmar "llegó dentro de la ventana": `sent_at` responde otra pregunta |

El tercer punto convierte al segundo en un riesgo de decisión: un informe incompleto que se ve
completo es peor que un error tipado.

**Éxito.** Una sola invocación por ruta entrega la bandeja de un trimestre institucional completa,
con instante de recepción y carpeta explícitos, sin relajar ninguna garantía fail-closed.

## Alcance

### En alcance

- Capacidad nueva `mail.inbox.list` para Outlook, declarada en los **siete** puntos que exige el
  diseño fail-closed, más los cuatro sitios de despacho.
- Lectura de bandeja en **ambas rutas**: Microsoft Graph (oficial) y navegador supervisado
  (Outlook REST v2).
- Tres campos aditivos con valor por defecto al final de `MailMessage`:
  `internet_message_id: str | None`, `folder: str | None` (`inbox|sent`),
  `received_at: datetime | None`.
- `internetMessageId` incorporado al `$select` de `mail.sent.list` en las dos rutas.
- Tope de `MailQuery.limit` de 100 a **1000** (alinea `CalendarQuery`, CLI y servicio HTTP, que ya
  aceptan 1000).
- `$orderby` **ascendente** en bandeja en ambas rutas. El orden de `mail.sent.list` no cambia.
- Matriz de capacidades, `README.md`, runbook de Outlook y el `SKILL.md` del agente.

### Fuera de alcance

- **Bandeja de entrada de Gmail.** `GoogleGmailAdapter` codifica la carpeta en la consulta
  (`labelIds="SENT"`, `q="from:me …"`); requeriría rama de despacho, `TARGET_SPECS`, matriz, docs y
  pruebas propias. Declarado explícitamente: ningún cambio de puerto hace que Gmail la anuncie por
  accidente.
- Carpetas Archive/Drafts/carpeta arbitraria por parámetro.
- Consumo del lado de Command (mapear `Capability.mail` a ambas capacidades, transporte por
  archivo): es el cambio 47 de Command, declarado aquí como dependencia de integración.
- Subir `_OUTLOOK_PAGE_SIZE` o `_OUTLOOK_MAX_PAGES`: 1000 es el techo estructural del navegador y se
  respeta tal cual.
- Cambiar el orden de `mail.sent.list`, la allowlist de hosts del navegador o cualquier `ReasonCode`.

## Capacidades

> Contrato con la fase `sdd-spec`. **`openspec/specs/` no existe todavía en este repositorio**: no
> hay línea base contra la cual generar deltas, así que cada capacidad de esta sección se especifica
> como spec propio dentro de la carpeta del cambio.

### Capacidades nuevas

- `mail-inbox-list`: lectura de bandeja de entrada de Outlook por ruta oficial y por navegador
  supervisado, con ventana horaria de fin exclusivo, orden ascendente y paridad de registro entre
  rutas.

### Capacidades modificadas

- `mail-record-contract`: `MailMessage` gana tres campos opcionales al final del dataclass y el
  `$select` de enviados incorpora `internetMessageId`. Adición pura, sin subida de versión de
  esquema.
- `mail-query-limits`: `MailQuery.limit` pasa de `1..100` a `1..1000`, aplicable también a
  `mail.sent.list`. Es **decisión de producto**, no ajuste cosmético: sin ella la funcionalidad se
  entrega truncada.
- `capability-advertisement`: `doctor`, `providers`, `CAPABILITY_MATRIX`, documentación y el
  `SKILL.md` del agente deben anunciar la capacidad nueva por ambas rutas.

## Enfoque

### 1. Declarar la capacidad en los siete puntos

Una capacidad no vive en un solo lugar. Omitir cualquiera produce un fallo tipado distinto — que es
el comportamiento deseado, no un defecto:

| # | Punto de declaración | Archivo | Fallo si falta |
| --- | --- | --- | --- |
| 1 | Enum `Capability` | `domain.py:11-19` | `invalid_input` |
| 2 | `_CAPABILITY_RECORD_TYPES` → `MailMessage` | `domain.py:447-456` | `ValueError` al validar registros |
| 3 | `TARGET_SPECS` (Outlook) | `config.py:80-88` | `invalid_input` en `application.py` |
| 4 | Descriptor Graph | `microsoft_graph.py:57-71` | `official_capability_unavailable` |
| 5 | Descriptor navegador | `m365_browser/adapter.py:79-93` | `browser_capability_not_implemented` |
| 6 | `RoutePolicy.browser_fallback_capabilities` | `runtime.py:54-63` | `browser_fallback_denied` |
| 7 | `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]` | `m365_browser/adapter.py:39-52` | La superficie de sesión no se resuelve; `connect`/`status` no la anuncian |

Despacho, en cuatro sitios más: `microsoft_graph.execute` (`:103-133`), `driver.read_records`
(`:401-434`), `application._query` (`:282`) y `cli._query_payload` (`:661`).

### 2. Resolver la carpeta dentro del adaptador (eje A → A3)

Los puertos no cambian de firma. Cada adaptador expone un helper privado parametrizado por carpeta,
al que delegan enviados y bandeja:

- Graph: `_list_folder_mail(query, *, folder, time_field)`.
- Navegador: `_read_folder_mail(query, *, url, time_field, timeout_ms)`.
- `ports.py` **documenta** la segunda operación sin obligar a firmar un método nuevo.

Por qué así: mantiene un solo constructor de petición por ruta (imposible que las proyecciones
diverjan) y **no altera `request_id`/`receipt_id` de las peticiones de correo existentes**, que sí
cambiarían al meter la carpeta dentro de `MailQuery`.

### 3. Peticiones simétricas

| Aspecto | Graph oficial | Navegador (Outlook REST v2) |
| --- | --- | --- |
| Endpoint | `{base}/me/mailFolders/inbox/messages` | `https://outlook.office.com/api/v2.0/me/MailFolders/Inbox/messages` (constante hermana en `selectors.py`) |
| `$filter` | `receivedDateTime ge … and … lt …` | `ReceivedDateTime ge … and … lt …` |
| `$select` | **conserva `sentDateTime`** + `receivedDateTime` + `internetMessageId` | ídem con casing de REST v2 |
| `$orderby` | `receivedDateTime asc` | `ReceivedDateTime asc` |
| Paginación / seguridad | `_paged_values` + `require_same_origin_https_url` | `$top` 50, 20 páginas, `_is_allowed_outlook_request_url` |

**Detalle crítico:** `MailMessage.sent_at` es no opcional y `normalize_mail_message` falla cerrado
con `malformed_provider_data` si el instante de envío falta. Si el `$select` de bandeja omite
`sentDateTime`/`SentDateTime`, **todo registro de bandeja falla**. Un mensaje recibido sí lo trae:
basta con no quitarlo de la proyección, y una prueba fija el conjunto exacto.

### 4. Normalización

`normalize_mail_message` recibe `folder` como **argumento de palabra clave**. No se puede derivar de
`provenance.endpoint`: el adaptador del navegador lo sobrescribe con la evidencia de identidad
(`"outlook-rest-v2-me"`). `internetMessageId` y `receivedDateTime` se leen con el helper `field()`,
insensible a casing, ya existente.

### 5. Volumen y orden

`MailQuery.limit` sube a 1000 y se ajusta el recorte de `application.py:296`. 1000 no es arbitrario:
es exactamente el techo estructural del navegador (20 páginas × `$top` 50) y el valor que ya usan
`CalendarQuery`, la opción de CLI y el campo HTTP. El orden ascendente hace la truncación
**determinista y reanudable**: al alcanzar el tope se devuelve siempre el prefijo temporal más
antiguo, y el operador estrecha la ventana para continuar.

## Impacto de enrutamiento

**Ambas rutas, en este mismo cambio** (eje D → D1).

| Ruta | Impacto |
| --- | --- |
| Oficial (Microsoft Graph) | Capacidad nueva en descriptor y despacho; lectura de bandeja; `$select` de enviados ampliado |
| Fallback navegador Microsoft | Capacidad nueva en descriptor, allowlist de fallback y superficie de extracción; endpoint de bandeja como constante declarada |
| Google / Jira / GLPI | Sin cambio |

La paridad de registros entre rutas es contrato publicado en cuatro documentos y verificado por
`tests/test_route_contract_parity.py`. Entregar sólo la ruta navegador dejaría una capacidad de
segunda y obligaría a un segundo cambio.

**El fallback sigue siendo opt-in explícito en cinco capas**, ninguna relajada: `--allow-browser-fallback`
por invocación, razón tipada allowlisteada, capacidad allowlisteada, capacidad en el descriptor y
capacidad en la superficie de extracción. El servicio HTTP sigue rechazando el navegador por diseño.

## Límite de seguridad afectado

| Dimensión | Estado |
| --- | --- |
| **Identidad de cuenta** | **Sin cambio.** La atestación canónica previa a cualquier lectura permanece: `_identity()` primero en Graph, `_prepare` antes de `read_records` en navegador, y `_validate_records` verifica cuenta por registro |
| **Alcance mínimo de permisos** | **Sin scope nuevo.** `Mail.Read` ya cubre la bandeja de entrada |
| **Almacenamiento de secretos** | **Sin cambio.** El bearer sigue siendo transitorio, no imprimible y limpiado en `finally`; nada de tokens, cookies ni cuerpos crudos en registros |
| **Allowlist de hosts del navegador** | **Sin cambio.** `outlook.office.com` ya está permitido; la bandeja vive en ese mismo origen |
| **Códigos de razón** | **Sin cambio.** Ningún `ReasonCode` nuevo |
| **Endpoints** | Declarados como constantes, nunca construidos con datos del usuario |

## Compatibilidad con `solercia.connect/v1` y recibos

- **Sin subida de versión de esquema** (`solercia.connect/v1` ni `solercia.connect/record/v1`).
  `to_primitive` recorre los campos del dataclass: los campos nuevos aparecen como claves nuevas del
  JSON y son inertes para todo consumidor que no los lea.
- **Adición pura**: los tres campos van **después de `attachments`** y con valor por defecto. Es
  obligatorio: `MailMessage` se construye posicionalmente en `_microsoft.py`, `google.py` y
  `tests/fakes.py`.
- **Recibos deterministas intactos** para las peticiones existentes: al no tocar `MailQuery` ni las
  firmas de puerto, `request_id` y `receipt_id` de `mail.sent.list` no cambian. `receipt_fingerprint`
  mezcla petición y pasos, nunca el orden ni el contenido de los registros.
- **Sin cambio de esquema HTTP**: `RunRequest.capability` es `str` libre, validado aguas abajo.
- `received_at` queda `None` en enviados y en Gmail. Es honesto: no se inventa un instante.

## Áreas afectadas

| Área | Impacto | Qué cambia |
| --- | --- | --- |
| `src/solercia_connect/domain.py` | Modificado | Enum, tipos de registro, tres campos, tope de `limit` |
| `src/solercia_connect/config.py` | Modificado | `TARGET_SPECS` de Outlook |
| `src/solercia_connect/ports.py` | Modificado | Documentar la segunda operación de correo |
| `src/solercia_connect/runtime.py` | Modificado | Allowlist de fallback |
| `src/solercia_connect/application.py` | Modificado | `_query` para ambas capacidades; recorte de `limit` |
| `src/solercia_connect/cli.py` | Modificado | `_query_payload` cubre la capacidad nueva fallando cerrado |
| `src/solercia_connect/matrix.py` | Modificado | Filas nuevas por ruta |
| `src/solercia_connect/providers/_microsoft.py` | Modificado | `normalize_mail_message` con `folder`, `internetMessageId`, `receivedDateTime` |
| `src/solercia_connect/providers/microsoft_graph.py` | Modificado | Descriptor, despacho, helper de carpeta, `$select`, `endpoint` de procedencia |
| `.../m365_browser/selectors.py` | Modificado | `outlook_inbox_messages` |
| `.../m365_browser/driver.py` | Modificado | Despacho, helper de carpeta, `_SENT_MAIL_SELECT` |
| `.../m365_browser/adapter.py` | Modificado | Descriptor y superficie de extracción |
| `README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md` | Modificado | Matriz y ejemplos |
| `agents/skills/solercia-connect/SKILL.md` | Modificado | Tabla "What each route reads" |
| `tests/` | Modificado | Paridad, contratos de proveedor, extracción de navegador, política de runtime, aplicación, servicio, CLI, docs |

**Sin cambio:** `service.py`, `envelope.py`, `serialization.py`, `registry.py`, `bootstrap.py`,
allowlist de hosts del navegador.

## Riesgos

| # | Riesgo | Probabilidad | Mitigación |
| --- | --- | --- | --- |
| 1 | Entregar la capacidad sin subir el tope: se lee bandeja pero truncada en silencio | Alta | Subir `MailQuery.limit` a 1000 es parte del alcance, no un extra; prueba de lectura paginada hasta el tope |
| 2 | `$select` de bandeja sin `sentDateTime` → todo registro falla con `malformed_provider_data` | Alta | Prueba en rojo que fija el **conjunto exacto** de `$select` en ambas rutas antes del código |
| 3 | `InternetMessageId` en Outlook REST v2: propiedad documentada, pero este repositorio sostiene que las pruebas offline no demuestran contratos vivos | Media | Validación `live` con `make test-live TYPE=outlook`; si el proveedor lo rechaza, `internet_message_id` queda `None` **sin romper el registro** |
| 4 | Paridad falsa: campos nuevos en el código pero no en los fixtures gemelos → la prueba pasa con ambos en `None` y no demuestra nada | Media | Primer paso del orden TDD; la prueba asevera **valores concretos**, nunca igualdad de `None` |
| 5 | `tests/test_cli_lifecycle.py:362-363` recorre todo el enum contra `cli._query_payload`, que devolvería un `TicketQuery` para la capacidad nueva sin fallar | Media | Cubrir `_query_payload` para la capacidad nueva **fallando cerrado**; producción ya lanza `ValueError` |
| 6 | Asimetría de orden preexistente entre rutas en `mail.sent.list` (`desc` Graph / `asc` navegador) replicada en bandeja | Media | `asc` en ambas rutas para bandeja; declarar explícitamente que `mail.sent.list` no cambia |
| 7 | La cuenta `cccalderon@sdmujer.gov.co` sólo tiene ruta navegador (sin `SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID`): la rama Graph no se valida en vivo con ella | Media | Cobertura hermética completa de Graph; validación viva diferida o con cuenta dedicada; el runbook ya documenta la restricción |
| 8 | Subir el tope a 1000 también hace más permisivo `mail.sent.list` | Baja | Aceptado como decisión de producto; el techo estructural del navegador sigue siendo 1000 y no se toca |
| 9 | La capacidad nueva no se consume sola: Command mapea `Capability.mail → "mail.sent.list"` | Baja | Dependencia de integración declarada (cambio 47 de Command), fuera de alcance aquí |

## Plan de rollback

Por capas, de menor a mayor alcance:

1. **Revertir las siete declaraciones de capacidad** devuelve el runtime al estado actual: la
   capacidad deja de ser enrutable y cada capa vuelve a fallar cerrado con su razón tipada. No queda
   estado persistido que limpiar salvo los perfiles de conexión, que se regeneran con `connect`.
2. **Los tres campos aditivos son inertes**: revertirlos sólo elimina claves del JSON; ningún
   consumidor existente las lee. No hay migración de datos porque este cambio no persiste nada.
3. **El tope de `limit`** se revierte cambiando una sola línea de validación; las lecturas ya
   emitidas siguen siendo válidas.
4. **Autenticación, enrutamiento y fallback no se tocan** más allá de agregar la capacidad a las
   allowlists: no hay cambio de credenciales, de política de rutas ni de la allowlist de hosts que
   revertir.
5. Con entrega encadenada, cada PR se revierte con `git revert` en orden inverso; PR3 y PR2 son
   reversibles de forma independiente de PR1.

## Dependencias

- **Cambio 47 de Solercia Command** (integración, no bloqueante para este cambio): mapear
  `Capability.mail` a ambas capacidades y consumir el sobre por archivo.
- Validación viva de Outlook REST v2 para `InternetMessageId` (opt-in, marcador `live`).
- Sin dependencias de librerías, credenciales ni infraestructura nuevas.

## Criterios de aceptación

- [ ] Pruebas herméticas cubren la bandeja **en ambas rutas**: endpoint, `$filter` sobre el instante
      de recepción, conjunto exacto de `$select` (incluido `sentDateTime`), `$orderby` ascendente,
      seguimiento de `nextLink` y corte por `limit`.
- [ ] La prueba de paridad asevera **valores concretos** de `internet_message_id`, `folder` y
      `received_at` en los dos fixtures gemelos; ningún campo nuevo se compara contra `None`.
- [ ] `folder` vale `"inbox"` en bandeja y `"sent"` en enviados, por ambas rutas.
- [ ] `doctor` y `providers` anuncian `mail.inbox.list` para Outlook en las dos rutas.
- [ ] Una lectura con `limit=1000` pagina correctamente y no lanza `ValueError`; `limit=1001` sigue
      siendo rechazado.
- [ ] `cli._query_payload` falla cerrado para la capacidad nueva en lugar de devolver un
      `TicketQuery`.
- [ ] `request_id`/`receipt_id` de una petición `mail.sent.list` equivalente **no cambian**.
- [ ] `README.md`, runbook de Outlook, `docs/USO-CON-AGENTES.md`, `CAPABILITY_MATRIX` y el `SKILL.md`
      del agente declaran la capacidad; `tests/test_cli_and_docs.py` lo verifica.
- [ ] `make check` en verde: formato, lint, mypy y la suite completa sin regresión sobre la línea
      base de 402 pruebas, con cobertura de ramas ≥ 80 %.

## Carga de revisión

Presupuesto de sesión: 2000 líneas; presupuesto por PR: 400. El pronóstico formal lo emite
`sdd-tasks`; con `delivery_strategy: auto-chain` el corte natural es:

| PR | Contenido | Autonomía |
| --- | --- | --- |
| **PR1** | Capacidad + campos aditivos + ruta Graph (enum, tipos de registro, `TARGET_SPECS`, descriptor, despacho, helper de carpeta, normalización, tope de `limit`) y sus pruebas | Entregable: la bandeja funciona por ruta oficial |
| **PR2** | Ruta navegador (selector, descriptor, superficie de extracción, allowlist de fallback, helper de carpeta) y sus pruebas | Entregable: paridad de rutas completa |
| **PR3** | Matriz de capacidades, `README.md`, runbook, `USO-CON-AGENTES.md` y `SKILL.md` | Entregable: la capacidad queda anunciada y documentada |

Cadena de ramas: PR1 apunta a la rama del cambio; PR2 a PR1; PR3 a PR2.
