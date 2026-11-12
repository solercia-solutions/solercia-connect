# Decisión: transición de estado de issues de Jira

- **Estado:** Aceptado
- **Fecha de decisión:** 2026-10-25
- **Decidido por:** Camilo Calderón, propietario del producto
- **Ámbito:** Regla de escritura acotada, ruta oficial de Jira Cloud (REST v3), autenticación de
  Jira por API token y por OAuth, ruta de navegador de Jira y servicio HTTP
- **Aplicación:** Obligatoria para las capacidades `external.issues.transitions.list` y
  `external.issues.transition`
- **Actualiza:** [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisión 3.9), [`SECURITY.md`](../../SECURITY.md), [`AGENTS.md`](../../AGENTS.md) y el skill
  [`agents/skills/solercia-connect/SKILL.md`](../../agents/skills/solercia-connect/SKILL.md)
- **Complementa:** [`2026-10-19-escritura-categorias-outlook.md`](2026-10-19-escritura-categorias-outlook.md),
  la primera escritura del producto

## Contexto

Antes de esta decisión, Solercia Connect admitía una sola escritura. Las reglas vigentes lo dicen
de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| `AGENTS.md`, encabezado | «Its only write is assigning Outlook categories to one message (`mail.categories.set`)» |
| `AGENTS.md`, convenciones | «Writes need an explicit per-invocation `--allow-write` flag, off by default, never run over HTTP, never retry, and touch nothing but the categories of one message. Any other write needs a new ADR.» |
| `SECURITY.md`, encabezado | «Its only write is assigning Outlook categories to one message» |
| Registro de plataforma, decisión 3.9 | «Ninguna otra escritura queda autorizada.» |

El pedido que origina esta decisión quedó registrado en el documento de la feature
(`odd/tasks/google-jira-browser-fallback.md`, fase 8): «mi idea es conectarme con Jira en
pandora_proxy e ir haciendo el cambio de estado de las historias que tengo acceso por api»
(Camilo, 2026-10-25). La decisión D4 de ese documento la aceptó el mismo día con la nota «ADR de
transición». El contexto de uso es el sitio `idartes-tecnologia.atlassian.net` y el proyecto `CP`.

Solercia Connect ya lee issues de Jira (`external.issues.search`) por la ruta oficial, con la
identidad verificada en cada llamada. Cambiar el estado de una historia contradice las cuatro
reglas anteriores, así que la escritura necesita esta decisión antes de existir en el código.

## Decisión

1. **Segunda excepción.** Cambiar el estado de un issue existente mediante una transición de su
   flujo de trabajo (`external.issues.transition`) es la segunda escritura que Solercia Connect hace
   en un proveedor, después de las categorías de Outlook. No se crean, editan, comentan, asignan,
   archivan ni borran issues; no se escribe ningún campo, ni siquiera los de la pantalla de la
   transición; no se tocan registros de trabajo, enlaces, adjuntos, sprints ni la configuración del
   proyecto o del flujo.
2. **Autorización por invocación.** Cada invocación exige la bandera `--allow-write`, apagada por
   defecto. Sin ella la orden falla con `write_not_authorized` antes de construir el registro de
   proveedores o llamar a Jira. Ninguna variable de entorno ni perfil la sustituye. Quien usa la
   biblioteca directamente debe marcar la petición como autorizada; si no lo hace, el runtime la
   rechaza sin invocar ningún adaptador.
3. **Solo la ruta oficial.** La lectura de transiciones y la transición usan únicamente la API REST
   v3 de Jira Cloud. Ninguna de las dos capacidades entra en la lista de capacidades del navegador
   de la familia Jira, así que `--allow-browser-fallback` no abre ninguna ruta para ellas: aunque
   exista un adaptador de navegador de Jira, la política lo niega con `browser_fallback_denied` y
   el runtime nunca lo invoca.
4. **Cuenta obligatoria.** La orden exige `--account`. Antes de leer o escribir se verifica la
   identidad con `GET /rest/api/3/myself`, igual que en las lecturas actuales: el `accountId` debe
   coincidir con el guardado al conectar y un `emailAddress` visible debe coincidir con la cuenta.
   Una identidad distinta produce cero lecturas y cero escrituras.
