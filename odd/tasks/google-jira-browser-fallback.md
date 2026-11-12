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
| D3 | **Modo proyecto.** Se activa con `SOLERCIA_CONNECT_HOME=<proyecto>/.solercia-connect` y concentra en esa carpeta la configuración, los perfiles, las sesiones de navegador, los secretos en archivo y `connect.env`. Protecciones: carpeta 0700, archivos 0600 con escritura atómica, `.gitignore` automático con `*`, y fallo cerrado si git rastrea algo dentro. Sin `SOLERCIA_CONNECT_HOME` todo sigue igual (XDG y llavero), así que Solercia Command y los demás consumidores no cambian. | **Aceptada**: Camilo, 2026-10-25, «Archivo en el proyecto». Caso de uso: agente `migracion-angular-pandora` en `IDARTES/pandora_proxy`, con Gmail y Jira |
| D4 | Escritura acotada en Jira: transición de estado de un issue por invocación, con `--allow-write`, solo por la ruta oficial, sin HTTP ni reintentos y con verificación posterior. Es la segunda escritura del producto, después de las categorías de Outlook. | **Aceptada**: Camilo, 2026-10-25, «ADR de transición» |
| D5 | **Envío de correo en Gmail dentro de `solercia-connect`, en dos pasos:** primero un borrador revisable y después el envío con aprobación y `--allow-write`. Usa una conexión separada (`gmail-compose`) y verifica duplicados antes de enviar. Es la tercera escritura del producto. | **Aceptada**: Camilo, 2026-10-25, «Dentro de solercia-connect». El orquestador decidió que la conexión fuera separada, por mínimo privilegio |
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
6. T7.0 a T7.5 (modo proyecto, agregado el 2026-10-25)
7. T3.1 y T3.2
8. T5.0 a T5.4
9. T6.1 y T6.2

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

- [x] **T1.1** Separar el código genérico del código de Microsoft.
  - Agregar a `ports.py` un puerto de navegador neutral.
  - Extraer un núcleo Playwright común: perfil, observador de solicitudes, `_rest_get` y bearer
    transitorio.
  - Separarlo del driver y del adaptador de Microsoft.
  - Las pruebas de Microsoft deben seguir en verde sin cambiar su contrato.
- [x] **T1.2** Raíz de perfil por familia.
  - Usar `data_dir/m365`, `data_dir/google` y `data_dir/jira`, con variables de entorno opcionales.
  - En el registro, elegir el adaptador por la familia del destino y no por el primero que coincida.
- [x] **T1.3** Ajustar `RoutePolicy` y las razones del respaldo.
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
- [x] **T2.2** Admitir tokens con alcance, que van contra
  `api.atlassian.com/ex/jira/{cloudId}`.
