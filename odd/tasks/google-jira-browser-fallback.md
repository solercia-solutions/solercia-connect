# ODD — Google y Jira con cualquier cuenta: oficial primero, navegador como respaldo

- **Feature:** `google-jira-browser-fallback`
- **Rama:** `feat/google-jira-fallback` (worktree
  `~/Proyectos/SOLERCIA/solercia-connect-worktrees/google-jira-fallback`)
- **Base:** `feat/outlook-categorias` en `f8c1788`. Esa rama contiene `feat/http-interactive-login`
  y no está integrada en `main` (28 commits por delante el 2026-10-25), así que se parte de su punta.
- **Espejo en Engram:** `odd/google-jira-browser-fallback/tasks` (proyecto `solercia-connect`).

## Objetivo

Que Solercia Connect lea Google (Gmail, Google Calendar y Google Meet) y Jira con cualquier cuenta.
Primero se usa la API oficial (OAuth o token). Si la ruta oficial no está disponible, se usa el
navegador supervisado con Playwright, con las mismas garantías que hoy tiene Outlook.

## Pedido

Camilo, 2026-10-25: «quiero que también haya compatibilidad con conectores de Google o de Gmail y
de Jira, debe servir cualquier cuenta […] si se puede con permisos pues bien […] pero si no pues
por fallback». Al preguntarle por la ADR respondió: «Jira de ser posible por API e integraciones e
igual Google, pero si no se puede entonces por Playwright».

## Problema actual (evidencia)

| Hecho | Evidencia |
| --- | --- |
| Playwright solo puede actuar como respaldo de Microsoft. | `docs/decisions/2026-08-23-solercia-platform-decisions.md:60-63` (§3.2); `src/solercia_connect/runtime.py:68` (`browser_fallback_families={MICROSOFT}`) |
| `connect jira` responde «no implementado». Las credenciales solo salen de `JIRA_BASE_URL`, `JIRA_EMAIL` y `JIRA_API_TOKEN`, así que se admite un solo sitio y una sola cuenta. | `connections.py:103-112`; `bootstrap.py:161-172`; `config.py:155-160` |
| La identidad de Jira es el `accountId` opaco y el perfil exige un correo. Además, `/myself` puede ocultar `emailAddress` según la privacidad de la cuenta. | `providers/jira.py:130-144`; `config.py:175-179`; developer.atlassian.com, grupo Myself |
| Los fallos reales de Google no habilitan el respaldo. El 403 se clasifica como `AUTHORIZATION_DENIED` y `admin_policy_enforced` como `REQUEST_REJECTED`. | `transport.py:134-135`; `oauth.py:584`; `runtime.py:46-55` |
| `connect google-meet` completa el OAuth y después falla, porque `_verify_identity` no tiene rama para Meet. Ninguna prueba lo detecta. | `connections.py:534-576` |
| Los conjuntos de razones que habilitan el respaldo no coinciden entre sí. | `connections.py:40-42`, `runtime.py:46-55` y `docs/ARCHITECTURE.md:108` |
| El driver, el perfil y el registro de navegador están atados a Microsoft. | `m365_browser/driver.py:160-216`; `browser_profile.py:21-31`; `registry.py:22-24` |

## Por qué

- **Google:** `gmail.readonly` y `drive.readonly` son alcances restringidos.
  - En estado de prueba se admiten como máximo 100 usuarios y la autorización vence a los 7 días.
  - Un administrador de Workspace puede bloquear las apps no configuradas o restringir Gmail y
    Drive, y en ese caso responde `admin_policy_enforced`.
  - Fuentes: support.google.com/cloud/answer/15549945 y support.google.com/a/answer/7281227.
- **Jira:**
  - Atlassian OAuth (3LO) exige `client_secret` y no admite PKCE (ECO-283 sigue abierto).
  - Una organización puede impedir que se usen API tokens.
  - Fuente: support.atlassian.com, «authentication policy settings».

## Restricciones