5. **Un issue por invocación.** `--issue` recibe exactamente una clave que cumple el patrón
   `^[A-Z][A-Z0-9_]+-[1-9][0-9]*$`. No se aceptan listas, rangos ni JQL. Un cambio sobre varios
   issues exige que la persona usuaria nombre cada clave y que cada una se ejecute en su propia
   invocación.
6. **Escritura acotada.** La única llamada de escritura es un `POST` a las transiciones de un solo
   issue cuyo cuerpo es exactamente `{"transition": {"id": "<id>"}}`. El código no construye ningún
   otro cuerpo de escritura.
7. **Sin campos.** Si la transición elegida declara algún campo con `required: true`, la orden
   falla cerrada con `transition_requires_fields`. Esta capacidad nunca diligencia campos.
8. **Sin HTTP.** El servicio HTTP en loopback rechaza la capacidad por diseño, con o sin el
   interruptor de inicio de sesión interactivo: responde 403 sin invocar el servicio de aplicación,
   y el servicio de aplicación también la rechaza cuando se ejecuta en modo no interactivo. La
   escritura solo existe en la CLI canónica y en la biblioteca.
9. **Sin reintentos.** Una escritura fallida no se reintenta de forma automática. Quien la invoca
   repite la orden, que vuelve a leer el estado y calcula de nuevo el resultado.
10. **Autenticación.** Los API tokens clásicos y los API tokens con alcances (`scoped`) pueden
    escribir. Un perfil OAuth falla cerrado con `write_scope_missing` antes de cualquier llamada,
    porque la concesión guardada no incluye `write:jira-work`; la remediación es reconectar la
    cuenta con un API token. Los alcances OAuth por defecto de Atlassian no cambian.
11. **Verificación posterior.** Después del `POST` se lee de nuevo el estado. Si no coincide con el
    destino pedido, la orden falla con `write_unverified` y advierte que la escritura pudo haberse
    aplicado de forma parcial.

## Contrato de las capacidades

### Lectura: `external.issues.transitions.list`

| Opción | Regla |
| --- | --- |
| `--issue` | Obligatoria. Una clave exacta de Jira, por ejemplo `CP-1356`. Distingue mayúsculas. |
| `--account` | Elige la conexión de Jira de esa cuenta, como en `external.issues.search`. |

No usa `--start`, `--end` ni `--jql`. Verifica la identidad, lee el estado actual con
`GET /rest/api/3/issue/{clave}?fields=status` y las transiciones con
`GET /rest/api/3/issue/{clave}/transitions?expand=transitions.fields`. Devuelve un registro por
transición con `issue_key`, `transition_id`, `transition_name`, `to_status`, `to_status_category`
(la clave de la categoría del estado destino), `requires_fields`, `current_status`,
`current_status_category` y `provenance`. Un issue sin transiciones disponibles devuelve cero
registros.

### Escritura: `external.issues.transition`

| Opción | Regla |
| --- | --- |
| `--issue` | Obligatoria. Una clave exacta de Jira; mismo patrón de la lectura. |
| `--to` | Obligatoria. Nombre del estado destino. Se recorta y se compara sin distinguir mayúsculas y minúsculas. |
| `--allow-write` | Obligatoria para escribir; apagada por defecto. |
| `--account` | Obligatoria. |

Comportamiento determinista:

1. Verifica la identidad.
2. Lee el estado actual del issue.
3. Si el estado actual ya es el destino, responde con éxito y `changed: false`, sin `POST`. Repetir
   la misma orden es idempotente.
4. Lee las transiciones con sus campos (`expand=transitions.fields`).
5. Selecciona las transiciones cuyo estado destino tiene el nombre pedido. Si no hay ninguna, falla
   con `transition_unavailable` y lista los nombres de estado destino disponibles, que no son
   secretos. Si hay más de una, falla con `transition_ambiguous`.
6. Si la transición elegida exige algún campo, falla con `transition_requires_fields`.
7. Emite un solo `POST` con `{"transition": {"id": "<id>"}}`. Un `409` falla con `write_conflict`.
8. Lee de nuevo el estado. Si no es el destino, falla con `write_unverified`.

El registro `solercia.connect/record/v1` lleva `issue_key`, `previous_status`, `status`,
`transition_id`, `transition_name`, `changed` y `provenance`. El sobre `solercia.connect/v1` lleva
el recibo determinista, como cualquier otra capacidad.