- [x] **T2.3** Atlassian OAuth (3LO) con la app propia del usuario (`client_id` y
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
- [x] **T4.2** Clasificar los fallos de Google: `admin_policy_enforced` y `org_internal` en el
  token, y el 403 de API, pasan a razones tipadas según D0.1.
- [x] **T4.3** Actualizar el runbook: con la app propia publicada en producción para uso personal,
  sin verificar, la autorización no vence a los 7 días. Los alcances de los grants existentes no
  se tocan.
  - Si el callback de `connect` de Google expira, el mensaje de corrección debe explicar que Google
    muestra `admin_policy_enforced` u `org_internal` en su propia página y no redirige.
  - El mismo mensaje debe orientar hacia la ruta de navegador cuando esa ruta exista. Este punto
    viene de lo que se encontró en T4.2.

### Fase 5 — Google por navegador (alcance completo según D1)

- [ ] **T5.0** Compuerta en vivo: Camilo inicia sesión en Google dentro del perfil dedicado de
  Playwright. Si Google bloquea el navegador («This browser or app may not be secure»), la fase se
  detiene.
- [ ] **T5.1** Prueba de identidad de la sesión de Google, validada en vivo.
  - Sonda implementada el 2026-10-26 (commit `00e8e36`): botón de cuenta de la barra de Google en
    Calendar web. Falta la validación en vivo.
- [ ] **T5.2** Leer `calendar.events.list` por la exportación ICS.
- [x] **T5.3** Leer `meeting.transcripts.*` por la página, según la precisión de D1 del
  2026-10-26 en la ADR (implementada offline; ver «Detalle de T5.3»):
  - descubrir por la vista diaria de Calendar web y el detalle de cada evento, tomando solo el
    adjunto de transcripción;
  - leer el texto visible de la vista `mobilebasic` del documento;
  - sin API internas, sin cookies y sin URL firmadas.
- [ ] **T5.4** Leer Gmail `mail.sent.list` por navegador.
  - El listado sale del DOM de la búsqueda `in:sent after: before:`.
  - El cuerpo completo sale de la descarga del mensaje original (RFC 822).
  - Los selectores son versionados y tienen fixtures saneados.

### Fase 7 — Modo proyecto (D3)

Pedido de Camilo, 2026-10-25: «que esas claves […] se pueda llamar desde la variable de entorno
para que no haya que ponerle las claves a cada rato […] y que esos archivos de callback y de sesión
[…] se guarden […] dentro del proyecto».

- [x] **T7.0** Escribir la ADR del modo proyecto.
  - Corregir `SECURITY.md` y el docstring de `oauth.py:66`: el llavero sigue siendo el valor por
    defecto, y el almacén en archivo solo se usa con `SOLERCIA_CONNECT_HOME`.
  - Registrar los riesgos: los tokens quedan en texto plano en disco y los puede leer cualquier
    agente con acceso de lectura a la carpeta.
- [x] **T7.1** Implementar `SOLERCIA_CONNECT_HOME`.
  - Rutas: `config/`, `data/` (con los perfiles de navegador por familia) y `secrets/`.
  - Protecciones:
    - carpetas en 0700, comprobación de dueño y sin enlaces simbólicos
    - `.gitignore` con `*`
    - fallo cerrado si `git ls-files` muestra algo rastreado dentro
  - Sin la variable, el comportamiento actual no cambia.
- [x] **T7.2** Crear el almacén de secretos en archivo.
  - Implementa el protocolo `KeyringBackend`, de modo que `KeyringOAuthTokenStore` y
    `KeyringSecretStore` lo usen sin cambios.
  - Un archivo por clave, con nombre derivado por hash, permisos 0600 y escritura atómica.
- [x] **T7.3** Leer las credenciales desde el entorno del proyecto.
  - Cargar `<home>/connect.env` cuando exista. Precedencia: entorno del proceso, luego
    `SOLERCIA_CONNECT_ENV_FILE`, luego `connect.env`.
  - `connect jira … --token-env NOMBRE` lee el token de esa variable sin mostrarlo.
- [x] **T7.4** Uso como librería y documentación.
  - Documentar el punto de entrada de Python que respeta el mismo entorno.
  - Agregar en `docs/USO-CON-AGENTES.md` la sección «Por proyecto», con el ejemplo de
    `pandora_proxy`.
  - `doctor` informa el modo proyecto.
- [ ] **T7.5** Configurar `IDARTES/pandora_proxy`: variable, `connect.env` y conexión de Gmail y
  Jira.
  - Toca otro repositorio, así que requiere autorización explícita antes de hacerlo.
  - El inicio de sesión lo hace Camilo.

Pedido de Camilo, 2026-10-25: «es mejor compartirlo con todo como un skill general […] quisiera
que eso ya viniera como un skill y todo se hiciera transparente». Decisión D3.1, «Skill +
descubrimiento»: el lanzador por proyecto se reemplaza por el CLI global y el skill global.

- [x] **T7.6** Descubrimiento de la carpeta del proyecto y comando `project init`.
  - Si `SOLERCIA_CONNECT_HOME` no está definida y `SOLERCIA_CONNECT_PROJECT_DISCOVERY=1`, el CLI
    busca la raíz de git desde el directorio actual.
  - Si existe `<raíz>/.solercia-connect/`, entra en modo proyecto. Si no, usa el modo global.
  - El descubrimiento lo resuelve código determinista, nunca el modelo, y aplica las mismas
    protecciones del modo proyecto.
  - `solercia-connect project init` crea la carpeta, las protecciones y la plantilla de
    `connect.env`.
  - Hay que actualizar la ADR del modo proyecto y la promesa del README sobre `.env` en el
    directorio actual.
- [x] **T7.7** Skill global.
  - `agents/skills/solercia-connect/SKILL.md` siempre invoca `solercia-connect`, sin rutas.
  - El modo proyecto es transparente.
  - Ante `action_required`, el agente se detiene y pide el `connect`.
  - El agente nunca lee `connect.env`, `secrets/` ni `data/`.
- [x] **T7.8** Retirar el lanzador de `pandora_proxy`. Esta tarea depende de que la herramienta
  global tenga este código; esa decisión de entrega la toma Camilo.

### Fase 8 — Transición de estado en Jira (D4)

Pedido de Camilo, 2026-10-25: «mi idea es conectarme con Jira en pandora_proxy e ir haciendo el
cambio de estado de las historias que tengo acceso por api». Decisión D4, «ADR de transición»,
aceptada ese mismo día. Contexto: sitio `idartes-tecnologia.atlassian.net`, proyecto `CP`.

- [x] **T8.0** Escribir la ADR de escritura de transiciones en Jira.
  - Sigue el molde de `2026-10-19-escritura-categorias-outlook.md`.
  - Actualizar `AGENTS.md`, `SECURITY.md` y el skill, para que la segunda escritura permitida sea
    la transición de estado de un issue.
- [x] **T8.1** Lectura `external.issues.transitions.list --issue <clave>`.
  - Devuelve las transiciones disponibles con su estado destino y si exigen campos.
- [x] **T8.2** Escritura `external.issues.transition --issue <clave> --to <estado> --allow-write`.
  - Solo un issue y solo su estado.
  - Exige coincidencia exacta de una sola transición. Si coinciden cero o varias, falla de forma
    cerrada.
  - Si la transición exige campos, falla de forma cerrada.
  - Usa solo la ruta oficial, nunca por HTTP y sin reintentos.
  - Lee el estado antes y después, y deja recibo de ambos.
  - Con OAuth falla de forma cerrada mientras el grant no incluya `write:jira-work`.

Detalle de la Fase 8 (commits `1f42e9d`, `b6a043e` y `b0a413e`):

- **T8.0:** se escribió la ADR `2026-10-25-escritura-transiciones-jira.md`. Se actualizaron
  `AGENTS.md`, `SECURITY.md`, el skill y el §3.9 del registro de plataforma.
- **T8.1:** se agregó `external.issues.transitions.list --issue`.
  - Rojo: 28 fallos.
  - Un 404 devuelve `issue_not_found`.
  - Si la clave pedida resuelve a otro issue, falla de forma cerrada.
- **T8.2:** se agregó `external.issues.transition --issue --to --allow-write`.
  - Rojo: 42 fallos.
  - Errores tipados: `transition_unavailable`, `transition_ambiguous`,
    `transition_requires_fields`, `write_scope_missing` y `write_unverified`. Un 409 devuelve
    `write_conflict`.
  - Se probó desactivando la exclusión de reintentos y la protección de `to`.
- **Pruebas existentes:**
  - `test_cli_lifecycle.py` recorre todas las capacidades. Se le agregaron 6 líneas (importación,
    entrada, `issue` y una omisión para la escritura). No se quitó nada.
  - El orquestador lo acepta como extensión.
- **Cambio transversal aceptado:** el `error.message` de un `run` fallido ahora sale del paso que
  produjo la razón final, no del último paso. Antes, toda falla oficial sin respaldo decía
  «Browser fallback was not explicitly enabled».
- **Compuertas:** 1.069 pruebas (verificado por el orquestador), cobertura 87,42 %.
- **Dato de uso:** `--provider` recibe el identificador del proveedor (`jira.cloud`,
  `google.gmail`), no el destino. El `LEEME.md` de `pandora_proxy` se corrigió.

Corrección en vivo `e72fb18` (verificada en `pandora_proxy`):

- **Defecto:** `status gmail` fallaba porque Google guarda `email` como
  `https://www.googleapis.com/auth/userinfo.email`.
  - `grant_covers()` ahora reconoce los alias de `email` y `profile`.
  - Rojo: 3 fallos.
  - Verde: 1.073 pruebas, cobertura 87,42 %.
- **Riesgo:** el token guardado traía `gmail.modify`, `calendar`, `drive.readonly` y
  `gmail.settings.basic`, heredados por `include_granted_scopes=true` del cliente compartido con
  AUTOMATINFORMES. Se eliminó ese parámetro. Camilo debe volver a ejecutar `connect gmail` para
  quedar con un token de solo lectura.
- **Reinstalación global:** se reinstaló la herramienta desde la rama, con la Fase 8 y esta
  corrección, y se refrescaron las copias del skill.
- **Validación en vivo:**
  - `status jira`: `connected_official`, token clásico, `idartes-tecnologia`.
  - `status gmail`: `connected_official`.
  - `transitions.list CP-1356`: el estado actual es «Tareas por hacer» y los destinos son «Tareas por
    hacer», «En curso», «Finalizada» (transición «Listo»), «impediment» y «Por Desplegar».
  - Todavía no se ha probado en vivo la escritura de una transición.
- **Aviso a la sesión `migracion-angular-pandora` (2026-10-25):**
  - le llegaron los comandos y las reglas de la ADR, y se le aclaró que Gmail es de solo lectura y
    no envía correos;
  - queda abierta la aclaración sobre la cuenta `idt.gov.co` que mencionó Camilo.

Pendientes nuevos:

- [ ] **T7.9** Mejorar los mensajes de error.
  - Si el descubrimiento está apagado y existe `.solercia-connect/` en la raíz de git, el mensaje
    debe sugerir `source ~/.zshrc`.
  - `--token-env` debe explicar dónde busca la variable.
- [x] **T8.3** Lectura de historias abiertas, por ejemplo las del sprint actual asignadas al
  usuario. Hoy `external.issues.search` solo devuelve issues resueltos. Requiere la decisión de
  Camilo.
  - **Delegada** a la sesión `migracion-angular-pandora` el 2026-10-26, por pedido de Camilo.
  - Trabaja en la rama `feat/jira-open-issues` desde `7c2b8fb`, con worktree propio, y no toca
    esta rama.
  - Capacidad acordada: `external.issues.open.list` con `--project`, `--sprint current` y `--jql`.
    El JQL por defecto es `assignee = currentUser() AND statusCategory != Done`.
  - Al terminar, el orquestador la integra, la verifica y reinstala la herramienta.
  - **Hecha:** commit `6422999` de la otra sesión, integrado con cherry-pick como `23e42cf`.
    - El orquestador verificó 1.288 pruebas, cobertura 88,58 %, `ruff` y `mypy` limpios.
    - Ninguna prueba existente perdió líneas y la lista de navegador sigue igual.
    - Documento propio de la tarea: `odd/tasks/jira-open-issues.md`.
- **Gmail compose:** Camilo conectó `gmail-compose` el 2026-10-26. La autorización pidió solo
  `gmail.compose` y la conexión está en `connected_official`.
- **Bitbucket:** es una feature aparte (`feat/bitbucket-connector` desde `7c2b8fb`), con worktree
  `solercia-connect-worktrees/bitbucket-connector` y workspace `idartes-tecnologia`.
- Candidato a feature nueva: integración con Bitbucket Cloud. Se evalúa aparte.

### Fase 9 — Gmail: bandeja de entrada y envío en dos pasos (D5)

Pedido de Camilo, 2026-10-25: «conectar para leer y enviar tambien correos a
cristian.calderontapia@idartes.gov.co». Decisión D5, «Dentro de solercia-connect».

Decisiones del orquestador durante la Fase 9, derivadas de D5 (2026-10-25):

- `test_inbox_mail_request_for_gmail_is_rejected_before_any_capability_dispatch` codificaba «Gmail no
  tiene bandeja de entrada». Se adapta a D5 sin perder su intención:
  - el rechazo antes del despacho se prueba con una capacidad que Gmail no ofrece;
  - una prueba nueva demuestra que la bandeja de Gmail llega al registro.
- La verificación de duplicados lee Enviados por la conexión `gmail` de solo lectura, porque
  `gmail.compose` no autoriza `messages.list`.
  - Si esa conexión falta, se falla de forma cerrada con `duplicate_check_unavailable`.
  - `--allow-duplicate` omite la verificación y requiere la aprobación de Camilo.
- Se agrega `"gmail-compose"` a la lista ordenada de `test_connection_types_are_ordered…`, una
  línea aditiva.
- `--to` pasa a ser repetible: exactamente un estado para la transición de Jira y una lista de
  destinatarios para el borrador de Gmail.

- [x] **T9.0** Escribir la ADR del envío de correo en Gmail en dos pasos (commit `16f4b8c`).
  - El agente crea un borrador, Camilo lo revisa en Gmail y solo con su aprobación se envía.
  - El envío usa una conexión separada, `gmail-compose` con `gmail.compose`, por mínimo privilegio.
    La conexión `gmail` sigue siendo de solo lectura.
  - Actualizar `AGENTS.md`, `SECURITY.md` y el skill.
- [x] **T9.1** Lectura `mail.inbox.list` para Gmail, solo por la ruta oficial con `gmail.readonly`.
  - Usa el mismo registro normalizado que la bandeja de Outlook.
- [x] **T9.2** Escritura `mail.draft.create`.
  - Crea un borrador con destinatarios, asunto y cuerpo explícitos.
  - Remitente: solo la cuenta conectada, sin alias. Sin adjuntos.
  - Requiere `--allow-write`, nunca por HTTP y sin reintentos.
- [x] **T9.3** Escritura `mail.draft.send --draft-id`.
  - Requiere `--allow-write`, nunca por HTTP y sin reintentos.
  - Antes de enviar busca en Enviados un correo con el mismo asunto y los mismos destinatarios. Si
    lo encuentra, falla de forma cerrada salvo que se pase `--allow-duplicate`.
  - Deja recibo con el identificador del mensaje enviado.

Detalle de la Fase 9 (commits `16f4b8c`, `20d954c`, `560a5dc` y `89ea037`):

- **T9.1:** bandeja de entrada de Gmail con `in:inbox` y `labelIds=INBOX`, más `messages.get full`.
  - Usa el mismo registro normalizado que Outlook.
  - Rojo: 12 fallos, más la prueba nueva que no pasaba con el `config.py` de `HEAD`.
- **T9.2:** se agregaron el destino `gmail-compose` (alcance `gmail.compose`) y `mail.draft.create`.
  - Límites: 20 destinatarios, cuerpo de 200 KB y asunto de 255 caracteres.
  - Las direcciones pasan por un control que no deja romper los encabezados.
  - Un POST sin confirmar devuelve `write_unverified`.
  - Rojo: `ImportError: GMAIL_COMPOSE_SCOPE`.
- **T9.3:** se agregó `mail.draft.send --draft-id`.
  - La verificación de duplicados usa la conexión `gmail` de lectura y revisa como máximo 100
    candidatos. Si no puede verificar, falla de forma cerrada.
  - Códigos nuevos: `duplicate_suspected` y `duplicate_check_unavailable`.
  - Rojo: 47 fallos.
- **Pruebas existentes:**
  - Solo los cambios autorizados.
  - En `test_application` se adaptó la prueba de la bandeja.
  - En `test_cli_lifecycle` el conteo pasó a `== len(TARGET_SPECS)`, se reorganizó la importación y
    se agregaron líneas en el ciclo.
  - En `test_standalone_foundation` se agregó una línea.
- **Compuertas:** 1.212 pruebas (verificado por el orquestador), cobertura 88,28 %, `ruff` y `mypy`
  limpios.
- **Brechas:**
  - Todo está `implemented-offline`.
  - `gmail.compose` es un alcance restringido; puede que haya que declararlo en la pantalla de
    consentimiento del cliente propio.
  - No se verificó si Gmail conserva el encabezado Bcc en Enviados.
  - Un 403 por falta de alcance no se refina a `write_scope_missing`. Lo cubre la revisión del
    grant guardado antes de cada llamada.

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

Detalle de T2.2 (commit `5ccef97`):

- **Rojo:**
  - Al cargar las pruebas: `ImportError: JiraTokenKind`.
  - Después fallaron 15 pruebas: `connect_jira` no aceptaba `token_kind`, el CLI no tenía
    `--token-kind` y la prueba de perfil antiguo daba `KeyError: 'token_kind'`.
- **Verde:**
  - `--token-kind classic|scoped` explícito, sin detección automática.
  - `--cloud-id` debe ser un UUID; si no se da, el `cloudId` se obtiene de
    `https://<sitio>.atlassian.net/_edge/tenant_info` sin enviar el token. Fuente:
    support.atlassian.com/jira/kb/retrieve-my-atlassian-sites-cloud-id.
  - Con token con alcance, la base es `api.atlassian.com/ex/jira/{cloudId}`.
  - Los perfiles antiguos se tratan como `classic`.
- **Compuertas:** 582 pruebas, cobertura 84,28 %, `ruff` y `mypy` limpios.
- **Tamaño:** 528 líneas.

Detalle de T4.2 (commit `6c4bd6c`):

- **Rojo:** 8 fallos.
  - En el intercambio y la renovación del token se esperaba `ADMIN_CONSENT_REQUIRED` y llegaba
    `REQUEST_REJECTED`.
  - En los 403 de Gmail, Calendar, Meet-Calendar y Meet-Drive se esperaba `ADMIN_CONSENT_REQUIRED`
    y llegaba `AUTHORIZATION_DENIED`.
- **Verde:**
  - `admin_policy_enforced` y `org_internal` pasan a `ADMIN_CONSENT_REQUIRED`, solo en
    `GoogleOAuthProvider`.
  - Un 403 pasa a `ADMIN_CONSENT_REQUIRED` solo cuando todas sus razones son `domainPolicy`, una
    razón documentada en las páginas de errores de Gmail y Drive.
  - Se agregó el gancho `failure_refiner` en el transporte, que lee la razón estructurada sin
    registrar el cuerpo.
  - Microsoft no cambia: sus archivos no tienen diferencias.
- **Se mantienen igual:** `invalid_grant` sigue como `INVALID_CREDENTIALS` y `access_denied` como
  `AUTHORIZATION_DENIED`.
- **Compuertas:** 658 pruebas (verificado por el orquestador), cobertura 84,46 %, `ruff` y `mypy`
  limpios.
- **Tamaño:** 377 líneas.
- **Brechas:**
  - Calendar no documenta `domainPolicy`; si nunca lo devuelve, el mapeo no tiene efecto.
  - Un `--cloud-id` explícito no se contrasta con el sitio, aunque la identidad por `/myself` sigue
    aplicando.

Detalle de T2.3 (commit `675e80e`):

- **Rojo:**
  - Al cargar las pruebas: `ImportError: atlassian_oauth_provider_from_settings`.
  - `LoopbackCallbackServer` no aceptaba `port`.
  - Durante el verde, dos pruebas de estado fallaron porque se leía el reloj real. Se corrigió
    inyectando el reloj del servicio.
- **Verde:**
  - Se agregó `AtlassianOAuthProvider`, que usa `client_secret` y no envía PKCE.
  - El refresh token rota en cada renovación. Si no llega uno nuevo, el resultado es
    `INVALID_CREDENTIALS` y se conserva el anterior.
  - El sitio se elige por coincidencia exacta en `accessible-resources`. Si no aparece o aparece
    más de una vez, el resultado es `AUTHORIZATION_BOUNDARY_VIOLATION`.
  - La identidad se prueba con `/me` y `/myself`, y exige un correo coincidente.
  - El CLI usa `--token-kind oauth` y no pide token. Ejecutar usa Bearer contra
    `api.atlassian.com/ex/jira/{cloudId}`.
- **Configuración:** `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID`,
  `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET` y `SOLERCIA_CONNECT_ATLASSIAN_REDIRECT_URI`. El valor
  por defecto de la redirección es `http://127.0.0.1:8766/oauth/callback`, y es la URL que se
  registra en la consola de Atlassian.
- **Pruebas existentes ajustadas:** tres pruebas usaban `"oauth"` como valor inválido y ahora usan
  `"bearer"`. El caso de `disconnect` ahora usa un almacén de OAuth respaldado por keyring.
- **Compuertas:** 688 pruebas, cobertura 84,95 %, `ruff` y `mypy` limpios.
- **Tamaño:** 1.569 líneas. La tarea abarca proveedor, configuración, servicio, CLI y bootstrap, cada
  uno con sus pruebas.
- **Brechas:**
  - La documentación no confirma que Atlassian acepte un callback de loopback; queda pendiente la
    validación en vivo.
  - Si una cuenta cambia de token a OAuth, la credencial anterior queda guardada hasta
    `disconnect`.
  - La corrección del `run` por HTTP no agrega `--token-kind oauth` (T6.1).
  - `doctor` no reporta `SOLERCIA_CONNECT_ATLASSIAN_*` (T6.1).

Detalle de T4.3 (commit `8fa6b87`):

- **Rojo:** 3 fallos, uno por cada destino de Google, porque faltaba la línea de corrección cuando
  expira el callback.
- **Verde:**
  - Se agregó la línea que explica `admin_policy_enforced` y `org_internal` y orienta hacia la ruta
    de navegador. Solo aplica a Google; el código de razón no cambia.
  - Los runbooks de Gmail, Calendar y Meet ahora explican la app propia en producción, los límites
    del estado de prueba y el bloqueo del administrador, con las citas separadas del análisis.
- **Compuertas:** 692 pruebas (verificado por el orquestador), cobertura 84,96 %, `ruff` y `mypy`
  limpios.

Detalle de T1.1 (commit `43acc56`):

- **Rojo:** al cargar las pruebas, `ImportError: BrowserProviderAdapter`.
- **Verde:**
  - Se creó el paquete `providers/browser_core/` con `states.py`, `session.py` (`PlaywrightSession`)
    y `login.py` (`poll_until_ready`).
  - Microsoft importa el núcleo de vuelta, con alias para los nombres anteriores.
  - Se agregó el puerto `BrowserProviderAdapter`.
- **Pruebas existentes modificadas:** ninguna.
- **Compuertas:** 751 pruebas, cobertura 85,77 %.
- **Cambio visible:** el `repr` del bearer pasó de `_TransientBearer` a `TransientBearer`, y sigue
  redactado.

Detalle de T1.2 (commit `1d31b86`):

- **Rojo:**
  - Google recibía `microsoft365.browser`, porque se elegía el primero que coincidía.
  - Microsoft recibía `google.browser` si Google se registraba antes.
- **Verde:**
  - Se agregó `browser_profile_path` por familia. La ruta de Microsoft quedó fijada a la fórmula
    anterior.
  - El perfil de Jira se identifica por el sitio más la cuenta.
  - La familia sale de una tabla fija, `official_provider_family`. Si no se puede saber, no se
    elige ningún adaptador y se falla de forma cerrada.
- **Pruebas existentes modificadas:** ninguna.
- **Compuertas:** 782 pruebas, cobertura 85,93 %.

Decisión de T1.3, tomada por el orquestador el 2026-10-25 a partir de D0:

- **Lista de capacidades por familia:**
  - Google: `mail.sent.list`, `calendar.events.list` y `meeting.transcripts.*`.
  - Jira: `external.issues.search`.
  - Microsoft: sin cambios.
- **Por qué no una lista única:** permitiría combinaciones que la ADR no aprobó, como Jira con
  `mail.categories.set`.
- **Prueba adaptada:** `test_browser_fallback_is_denied_for_non_microsoft_family` codificaba el §3.2
  anterior y se adapta a D0.
  - Pasa a llamarse `test_browser_fallback_is_denied_outside_the_family_allowlist`.
  - Conserva su intención con dos casos de rechazo: GLPI, y Google con una capacidad fuera de su
    lista.

Detalle de T1.3 (commit `88955a3`):

- **Rojo:** 9 fallos contra la lista única.
  - Jira con `mail.categories.set` y Google con `mail.inbox.list` quedaban permitidos.
  - Microsoft con `external.issues.search` también quedaba permitido.
  - No existía el atributo por familia.
- **Verde:**
  - La tabla `BROWSER_FALLBACK_CAPABILITIES` tiene una lista por familia.
  - Las constantes `RUNTIME_FALLBACK_REASONS` y `CONNECT_FALLBACK_REASONS` son la única fuente de
    las razones.
  - `browser_routes.py` centraliza las compuertas de superficie.
  - El escaneo de seguridad cubre todos los paquetes de navegador.
- **Pruebas existentes:**
  - Solo se adaptó la autorizada, con 28 líneas agregadas y 12 eliminadas.
  - `test_inbox_mail_is_allowlisted_for_the_microsoft_browser_fallback` pasa sin cambios.
- **Compuertas:** 861 pruebas (verificado por el orquestador), cobertura 86,06 %, `ruff` y `mypy`
  limpios.
- **Partes de Microsoft que se dejan para T3, T5 y T6.1:**
  - textos de auditoría y de rechazo que mencionan a Microsoft;
  - `browser_adapter_from_environment` y `_browser_adapter`, que solo construyen Microsoft;
  - `BrowserDriver` y `BrowserSnapshot`, todavía con la forma de Microsoft;
  - el check de doctor `microsoft_browser_route`;
  - `SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR` y `SOLERCIA_CONNECT_JIRA_PROFILE_DIR`, sin documentar.

Detalle de la Fase 7 (commits `a6142b4` a `6ad07e3` y `0ab30c7`):

- **T7.0 (`a6142b4`):** se escribió la ADR `2026-10-25-modo-proyecto-secretos-en-archivo.md`.
  - `SECURITY.md` ganó la sección «Project mode».
  - En el registro de plataforma se ajustaron §4.4 y §4.7, porque eran los que decían «solo
    llavero».
  - Se corrigió el docstring de `oauth.py`.
- **T7.1 (`ab8430d`):**
  - Se creó `project_home.py`: la carpeta exige ruta absoluta, se crean `config/`, `data/` y
    `secrets/` con permisos 0700, y el `.gitignore` con `*`.
  - La verificación de git usa un ejecutor inyectable con límite de 10 s.
  - Rojo: `ImportError: ProjectHomeError`.
- **T7.2 (`4efa20c`):**
  - Se creó `FileSecretBackend`, que cumple el protocolo `KeyringBackend` y escribe archivos 0600
    de forma atómica.
  - Las fábricas `oauth_token_store_from_settings` y `api_token_store_from_settings` reemplazan
    todas las construcciones directas de los almacenes.
  - Rojo: el CLI seguía usando el llavero y la prueba falló con `FileNotFoundError`.
- **T7.3 (`96c0411`):**
  - Precedencia: el proceso gana sobre `SOLERCIA_CONNECT_ENV_FILE`, y este sobre `connect.env`.
  - `connect.env` debe tener permisos 0600 y no puede definir `SOLERCIA_CONNECT_HOME` ni
    `SOLERCIA_CONNECT_ENV_FILE`.
  - Se agregó `--token-env`.
- **T7.4 (`6ad07e3`):**
  - `doctor` reporta el modo proyecto.
  - `docs/USO-CON-AGENTES.md` tiene la sección «Por proyecto».
  - El README documenta tres variables.
  - El punto de entrada de librería es `solercia_connect.application.run_application`, que ya
    respeta el modo proyecto. No se creó una API paralela.
- **Corrección del orquestador (`0ab30c7`):** el redactor reportó que `run`, `status` y HTTP no
  preparaban la carpeta, así que una ruta de navegador podía crear `data/` con cookies sin
  `.gitignore`.
  - Ahora `registry_from_environment` prepara y verifica la carpeta en cada `run`.
  - `ConnectionService` lo hace en todas las operaciones.
  - El servicio HTTP se cierra desde el arranque si git rastrea algo dentro.
  - Rojo:
    - 6 fallos por falta del `.gitignore` y por no detectar el archivo rastreado.
    - La protección de `bootstrap` se probó desactivándola: fallaron 4 pruebas. La primera sonda
      falló porque el reemplazo no coincidió; se corrigió y se repitió.
  - Una prueba existente (`test_run_application_via_http_serves_the_browser_route_without_an_upfront_refusal`)
    impidió poner la protección en `application.py`; por eso quedó en `bootstrap`. La prueba no se
    modificó.
- **Compuertas:** 942 pruebas, cobertura 86,82 %, `ruff` y `mypy` limpios. Ninguna prueba
  existente se modificó en la Fase 7.
- **Brecha:** el `.gitignore` debe contener exactamente `*\n`; si el archivo tiene otro contenido,
  se rechaza y no se reescribe.

Detalle de T7.6 (commit `5e1c367`) y T7.7 (commit `80d4103`):

- **Rojo:**
  - Fallaron 27 pruebas nuevas: `load_settings` no aceptaba `cwd`, no existía el comando
    `project`, y `connect.env` no rechazaba la variable de descubrimiento.
  - Un clon con la carpeta rastreada terminaba con código 0.
  - Además fallaron 4 pruebas de contrato del skill.
- **Protección probada desactivándola:** el redactor verificó con grep que la edición sí se aplicó.
  Sin la verificación de git, un clon con `.solercia-connect/` rastreada cargaba un `CLIENT_ID`
  ajeno.
- **Verde:**
  - `SOLERCIA_CONNECT_PROJECT_DISCOVERY` solo se lee del proceso o de `SOLERCIA_CONNECT_ENV_FILE`,
    nunca de `connect.env`.
  - Resolución: `SOLERCIA_CONNECT_HOME` explícita, luego `<raíz git>/.solercia-connect` si es una
    carpeta real, y si no, el modo global.
  - La carpeta descubierta se verifica con git y por privacidad antes de leer `connect.env`.
  - `project init [--path]` crea la carpeta y la plantilla, y nunca sobrescribe.
  - El skill 2.1 siempre usa `solercia-connect`. Ante `action_required`, el usuario hace el
    `connect`, y el agente nunca lee `connect.env`, `secrets/` ni `data/`.
- **Pruebas existentes:** solo se agregaron 9 líneas a `tests/conftest.py`, un fixture que limpia
  la variable de descubrimiento. No se modificó ninguna prueba.
- **Compuertas:** 973 pruebas (verificado por el orquestador), cobertura 86,99 %, `ruff` y `mypy`
  limpios.
- **Brechas:**
  - Con descubrimiento, los mensajes de permisos todavía dicen
    `chmod 700 "$SOLERCIA_CONNECT_HOME"`.
  - `git ls-files` se ejecuta de 2 a 4 veces por comando.
  - Tres ramas de error no tienen pruebas.

**Estado global verificado el 2026-10-25:**

- No hay skill instalado en `~/.claude/skills`, `~/.agents/skills` ni `~/.codex/skills`.
- No hay `uv tool` instalado.
- El `solercia-connect` del PATH sale de `solercia-connect/.venv`, el checkout principal en
  `feat/http-interactive-login`.
- La transparencia depende de T7.8 y de la decisión de entrega.

**T7.8 (hecha el 2026-10-25).** Camilo eligió «Instalar desde esta rama».

- **CLI:** `uv tool install --reinstall "solercia-connect[oauth,browser,service] @ file://<worktree>"`.
  Quedó la versión 0.4.0 en `~/.local/bin`. En un shell de login nuevo, `solercia-connect` resuelve a
  esa copia. El `.venv` del checkout principal no se tocó.
- **Skill 2.1:** se copió, no se enlazó, a `~/.claude/skills/solercia-connect` y
  `~/.agents/skills/solercia-connect`. El arnés lo detecta.
- **Shell:** se agregó `export SOLERCIA_CONNECT_PROJECT_DISCOVERY=1` al final de `~/.zshrc`.
- **pandora_proxy:**
  - Se retiró `solercia-connect.sh`.
  - `LEEME.md` ahora usa `solercia-connect` directo.
  - Verificación desde un shell nuevo: `project_home_source: discovered`, `ready: true`,
    `git_guard: clean` y `gitignore: ignores_all`. git no ve nada.
- **Reversión:**
  - `uv tool uninstall solercia-connect`
  - borrar las dos copias del skill
  - quitar la línea de `~/.zshrc`
- **Cuando la rama se integre:** reinstalar desde `main` y volver a copiar el skill.

Detalle de T5.3 (commits `68ca937`, `00e8e36` y `19544f2`, más la documentación):

- **Origen:** prueba en vivo del 2026-10-26 hecha por el orquestador con la cuenta de Workspace de
  IDARTES en un perfil de Chrome con la sesión iniciada por el usuario. La exportación a texto
  falla por CORS desde la página; la vista `mobilebasic` sí entrega el texto.
- **Decisión primero:** `68ca937` precisa D1 en la ADR y en `SECURITY.md` antes del código.
- **Paquete `providers/google_browser/`** (`00e8e36`):
  - `selectors.py`: versión `google-web-2026.10-v1`, hosts permitidos y selectores en español e
    inglés.
  - `parsing.py`: nombre del documento con su marca de tiempo, ID del documento, referencia del
    evento, normalización del texto y días de la ventana.
  - `driver.py`: Playwright solo observa el DOM; la navegación queda limitada a Calendar y Docs.
  - `adapter.py`: identidad, descubrimiento por ventana, lectura y registro con el contrato de la
    ruta oficial.
- **Conexión con las superficies** (`19544f2`): `has_browser_route` exige que el adaptador de la
  familia sirva alguna capacidad del destino, así que solo `google-meet` gana la ruta. `connect`
  usa el navegador sin cliente de Google o ante `admin_consent_required`, y `bootstrap` registra el
  adaptador para la conexión por navegador.
- **Pruebas existentes adaptadas:** las que fijaban a Meet como solo oficial (gates, aplicación,
  CLI, servicio, doctor, remediación de política y fila de la matriz). Gmail y Calendar conservan
  sus rechazos.
- **Compuertas:** 1381 pruebas aprobadas y 4 excluidas, cobertura de ramas 89 %, `ruff format
  --check`, `ruff check` y `mypy src` limpios.
- **Tamaño:** unas 2.500 líneas, de las cuales unas 1.380 son pruebas. Supera la heurística de 400
  porque el paquete, su driver y sus fixtures forman una sola unidad; se partió en paquete y
  conexión.
- **Brechas y preguntas abiertas:**
  - La sonda de identidad y el atributo `data-eventid` no se observaron en la prueba del
    2026-10-26; los valida T5.1.
  - `recorded_at` sale de la marca del nombre del documento y `organizer` queda en `null`: son las
    dos diferencias con la ruta oficial.
  - `--meeting-id` no está disponible en esta ruta.
  - El enlace de la grabación no tiene campo en el esquema; no se agregó.
  - La ruta oficial solo reconoce títulos con «transcript» y no los que terminan en
    «Transcripción».
  - El check de doctor de la ruta se sigue llamando `microsoft_browser_route` (T6.1).

### Rebanadas de entrega (`feature-branch-chain`)

| Rebanada | Commits | Líneas | Contenido |
| --- | --- | --- | --- |
| R1 | `9a186db` | 640 | ADR y documentos |
| R2 | `a982184`, `d76cdbd` | ~1.110 | Meet y Jira por cuenta |
| R3 | `5ccef97`, `6c4bd6c` | ~905 | Tokens con alcance y política de Google |
| R4 | `675e80e`, `8fa6b87` | ~1.770 | OAuth de Atlassian y bloqueo de política en Google |
| R5 | `43acc56`, `1d31b86`, `88955a3` | ~3.070 | Base de navegador por familias (Fase 1) |
| R6 | `a6142b4` a `6ad07e3`, `0ab30c7` | ~2.730 | Modo proyecto (Fase 7) |
| R7 | `68ca937`, `00e8e36`, `19544f2` y la documentación | ~2.700 | Google Meet por navegador (T5.3) |

## Seguimiento fuera de alcance

- `openspec/config.yaml:31-34` (`rules.apply`) mezcla elementos de lista con claves `tdd` y
  `test_command`, así que el YAML no se puede parsear.
- El problema ya existía antes de este cambio (ver `HEAD~1`).
- Necesita una corrección propia. No se corrige aquí sin autorización.

## Siguiente paso

La ruta oficial, la Fase 1 y la Fase 7 (T7.0 a T7.4) están completas.

**T7.5 (preparada, pendiente de Camilo).** Camilo la autorizó el 2026-10-25 con la opción «Preparar
sin reinstalar». En `IDARTES/pandora_proxy/.solercia-connect/` quedaron:

- la estructura creada por `prepare_project_home`: carpetas 0700 y `.gitignore` 0600 con `*`;
- `connect.env` 0600, con los nombres de las variables comentados y sin valores;
- `solercia-connect.sh` 0700, que usa `uv run --project` contra esta rama sin tocar la instalación
  global;
- `LEEME.md` 0600, con los pasos y la instrucción para el agente.

Verificación:

- `doctor` reporta `project_mode ready: true`, `git_guard: clean` y `gitignore: ignores_all`.
- `git status` de `pandora_proxy` no muestra nada de esa carpeta.
- No se hizo ningún commit en `pandora_proxy`.

Falta que Camilo llene `connect.env` y ejecute `connect gmail` y `connect jira`.

Hay una limitación: Gmail solo ofrece `mail.sent.list`, y la bandeja de entrada existe solo para
Outlook.

Siguiente paso técnico: T3.1, el adaptador de navegador para Jira.

Al 2026-10-26, T5.3 está implementada offline. Para usarla con datos reales, Camilo ejecuta las
compuertas en vivo T5.0 (inicio de sesión en el perfil dedicado) y T5.1 (sonda de identidad) y
confirma la precisión de D1 del 2026-10-26.
