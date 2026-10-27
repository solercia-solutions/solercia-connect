# Decisión: fallback de navegador para Google y Jira

- **Estado:** Aceptada
- **Fecha de decisión:** 2026-10-25
- **Decidido por:** Camilo Calderón, propietario del producto
- **Ámbito:** Enrutamiento oficial primero, fallback de navegador, frontera de seguridad del
  navegador e identidad de cuenta en las familias de Google y Jira
- **Aplicación:** Obligatoria para toda ruta de navegador de las familias `google` y `jira`
- **Actualiza:**
  [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisiones 3.2, 3.6, 3.8, 4.8 y 5.6), [`SECURITY.md`](../../SECURITY.md),
  [`AGENTS.md`](../../AGENTS.md), [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md),
  [`openspec/config.yaml`](../../openspec/config.yaml) y
  [`agents/skills/solercia-connect/SKILL.md`](../../agents/skills/solercia-connect/SKILL.md)
- **Documento de trabajo:**
  [`odd/tasks/google-jira-browser-fallback.md`](../../odd/tasks/google-jira-browser-fallback.md)

## Contexto

Hasta esta decisión, el navegador supervisado solo podía actuar como respaldo de Microsoft. Las
reglas vigentes lo dicen de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| Registro de plataforma, decisión 3.2 | «Playwright es únicamente un fallback para Microsoft.» |
| `SECURITY.md`, fallback de navegador | «Browser fallback is Microsoft-only, explicit, typed, and allowlisted. It is never primary.» |
| `SECURITY.md`, fallback de navegador | «Do not call retired or undocumented provider endpoints.» |
| `AGENTS.md`, arquitectura | «`providers/m365_browser/` is an explicit Microsoft-only fallback foundation.» |
| `src/solercia_connect/runtime.py:68` | `browser_fallback_families: frozenset[ProviderFamily] = frozenset({ProviderFamily.MICROSOFT})` |

El pedido de Camilo del 2026-10-25 fue: «quiero que también haya compatibilidad con conectores de
Google o de Gmail y de Jira, debe servir cualquier cuenta […] si se puede con permisos pues bien
[…] pero si no pues por fallback». Al preguntarle por esta decisión respondió: «Jira de ser posible
por API e integraciones e igual Google, pero si no se puede entonces por Playwright».

### Por qué la ruta oficial puede no estar disponible

- **Google.** `gmail.readonly` y `drive.readonly` son alcances restringidos. Una app en estado de
  prueba admite como máximo 100 usuarios y la autorización vence a los 7 días. Además, un
  administrador de Google Workspace puede bloquear las apps no configuradas o restringir el acceso
  a Gmail y Drive; en ese caso Google responde `admin_policy_enforced`.
- **Jira.** Atlassian OAuth (3LO) exige `client_secret` y no admite PKCE, de modo que una CLI
  instalada no puede ser un cliente público. Por otra parte, una organización puede impedir que sus
  miembros creen o usen API tokens.

Las citas textuales que respaldan estos hechos están en la sección «Fuentes oficiales».

### La ruta oficial funciona hoy para las cuentas actuales

El documento de trabajo registra la evidencia de AUTOMATINFORMES, otro proyecto del mismo usuario:

- Gmail y Google Calendar se leen hoy con un cliente OAuth propio de Google Cloud y tokens de tres
  cuentas institucionales. Las credenciales son del 2026-02-28 y los tokens se renovaron el
  2026-09-19 y el 2026-10-18; por tanto, la autorización no vence a los 7 días y los administradores
  de esas organizaciones no bloquean la app.
- Jira se lee con `Authorization: Basic` y API token contra `/rest/api/3/search/jql`.

Lo anterior implica que, para las cuentas actuales, la ruta oficial es suficiente. El navegador se
construye como red de seguridad para cuentas u organizaciones donde la ruta oficial no esté
disponible, no como ruta primaria. Camilo confirmó ese alcance el 2026-10-25 («Plan completo»),
después de revisar esta evidencia.

## Decisión