- No se leen ni se imprimen `.env`, tokens, cookies ni perfiles de navegador.
- Solo lectura: el único método de escritura sigue siendo `mail.categories.set` de Outlook.
- Las pruebas en vivo las ejecuta Camilo, que hace el inicio de sesión y el MFA. Ningún agente
  corre `-m live`.
- Sin push, sin PR y sin tocar otros repositorios.
- Commits: `AGENTS.md` prohíbe hacer commit sin una petición explícita. Ver D2.
- Si la decisión aceptada cambia, primero se corrige la ADR y después se escribe código
  (`AGENTS.md`, sección «Mandatory platform decision»).

## Decisiones

| ID | Decisión | Estado |
| --- | --- | --- |
| D0 | Se amplía el respaldo por navegador a Google y Jira; la ruta oficial sigue siendo la primaria. | **Aceptada**: Camilo, 2026-10-25 |
| D0.1 | `admin_policy_enforced` de Google y el token de Jira bloqueado por política se tratan como «permiso de la organización no disponible», es decir, son razones válidas para usar el respaldo. Es el mismo trato que hoy recibe `ADMIN_CONSENT_REQUIRED` en Outlook. | Se deriva de D0 y queda registrada en la ADR |
| D1 | Alcance del navegador en Google frente a `SECURITY.md:80-82` («Do not call retired or undocumented provider endpoints»). La ADR registra una excepción solo para Google y solo de lectura, que cubre tres mecanismos: la exportación ICS de Calendar, la exportación a texto de Docs para Meet, y Gmail por el DOM de la búsqueda más la descarga del mensaje original. Los selectores son versionados y deterministas. Si un selector cambia, se falla de forma cerrada con `BROWSER_SELECTOR_MISMATCH`. | **Aceptada**: Camilo, 2026-10-25, «Todo, incluido Gmail» |
| D2 | Un commit convencional por tarea en esta rama: en español, con scope `solercia` y sin atribución a IA. Cada rebanada de unas 400 líneas se anota en el documento, para encadenar más adelante los PRs sobre la rama de la feature (`feature-branch-chain`). Sin push ni PR. | **Aceptada**: Camilo, 2026-10-25 |

### Evidencia de que la ruta oficial funciona (AUTOMATINFORMES)

- **Gmail:** AUTOMATINFORMES usa un cliente OAuth propio de Google Cloud
  (`~/.gmail-mcp/gcp-oauth.keys.json`) con tokens de IDARTES, IDT y pandora-idt.
  - Las credenciales son del 2026-02-28 y los tokens se renovaron el 2026-09-19 y el 2026-10-18.
  - Por tanto, el token no vence a los 7 días y los administradores de IDT e IDARTES no bloquean la
    app.
- **Google Calendar:** usa el mismo esquema (`~/.google-calendar-mcp/`).
- **Jira:** usa `Authorization: Basic` con API token contra `/rest/api/3/search/jql`
  (`AUTOMATINFORMES/agents/skills/extraer-issues-jira/scripts/jira_extract.py:84,102`).
- **Consecuencia:** para las cuentas actuales, la ruta oficial es suficiente. El navegador se
  construye como red de seguridad, por decisión de Camilo del 2026-10-25 («Plan completo»).
- **Para la validación en vivo:** se puede reutilizar el mismo cliente OAuth de Google en
  `SOLERCIA_CONNECT_GOOGLE_CLIENT_ID`. Camilo lo configura y ningún agente lee ese archivo.

### Orden de ejecución

Lo oficial va primero:

1. T0.1
2. T4.1
3. T2.1 a T2.4
4. T4.2 y T4.3
5. T1.1 a T1.3
6. T3.1 y T3.2
7. T5.0 a T5.4
8. T6.1 y T6.2

Los identificadores de las tareas no cambian.

## Modo TDD