### Errores tipados

| Código | Cuándo |
| --- | --- |
| `invalid_input` | La clave no cumple el patrón, falta `--to` o falta `--account`. |
| `write_not_authorized` | Falta `--allow-write`; no se hizo ninguna llamada al proveedor. |
| `write_not_available_over_http` | La escritura llegó por el servicio HTTP. |
| `write_scope_missing` | El perfil de la cuenta es OAuth y la concesión no incluye `write:jira-work`. |
| `issue_not_found` | Jira responde `404`, o resuelve la clave a otro issue porque fue movido. |
| `invalid_credentials` / `authorization_denied` | Jira responde `401` o `403`. |
| `transition_unavailable` | Ninguna transición disponible lleva al estado pedido. |
| `transition_ambiguous` | Más de una transición lleva al estado pedido. |
| `transition_requires_fields` | La transición elegida exige campos. |
| `write_conflict` | Jira responde `409` al `POST`. |
| `write_unverified` | El estado leído después del `POST` no es el destino; la escritura pudo aplicarse. |
| `malformed_provider_data` | La respuesta de Jira no tiene la forma documentada. |

## Rutas, endpoints y alcances

Las citas siguientes son textuales y conservan el idioma de la fuente; el análisis va aparte.

### Leer el estado actual

- «GET /rest/api/3/issue/{issueIdOrKey}» y, para el parámetro `fields`, «A list of fields to return
  for the issue.» (*The Jira Cloud platform REST API*, grupo *Issues*, «Get issue»).
- «The issue is identified by its ID or key, however, if the identifier doesn't match an issue, a
  case-insensitive search and check for moved issues is performed. If a matching issue is found its
  details are returned, a 302 or other redirect is **not** returned. The issue key returned in the
  response is the key of the issue found.» (misma operación).
- «404 … Returned if the issue is not found or the user does not have permission to view it.»
  (misma operación, «Responses»).

Lo anterior implica que Jira puede responder con un issue cuya clave no es la pedida. La orden
compara la clave de la respuesta con la solicitada y falla cerrada con `issue_not_found` si
difieren, para no escribir sobre un issue que la persona no nombró.

### Leer las transiciones

- «GET /rest/api/3/issue/{issueIdOrKey}/transitions» y «Returns either all transitions or a
  transition that can be performed by the user on an issue, based on the issue's status.» («Get
  transitions»).
- Parámetro `expand`: «This parameter accepts `transitions.fields`, which returns information about
  the fields in the transition screen for each transition. Fields hidden from the screen are not
  returned.» (misma operación, «Query parameters»).
- «However, if the user does not have the _Transition issues_ project permission the response will
  not list any transitions.» (misma operación).
- `required`: «Whether the field is required.» (esquema `FieldMetadata` de la especificación
  OpenAPI publicada por Atlassian).
- Alcance OAuth clásico recomendado: `read:jira-work` (misma operación, «Scopes»).

### Hacer la transición

- «POST /rest/api/3/issue/{issueIdOrKey}/transitions» y «Performs an issue transition and, if the
  transition has a screen, updates the fields from the transition screen.» («Transition issue»).
- Permisos: «_Browse projects_ and _Transition issues_ project permission for the project that the
  issue is in.» (misma operación).
- «204 … Returned if the request is successful.» y «409 … Returned if the issue could not be updated
  due to a conflicting update.» (misma operación, «Responses»).
- Alcance OAuth clásico recomendado: `write:jira-work` (misma operación, «Scopes»).
- «`write:jira-work` | Create and manage issues | Create and edit issues in Jira, post comments as
  the user, create worklogs, and delete issues.» (*Jira scopes for OAuth 2.0 (3LO) and Forge apps*,
  «Classic scopes»).

Lo anterior implica que no existe un alcance clásico más estrecho que `write:jira-work` para hacer
una transición: el permiso autoriza también crear, comentar y borrar issues. Un API token clásico
tampoco tiene alcances propios y actúa con todos los permisos de la persona. El límite real lo pone
el código (puntos 5, 6 y 7 de la decisión), no el alcance ni el token.

## Estado de implementación