1. **Ampliación de familias (D0).** El fallback de navegador se amplía a dos familias más:
   - **Google:** Gmail `mail.sent.list`, Google Calendar `calendar.events.list` y Google Meet
     `meeting.transcripts.list` / `meeting.transcript.get`.
   - **Jira:** `external.issues.search`.

   La ruta oficial (OAuth o API token) sigue siendo la primaria. El navegador solo entra cuando la
   ruta oficial falla con una razón tipada de la lista permitida.
2. **Mismas garantías que Microsoft.** Las rutas de navegador de Google y Jira cumplen, sin
   excepción, las garantías que hoy tiene Microsoft:
   - La configuración `SOLERCIA_CONNECT_BROWSER_FALLBACK` solo habilita la disponibilidad de la
     ruta. Cada invocación, incluidos `status` y `health`, exige además `--allow-browser-fallback`.
   - Cada familia usa un perfil persistente dedicado por cuenta, bajo una raíz propia: `m365`,
     `google` y `jira`. Un perfil nunca se comparte entre familias y nunca es un perfil personal de
     Chrome, Edge, Brave o Chromium.
   - El usuario realiza el inicio de sesión, la contraseña y el MFA. El runtime nunca pide ni
     escribe credenciales.
   - Después del inicio de sesión visible, Connect reabre el mismo perfil en modo headless y repite
     la prueba de identidad antes de guardar `connected_browser`.
   - Las rutas son de solo lectura. La única escritura de Connect sigue siendo
     `mail.categories.set` de Outlook.
   - No se capturan, persisten ni registran cookies, tokens ni cabeceras de autorización.
   - El enrutamiento es determinista; ningún LLM elige rutas, selectores, reintentos, identidad ni
     clasificación de fallos.
   - Una identidad ausente, malformada, ambigua o distinta, y cualquier fallo desconocido, terminan
     en un fallo tipado y en cero lecturas de datos (fail-closed).
3. **Permiso de la organización no disponible (D0.1).** Solo para Google y Jira, dos fallos se
   tratan como «permiso de la organización no disponible» y habilitan el respaldo, igual que hoy
   `ADMIN_CONSENT_REQUIRED` en Outlook:
   - Google `admin_policy_enforced`.
   - Jira con API token bloqueado por la política de autenticación de la organización.

   Hoy el código no los distingue: el 403 de Google se clasifica como `AUTHORIZATION_DENIED` y
   `admin_policy_enforced` como `REQUEST_REJECTED`. Las tareas T2.4 y T4.2 del documento de trabajo
   los llevan a una razón tipada de la lista permitida. El comportamiento de Microsoft no cambia:
   `TENANT_POLICY_BLOCKED` sigue sin habilitar el respaldo y la doctrina de Teams se mantiene (una
   política de tenant o una cuenta personal nunca se rodean por el navegador).
4. **Excepción acotada a la regla de endpoints documentados (D1).** `SECURITY.md` prohíbe llamar
   endpoints de proveedor retirados o no documentados. Esta decisión registra una excepción
   **solo para Google y solo de lectura**, limitada a tres mecanismos:
   - **Google Calendar:** la exportación ICS del calendario.
   - **Google Meet:** la exportación a texto plano del documento de Docs que contiene la
     transcripción. Este mecanismo se probó en vivo el 2026-09-02. La prueba en vivo del
     2026-10-26 lo precisó: ver «Precisión del 2026-10-26: mecanismo de Google Meet».
   - **Gmail:** el listado de enviados sale del DOM de la búsqueda `in:sent after: before:` y el
     cuerpo completo sale de la descarga del mensaje original (RFC 822).

   Los selectores y formatos son versionados, deterministas y respaldados por fixtures saneados.
   Cualquier discrepancia falla cerrada con `BROWSER_SELECTOR_MISMATCH`. La regla general sigue
   vigente para todo lo demás: Microsoft y Jira no reciben esta excepción y ningún otro mecanismo
   no documentado de Google queda autorizado.
5. **Ruta de navegador de Jira.** Usa solo endpoints documentados del sitio:
   `GET /rest/api/3/myself` y `GET /rest/api/3/search/jql`, emitidos desde el contexto de la página
   del mismo sitio (mismo origen) con la sesión web del usuario. Connect no lee ni copia la cookie
   de sesión. La prueba de identidad es:
   - `accountId` de `/myself` igual a la cabecera de respuesta `X-AAccountId`, ligado al host del
     sitio;
   - si `/myself` expone `emailAddress`, debe coincidir con la cuenta solicitada;
   - un `accountId` vacío o `unknown` falla cerrado.