- **Modo:** estricto, activo.
- **Fuente:** `openspec/config.yaml:11` y `openspec/config.yaml:31-33` (`tdd: true`).
- **Runner:** `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`
  (`openspec/config.yaml:34`).
- **Compuertas:**
  - `ruff format --check .`
  - `ruff check .`
  - `mypy src`
  - cobertura de ramas ≥ 80 (`openspec/config.yaml:37-38`)

## RDD

`gentle-ai review mode status`: apagado (decidido por la configuración global). Rigen la política
ordinaria del repositorio y las compuertas funcionales.

## Proyección de entrega

Son alrededor de 2.500 líneas de autoría (adiciones más eliminaciones), sin contar archivos
generados. Supera el presupuesto de 400 líneas, así que la estrategia de cadena se decide en D2.

## Tareas

### Fase 0 — Decisión

- [x] **T0.1** Escribir la ADR `docs/decisions/2026-10-25-fallback-navegador-google-jira.md` y
  corregir los documentos que hoy dicen «solo Microsoft»:
  - el registro de 2026-08-23: §3.2, §3.6, §3.8, §4.8 y §5.6
  - `SECURITY.md:65-82`
  - `AGENTS.md:37`
  - `docs/ARCHITECTURE.md:21` y `:107-112`
  - `openspec/config.yaml:8`, `:15` y `:18`
  - `agents/skills/solercia-connect/SKILL.md`

  Incluye la excepción de D1 en `SECURITY.md`.

### Fase 1 — Base de navegador genérica, sin cambiar el comportamiento de Microsoft

- [ ] **T1.1** Separar el código genérico del código de Microsoft.
  - Agregar a `ports.py` un puerto de navegador neutral.
  - Extraer un núcleo Playwright común: perfil, observador de solicitudes, `_rest_get` y bearer
    transitorio.
  - Separarlo del driver y del adaptador de Microsoft.
  - Las pruebas de Microsoft deben seguir en verde sin cambiar su contrato.
- [ ] **T1.2** Raíz de perfil por familia.
  - Usar `data_dir/m365`, `data_dir/google` y `data_dir/jira`, con variables de entorno opcionales.
  - En el registro, elegir el adaptador por la familia del destino y no por el primero que coincida.
- [ ] **T1.3** Ajustar `RoutePolicy` y las razones del respaldo.
  - Agregar las familias GOOGLE y JIRA, y la capacidad `external.issues.search`.
  - Dejar una sola fuente para las razones que habilitan el respaldo: hoy `connections.py`,
    `runtime.py` y `ARCHITECTURE.md` no coinciden.
  - Corregir `test_security.py` para que revise todos los paquetes de navegador.

### Fase 2 — Jira oficial con cualquier cuenta

- [x] **T2.1** Conexión de Jira por cuenta.
  - El sitio se guarda en `ConnectionProfile` como dato no secreto y el API token en keyring, con
    la clave `jira:<email>`.
  - `connect jira --site … --account …` pide el token sin mostrarlo.
  - La identidad se prueba con el `accountId` de `/myself` ligado al sitio. Si `emailAddress` es
    visible, debe coincidir con la cuenta.
  - Las variables `JIRA_*` siguen funcionando.
- [ ] **T2.2** Admitir tokens con alcance, que van contra
  `api.atlassian.com/ex/jira/{cloudId}`.
- [ ] **T2.3** Atlassian OAuth (3LO) con la app propia del usuario (`client_id` y
  `client_secret`).
  - Pedir `offline_access` y usar `accessible-resources` para obtener el `cloudId`.
  - Rotar el refresh token y tratar el `invalid_grant` con error tipado.
- [ ] **T2.4** Clasificar los fallos de Jira: token bloqueado por política, 401 y 403 se tratan
  según D0.1.
  - **Bloqueada por evidencia.** Atlassian no documenta qué código devuelve un token bloqueado por
    la organización.
  - Mientras no se pueda distinguir de un token inválido, el 401 sigue siendo
    `INVALID_CREDENTIALS` y falla de forma cerrada, como exige `AGENTS.md`.
  - Se clasifica cuando T3.2 aporte la respuesta real.