Las dos capacidades se implementan con pruebas fuera de línea y quedan como `implemented-offline`
en la matriz de capacidades hasta una prueba viva controlada sobre el proyecto `CP`, que exige la
aprobación expresa del propietario. La ruta OAuth queda bloqueada por diseño mientras la concesión
no incluya `write:jira-work`; agregar ese alcance es una decisión aparte.

## Límites que no cambian

- La identidad se prueba antes de cualquier llamada de datos; una cuenta distinta produce cero
  lecturas y cero escrituras.
- Las credenciales de Jira siguen en el almacén seguro o, en modo proyecto, en la carpeta privada
  del proyecto; nunca se registran ni se imprimen.
- La ruta de navegador de Jira sigue siendo de solo lectura y sirve únicamente
  `external.issues.search`.
- `mail.categories.set` conserva intactas sus reglas.

## Riesgos

- **Privilegio del token.** Un API token clásico autoriza cualquier acción que la persona pueda
  hacer en Jira. El código limita la escritura a una transición; una fuga del token ya era un
  riesgo de las lecturas y no cambia con esta decisión.
- **Efectos del flujo.** Una transición puede disparar funciones posteriores, reglas de
  automatización y notificaciones a los observadores del issue. Connect no las controla; si una
  regla cambia el estado después de la transición, la verificación posterior falla con
  `write_unverified`.
- **Concurrencia.** Jira no ofrece una precondición de versión para las transiciones. Si otra
  persona cambia el estado entre la lectura y el `POST`, Jira rechaza la transición (`400` o `409`)
  o la aplica desde el estado nuevo; la lectura posterior deja constancia del resultado.
- **Nombres de estado.** Los nombres dependen de la configuración de cada proyecto y pueden
  repetirse en varias transiciones; la orden falla cerrada ante la ambigüedad en lugar de elegir.
- **Uso por agentes.** Un agente podría encadenar transiciones sin instrucción. El skill restringe
  la escritura a una petición explícita, un issue por invocación y nunca en lotes sin que la
  persona nombre cada clave.

## Consecuencias

- Un agente en `pandora_proxy` puede cambiar el estado de una historia de `CP` con una orden
  explícita, con recibo y verificación, sin reimplementar autenticación ni llamadas a Jira.
- El código, no el alcance, limita la escritura a la transición de un issue. Una prueba fija que el
  cuerpo de la escritura contiene solo el identificador de la transición.
- Las cuentas conectadas por OAuth necesitan reconectarse con un API token para escribir.

## Plan de reversión

Retirar `external.issues.transition` de `WRITE_CAPABILITIES`, del `TargetSpec` de Jira y del
descriptor del adaptador oficial deja la bandera `--allow-write` sin capacidad de Jira que
autorizar; la lectura `external.issues.transitions.list` se retira de la misma forma. No hay
estado local ni migración que deshacer. Las transiciones ya hechas quedan en el historial del
issue y se revierten desde Jira con otra transición.

## Alternativas descartadas

- **El conector Atlassian Rovo:** ofrece transiciones, pero solo se usa dentro de sesiones de
  Claude. No sirve a los agentes que invocan `solercia-connect`, no deja el recibo determinista ni
  liga la escritura a la identidad verificada de la conexión guardada.
- **Agregar `write:jira-work` a los alcances OAuth por defecto:** ampliaría el privilegio de todas
  las conexiones OAuth, también las que solo leen, y obligaría a reautorizar. Sigue como propuesta
  aparte.
- **Diligenciar campos de la pantalla de transición:** abre la escritura de cualquier campo del
  issue. Una transición que exige campos falla cerrada.
- **Transiciones por lote o por JQL:** una consulta mal formada cambiaría issues que nadie nombró.
  Se exige una clave por invocación.
- **Exponer la escritura por HTTP o por la ruta de navegador:** un cliente de loopback o una sesión
  web podrían cambiar issues sin una persona en la terminal ni la identidad de la API.

## Referencias oficiales

- [Get issue — The Jira Cloud platform REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-get)
- [Get transitions — The Jira Cloud platform REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-get)
- [Transition issue — The Jira Cloud platform REST API v3](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-issueidorkey-transitions-post)
- [Jira scopes for OAuth 2.0 (3LO) and Forge apps](https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/)
- [Especificación OpenAPI de la API REST v3 de Jira Cloud](https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json)