6. **Prueba de identidad del navegador de Google.** Google no publica un endpoint documentado que
   devuelva la identidad de la sesión web. La sonda se elige y se prueba en vivo en la tarea T5.1.
   Mientras esa prueba no exista, la ruta de navegador de Google permanece cerrada: responde con un
   fallo tipado y no lee datos.

## Precisión del 2026-10-26: mecanismo de Google Meet

- **Estado:** registrada el 2026-10-26 a partir de la prueba en vivo de ese día, hecha con una
  cuenta de Google Workspace en un perfil de Chrome con la sesión iniciada por el usuario. Queda
  pendiente la confirmación de Camilo; el documento de trabajo la lista como pregunta abierta.
- **Qué se observó:**
  - Pedida desde la página, la exportación a texto (`/document/d/{id}/export?format=txt`) responde
    302 hacia una URL firmada de `googleusercontent.com` y el navegador la bloquea por CORS. Desde
    la página no sirve.
  - La vista `https://docs.google.com/document/d/{id}/mobilebasic` presenta el documento como HTML
    simple. Su texto visible trae el título, «Asistentes», la lista de asistentes, «Transcripción»,
    los turnos «Nombre: texto», una marca de tiempo cada cinco minutos y el cierre de la reunión.
    En `/edit` el documento se dibuja en un canvas y su texto no se puede extraer.
  - La vista diaria de Calendar web (`/calendar/u/0/r/day/AAAA/M/D`) muestra cada evento como un
    botón. El detalle del evento trae los adjuntos «… - Recording» (Drive) y «… - Transcript»
    (Docs) y el enlace de Meet.
  - Calendar web y Drive web cargan sus datos con API internas (`sync.fetcheventrange`,
    `calendar-pa.clients6.google.com`, `drivefrontend-pa.clients6.google.com`). Esas API no tienen
    contrato y exigen una cabecera derivada de las cookies de la sesión.
- **Precisión de D1 para Meet.** La excepción de Meet cubre exactamente dos lecturas de página:
  1. el descubrimiento por el DOM de la vista diaria de Calendar web y del detalle de cada evento,
     del que solo se toma el adjunto de transcripción;
  2. el texto visible de la vista `mobilebasic` del documento de la transcripción.

  Quedan excluidas las API internas de Calendar y Drive, toda lectura de cookies o cálculo de
  cabeceras a partir de ellas, y las URL firmadas de la exportación. La exportación ICS de Calendar
  y el mecanismo de Gmail de D1 no cambian.
- **Prueba de identidad.** La sonda de T5.1 lee la cuenta del botón de cuenta de la barra de Google
  en Calendar web y exige que coincida con la cuenta pedida. Su validación en vivo sigue pendiente;
  mientras tanto, cualquier discrepancia del selector termina en un fallo tipado y en cero lecturas.

## Fuentes oficiales

Las citas siguientes son textuales y conservan el idioma de la fuente; el análisis va aparte.

### Google

- «Projects configured with a publishing status of **Testing** are limited to up to 100 test users
  listed in the OAuth consent screen.» y «Authorizations by a test user will expire seven days from
  the time of consent.» (*Manage App Audience*, Google Cloud Platform Console Help, sección
  «Publishing status»).
- «The Google Account is unable to authorize one or more scopes requested due to the policies of
  their Google Workspace administrator.» (*Using OAuth 2.0 for Web Server Applications*, error
  `admin_policy_enforced`).
- «**Don't allow users to access any third-party apps**—Users can't sign in with Google to any
  third-party apps and websites until you configure those apps and sites with an access setting.» y
  «For Gmail, Google Drive, and Google Chat, you can specifically restrict access to high-risk
  scopes» (*Control which apps access Google Workspace data*, Google Workspace Admin Help).
- «You can export a copy of your events from Google Calendar to your computer.» y «The exported
  file is an .ics file, which you can import into other calendar apps.» (*Export events from your
  Google Calendar*, Google Calendar Help).