### Fase 3 — Jira por navegador

- [ ] **T3.1** Crear el adaptador `jira_browser`.
  - El usuario inicia sesión de forma supervisada en el sitio.
  - La identidad se prueba con `/myself.accountId` igual a la cabecera `X-AAccountId`, ligada al
    sitio.
  - Las lecturas usan `GET /rest/api/3/search/jql`, un endpoint documentado, con la sesión.
  - Conectar CLI, servicio, doctor y matriz.
- [ ] **T3.2** Validación en vivo con Camilo: inicio de sesión, identidad y lectura.

### Fase 4 — Google oficial

- [x] **T4.1** Corregir `connect google-meet` agregando la rama de identidad que falta. La prueba
  se escribe primero, en rojo.
- [ ] **T4.2** Clasificar los fallos de Google: `admin_policy_enforced` y `org_internal` en el
  token, y el 403 de API, pasan a razones tipadas según D0.1.
- [ ] **T4.3** Actualizar el runbook: con la app propia publicada en producción para uso personal,
  sin verificar, la autorización no vence a los 7 días. Los alcances de los grants existentes no
  se tocan.

### Fase 5 — Google por navegador (alcance completo según D1)

- [ ] **T5.0** Compuerta en vivo: Camilo inicia sesión en Google dentro del perfil dedicado de
  Playwright. Si Google bloquea el navegador («This browser or app may not be secure»), la fase se
  detiene.
- [ ] **T5.1** Prueba de identidad de la sesión de Google, validada en vivo.
- [ ] **T5.2** Leer `calendar.events.list` por la exportación ICS.
- [ ] **T5.3** Leer `meeting.transcripts.*` exportando el documento a texto (ya probado en vivo el
  2026-09-02).
- [ ] **T5.4** Leer Gmail `mail.sent.list` por navegador.
  - El listado sale del DOM de la búsqueda `in:sent after: before:`.
  - El cuerpo completo sale de la descarga del mensaje original (RFC 822).
  - Los selectores son versionados y tienen fixtures saneados.

### Fase 6 — Cierre

- [ ] **T6.1** Documentación:
  - README: tablas de rutas, preparación y entorno
  - `matrix.py`
  - runbooks: crear `jira.md` y actualizar los de Gmail, Calendar y Meet
  - doctor por familia
- [ ] **T6.2** Pasar todas las compuertas y verificar que la cobertura sea ≥ 80.

## Criterios de aceptación

- Con la ruta oficial disponible, Google y Jira la usan siempre. El navegador solo entra con
  `--allow-browser-fallback` en la invocación y con `SOLERCIA_CONNECT_BROWSER_FALLBACK` activo.
- Jira se conecta a cualquier sitio y cualquier cuenta, y cada cuenta tiene su propia conexión.
- Una identidad que no coincide termina siempre en un fallo tipado y nunca devuelve datos de otra
  cuenta.
- Ningún token, cookie ni cabecera de autorización queda en logs, salidas o archivos.
- Outlook, Teams y OneDrive se comportan igual que antes: sus pruebas siguen en verde.

## Progreso y evidencia

| Tarea | Estado | Evidencia |
| --- | --- | --- |
| Exploración | Hecha | Mapa del código y evidencia externa, 2026-10-25 |
| D1 | Decidida | Respuesta de Camilo, 2026-10-25 |
| D2 | Decidida | Respuesta de Camilo, 2026-10-25 |
| Alcance | Confirmado: «Plan completo» | Respuesta de Camilo, 2026-10-25, después de revisar la evidencia de AUTOMATINFORMES |
| T0.1 | Hecha | Commit `9a186db`. Ver el detalle debajo de la tabla. |

Detalle de T0.1:

- **Pruebas:** `pytest -m 'not live'` dio 536 aprobadas y 4 excluidas. `ruff format --check` y
  `ruff check` pasaron.
- **Riesgo RDD:** pasivo, porque el cambio es solo de documentación y RDD está apagado.
- **Criterio aplicado:** `SECURITY.md:39` se limitó a las políticas de tenant de Microsoft para que
  no contradiga D0.1.

Detalle de T4.1:

- **Rojo:** dos pruebas nuevas en `tests/test_connections.py`. El resultado fue `failed` en lugar
  de `connected_official`, y `capability_not_implemented` en lugar de
  `authorization_boundary_violation`.
- **Verde:**
  - Se agregó la rama `GOOGLE_MEET` en `_verify_identity`.
  - Se agregó el puerto `transport` inyectable en `ConnectionService`. El factory OAuth ahora usa
    el tipo `HTTPTransport`.
- **Compuertas:**
  - pruebas: 538 aprobadas, 4 excluidas
  - cobertura: 83,25 %
  - `ruff format --check` y `ruff check`: limpios
  - `mypy src`: limpio
- **Riesgo RDD:** apagado, no aplica.

El commit es `a982184`.

Detalle de T2.1 (commit `d76cdbd`):

- **Rojo:**
  - Al cargar las pruebas: `ImportError: normalize_jira_site`.
  - Después fallaron 23 pruebas por `secret_store` inexistente.
  - El servicio respondía `invalid_input` donde se esperaba `configuration_error`.
- **Verde:**
  - Se creó `secret_store.py`, que guarda el token en keyring bajo el servicio
    `solercia-connect.api-token` y la clave `jira:<email>`.
  - El perfil ganó los campos opcionales `site` y `provider_account_id`. Los perfiles v1 se siguen
    cargando y el esquema no cambió.
  - Se agregó `connect_jira` con identidad por `/myself` y reversión si algo falla.
  - El CLI admite `--site`, prompt oculto y `--token-stdin` (que rechaza una terminal interactiva).
  - El servicio HTTP rechaza tokens de Jira con 422.
  - `bootstrap` construye Jira por cuenta y nunca cae a las variables de entorno de otra cuenta.
- **Prueba existente ajustada:**
  `test_connect_reports_non_oauth_targets_and_missing_configuration`. Jira ahora espera
  `configuration_invalid` y GLPI sigue sin cambio.
- **Compuertas:**
  - pruebas: 566 aprobadas
  - cobertura: 83,88 %
  - `ruff` y `mypy`: limpios (verificado por el orquestador)
- **Tamaño:** 1.055 líneas, de las cuales unas 567 son pruebas. Supera la heurística de 400 porque
  la tarea toca config, almacén, servicio, bootstrap, CLI y HTTP, cada uno con sus pruebas.
- **Brechas:**
  - Un mismo correo solo puede conectarse a un sitio. El API token de Atlassian sirve en todos los
    sitios de la cuenta, así que se puede ampliar si hace falta.
  - `doctor` sigue pidiendo `JIRA_*`; lo corrige T6.1.
  - Un perfil de Jira con ruta de navegador falla de forma cerrada hasta T3.1.

### Rebanadas de entrega (`feature-branch-chain`)

| Rebanada | Commits | Líneas | Contenido |
| --- | --- | --- | --- |
| R1 | `9a186db` | 640 | ADR y documentos |
| R2 | `a982184`, `d76cdbd` | ~1.110 | Meet y Jira por cuenta |

## Seguimiento fuera de alcance

- `openspec/config.yaml:31-34` (`rules.apply`) mezcla elementos de lista con claves `tdd` y
  `test_command`, así que el YAML no se puede parsear.
- El problema ya existía antes de este cambio (ver `HEAD~1`).
- Necesita una corrección propia. No se corrige aquí sin autorización.

## Siguiente paso

Siguen T2.2 (tokens con alcance) y T4.2 (clasificar `admin_policy_enforced`, `org_internal` y los
403 de política de Google). Después va T2.3 (Atlassian OAuth 3LO).