- «using automated means to access content from any of our services in violation of the
  machine-readable instructions on our web pages (for example, robots.txt files that disallow
  crawling, training, or other activities)» (*Google Terms of Service*, «Don't abuse our services»).

Lo anterior implica que la exportación ICS es una función que Google ofrece al usuario, pero su URL
de descarga no es un contrato de API. Lo mismo ocurre con la exportación a texto de Docs y con la
interfaz de Gmail. Por eso se necesita la excepción D1, acotada y de solo lectura.

### Atlassian

- «Cookie-based authentication is _deprecated_» y «Jira Cloud has deprecated cookie-based
  authentication in favor of basic authentication with API tokens or OAuth.» (*Cookie-based auth
  for REST APIs*, Jira Cloud platform).
- «Returns details for the current user.» y «A user with details as permitted by the user's
  Atlassian Account privacy settings.»; para registros corruptos, «`accountId` returns _unknown_
  and all other parameters have fallback values.» (*The Jira Cloud platform REST API*, grupo
  Myself, «Get current user»).
- «Refresh tokens are implemented using rotating refresh tokens.» y, en el cuerpo del intercambio,
  «`client_secret`: (_required_) Set this to the **Secret** for your app.» (*Implementing the
  Refresh Token Flow*).
- «Desktop applications should use Authorization Code grant flow with **PKCE extension** to
  authorize user and to avoid storing any secrets on user's device.» (ECO-283, sugerencia con
  resolución «Unresolved»).
- «With the user API token setting, you can allow or block members from:», seguido de «Creating a
  new API token» y «Using an existing API token» (*Authentication policy settings for your
  organizations*, sección «User API tokens»).

Lo anterior implica que la ruta de navegador de Jira usa endpoints documentados, pero con una forma
de autenticación (la sesión web) que Atlassian ya no recomienda para clientes REST de terceros. Su
funcionamiento se apoya en la observación y requiere validación en vivo (T3.2). La cabecera
`X-AAccountId` tampoco figura en la referencia: es una observación que la misma validación debe
confirmar. Que `/myself` pueda ocultar `emailAddress` según la privacidad de la cuenta explica por
qué la identidad se ancla en `accountId` y el correo solo se compara cuando está presente.

## Estado de implementación

Esta decisión no implementa código. Al 2026-10-25, `runtime.py` sigue limitando el respaldo a
`ProviderFamily.MICROSOFT` y la ruta de navegador de Google y Jira no existe. El documento de
trabajo ordena la implementación: primero la ruta oficial de Jira y Google (T2.x y T4.x), después
la base de navegador genérica (T1.x) y por último las rutas de navegador de Jira (T3.x) y Google
(T5.x).

Al 2026-10-26, la ruta de navegador de Google Meet (T5.3) está implementada y probada offline en
`providers/google_browser/`, con la sonda de identidad del botón de cuenta. La matriz la declara
`implemented-offline` hasta que Camilo ejecute las compuertas en vivo T5.0 y T5.1. Gmail, Calendar
y Jira siguen sin ruta de navegador.

### Validación en vivo pendiente

| Tarea | Qué debe demostrar |
| --- | --- |
| T3.2 | Inicio de sesión supervisado en Jira, prueba de identidad con `accountId` y `X-AAccountId`, y lectura con `search/jql` usando la sesión web. |
| T5.0 | Que Google permite iniciar sesión en el perfil dedicado de Playwright. Si Google lo rechaza («This browser or app may not be secure»), la fase de navegador de Google se detiene. |
| T5.1 | La sonda de identidad de la sesión web de Google. |

Las pruebas en vivo las ejecuta Camilo, que hace el inicio de sesión y el MFA. Las pruebas offline
no acreditan selectores, sesiones ni contratos reales.

## Límites que no cambian

- La API oficial sigue siendo la ruta primaria de todas las familias.
- Microsoft conserva su comportamiento: rutas, prueba de identidad de Outlook, estrategia de Teams,
  `TENANT_POLICY_BLOCKED` y la doctrina de transcripciones de Teams.
- La única escritura sigue siendo `mail.categories.set` de Outlook, con `--allow-write`.
- Esta decisión no modifica el opt-in de inicio de sesión interactivo por HTTP
  (`SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN`).
- Credenciales inválidas, identidad distinta, datos malformados y fallos desconocidos nunca entran
  al respaldo.

## Consecuencias y riesgos

- Las cuentas cuya organización bloquea la app o los API tokens pueden leer sus propios datos por el
  navegador supervisado, con la misma doble autorización que Microsoft.
- El navegador no amplía permisos: opera con lo que la organización ya permite a la sesión web del
  usuario. Una política que restringe apps de terceros puede buscar limitar el acceso automatizado,
  así que quien invoca el respaldo debe confirmar que su uso es compatible con las políticas de su
  organización.
- Google puede rechazar el inicio de sesión en un navegador automatizado. La compuerta en vivo T5.0
  detiene la fase en ese caso.
- Los términos de Google prohíben el acceso automatizado que contraviene las instrucciones legibles
  por máquina, como `robots.txt`. La ruta se limita a la sesión del propio usuario, con invocación
  explícita y de solo lectura; su compatibilidad con esos términos se revisa con la validación en
  vivo.
- La extracción de Gmail por el DOM es frágil y requiere mantenimiento de selectores. Un cambio de
  interfaz produce `BROWSER_SELECTOR_MISMATCH`, no datos incompletos.
- Atlassian declaró obsoleta la autenticación por cookie para clientes REST de terceros. La ruta por
  sesión web funciona por observación y depende de la validación en vivo T3.2.
- El código gana un paquete de navegador por familia y una raíz de perfil por familia. El costo de
  mantenimiento crece de forma proporcional.

## Plan de reversión

Retirar `GOOGLE` y `JIRA` de `browser_fallback_families` en `RoutePolicy` devuelve esas familias a
la ruta oficial únicamente; las invocaciones con `--allow-browser-fallback` responden con el fallo
oficial tipado. No hay estado compartido ni migración que deshacer. Los perfiles dedicados de
`google` y `jira` dejan de usarse y el usuario decide si los retira.

## Alternativas descartadas

- **Solo la ruta oficial, sin respaldo:** deja sin acceso a las cuentas cuya organización bloquea
  la app o los API tokens, contra el pedido explícito del propietario.
- **Usar el perfil personal del navegador o importar sus cookies:** contradice la regla del perfil
  dedicado y expone sesiones ajenas a Connect.
- **Capturar la cookie de sesión y llamar las APIs fuera del navegador:** contradice la regla de no
  leer ni persistir cookies. Para Jira, además, replica la autenticación por cookie que Atlassian
  declaró obsoleta.
- **Extender la excepción D1 a Microsoft o a Jira:** no hace falta. Microsoft ya cuenta con
  endpoints documentados para su ruta y Jira usa los mismos endpoints REST de la ruta oficial.
- **Navegación o selección de selectores con un LLM:** contradice la decisión 3.4 del registro de
  plataforma.

## Referencias oficiales

- [Cookie-based auth for REST APIs — Jira Cloud platform](https://developer.atlassian.com/cloud/jira/platform/jira-rest-api-cookie-based-authentication/)
- [The Jira Cloud platform REST API — Myself](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-myself/)
- [Implementing the Refresh Token Flow — Atlassian](https://developer.atlassian.com/cloud/oauth/getting-started/refresh-tokens/)
- [ECO-283 — PKCE for the Jira Cloud REST API](https://jira.atlassian.com/browse/ECO-283)
- [Authentication policy settings for your organizations — Atlassian Support](https://support.atlassian.com/security-and-access-policies/docs/authentication-policy-settings-for-your-organizations/)
- [Manage App Audience — Google Cloud](https://support.google.com/cloud/answer/15549945)
- [Control which apps access Google Workspace data — Google Workspace Admin Help](https://support.google.com/a/answer/7281227)
- [Using OAuth 2.0 for Web Server Applications — Google Identity](https://developers.google.com/identity/protocols/oauth2/web-server)
- [Export events from your Google Calendar — Google Calendar Help](https://support.google.com/calendar/answer/37111)
- [Google Terms of Service](https://policies.google.com/terms)
