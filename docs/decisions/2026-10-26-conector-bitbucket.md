# Decisión: conector de Bitbucket Cloud de solo lectura

- **Estado:** Aceptado
- **Fecha de decisión:** 2026-10-26
- **Decidido por:** Camilo Calderón, propietario del producto, a partir de la propuesta del
  orquestador (B-D0) y de la evidencia oficial recogida ese día (B-D1 y B-D2)
- **Ámbito:** Proveedores admitidos, autenticación por API token, identidad de cuenta, lecturas de
  repositorios, pull requests y commits
- **Aplicación:** Obligatoria para el target `bitbucket` y las capacidades `scm.repositories.list`,
  `scm.pullrequests.list` y `scm.commits.list`
- **Actualiza:** [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (mapa de responsabilidades y decisión 1.1), [`SECURITY.md`](../../SECURITY.md) y
  [`AGENTS.md`](../../AGENTS.md)

## Contexto

Solercia Connect integra Microsoft 365, Google Workspace, Jira y GLPI. El código de los proyectos de
IDARTES vive en Bitbucket Cloud: el `origin` de `pandora_proxy` es
`bitbucket.org/idartes-tecnologia/pandora_proxy`, dentro del workspace `idartes-tecnologia`.

El pedido quedó registrado así en el documento de la funcionalidad
(`odd/tasks/bitbucket-connector.md`, sección «Pedido»):

> Camilo, 2026-10-26: «dime si bitbucket tiene una integración similar para hacerla tambien con
> solercia-connect» y, después, «tu preocupate por conectarte a bitbucket».

El registro de plataforma no admite proveedores nuevos de forma implícita (decisión 1.1 y regla de
lectura). Esta decisión admite Bitbucket Cloud como una familia nueva, `BITBUCKET`, y fija sus
límites antes de que exista el código.

## Decisión

### B-D0. Primer corte de solo lectura y solo por la ruta oficial

1. La familia `BITBUCKET` lee por la API REST oficial de Bitbucket Cloud, con base fija en
   `https://api.bitbucket.org/2.0`. No tiene ruta de navegador: no figura en la lista de familias
   del fallback ni en la de capacidades permitidas, y `--allow-browser-fallback` se rechaza como
   entrada inválida.
2. No escribe nada en Bitbucket. Las tres escrituras acotadas de Connect siguen siendo las mismas;
   cualquier escritura en Bitbucket necesita su propia ADR.
3. Las capacidades son tres lecturas: `scm.repositories.list` (repositorios del workspace en los que
   la cuenta es miembro), `scm.pullrequests.list` (pull requests de un repositorio en una ventana de
   `updated_on`) y `scm.commits.list` (commits de una rama en una ventana de fechas).
4. El servicio HTTP en loopback nunca recibe el token: la conexión de Bitbucket, como la de Jira,
   solo existe en la CLI canónica.

### B-D1. Autenticación con un API token con alcances de Bitbucket

1. La conexión usa un API token de la cuenta de Atlassian creado con la opción «Create API token
   with scopes», para la app Bitbucket. Viaja por Basic, con el correo de la cuenta como usuario y el
   token como contraseña.
2. El token debe tener los cuatro alcances de lectura: `read:user:bitbucket`,
   `read:workspace:bitbucket`, `read:repository:bitbucket` y `read:pullrequest:bitbucket`. Ningún
   alcance de escritura es necesario.
3. El token se guarda por cuenta en el almacén de secretos, con el servicio de llavero
   `solercia-connect.api-token` y la clave `bitbucket:<email>`; en modo proyecto, en el archivo
   privado equivalente. La clave no colisiona con `jira:<email>`. El perfil no secreto guarda el
   workspace por defecto y el `account_id` observado.
4. El token se lee de una variable del entorno efectivo (`--token-env`), del prompt oculto o de una
   línea de stdin (`--token-stdin`). Nunca es un argumento y nunca se imprime.

### B-D2. Prueba de identidad determinista y cerrada ante fallos

Antes de guardar una conexión, al consultar su estado y antes de cada lectura se comprueban, en este
orden:

1. `GET /2.0/user` devuelve un `account_id` no vacío. En `status` y en las lecturas debe ser igual al
   guardado en el perfil.
2. `GET /2.0/user/emails`, siguiendo solo el enlace `next` y con un tope de páginas, lista el correo
   de la cuenta (sin distinguir mayúsculas y minúsculas) con `is_confirmed` verdadero. Una forma
   ausente o desconocida de la respuesta falla con `malformed_provider_data`.
3. `GET /2.0/workspaces/{workspace}` responde 200 para el workspace que se va a leer.

Cualquier diferencia produce cero lecturas de datos y un fallo tipado. Al conectar, un fallo no deja
nada guardado: primero se prueba la identidad, después se guarda el token y por último el perfil; si
el perfil no se puede escribir, el token se borra.

| Respuesta | Resultado |
| --- | --- |
| 401 | `invalid_credentials`, con la indicación de que hace falta un API token con alcances de Bitbucket y de que el token clásico de Jira no sirve |
| 403 | `authorization_denied`, con los nombres de los alcances que falten cuando la respuesta los trae; los nombres de alcance no son secretos |
| 404 del workspace | `workspace_not_found` |
| Correo ausente, sin confirmar o `account_id` distinto | `authorization_boundary_violation` |

### Paginación y tope

Las lecturas siguen solo el enlace `next`, que debe conservar el origen y la ruta de la colección
pedida, y tienen un tope fijo de páginas. Al alcanzarlo, la lectura falla con `result_incomplete` y
el sobre lleva `complete: false`: nunca devuelve una lista truncada. La salida se ordena de forma
determinista.

En los commits no existe filtro de fecha en el servidor. La ventana se filtra en el cliente y la
lectura se detiene después de una página completa en la que todos los commits son anteriores al
inicio de la ventana menos siete días, o falla con `result_incomplete` al llegar al tope.

## Evidencia oficial

Las citas siguientes son textuales y conservan el idioma de la fuente; el análisis va aparte. La
consulta se hizo el 2026-10-26.

### Autenticación

- «To authenticate with an API token, use Basic HTTP Authentication as per RFC-2617, where the
  username is your Atlassian email and password is the API token.» y «API token require an expiry
  date at creation, with a maximum duration of 1 year.» (*The Bitbucket Cloud REST API*, sección
  «Authentication methods», «API tokens»).
- «API tokens created to access Bitbucket APIs or perform Git commands must have scopes.» y los pasos
  «Select Create API token with scopes.» y «Select Bitbucket as the app and select Next.»
  (*Create an API token*, Bitbucket Cloud).
- «Bitbucket Cloud continues to support both Basic and Bearer authentication for API tokens.»
  (*Changelog*, entrada del 18 de agosto de 2026, «Added Bearer authentication support for API
  tokens»).
- «app passwords will be fully deprecated on Jul 28, 2026.» (*Changelog*, entrada del 29 de mayo de
  2026, «Brownout Notice: App password deprecation for Bitbucket Cloud»).

Lo anterior implica que el token clásico de Jira, creado sin alcances, no sirve para Bitbucket, y que
los app passwords ya no son una alternativa. Basic sigue admitido, así que la conexión reutiliza el
mismo mecanismo que Jira.

### Alcances

- «Unlike OAuth 2.0 scopes, Forge app and API token scopes do not implicitly grant access to other
  scopes» (*The Bitbucket Cloud REST API*, «Forge app and API token scopes»).
- `read:user:bitbucket`: «Allows viewing of data related to the current user.»
- `read:workspace:bitbucket`: «Allows viewing of workspace and workspace permission data.»
- `read:repository:bitbucket`: «Allows viewing of repository data. Note that this scope does not give
  access to a repository's pull requests.»
- `read:pullrequest:bitbucket`: «This scope does not imply the `read:repository:bitbucket` scope.»
- La especificación OpenAPI publicada asigna `read:user:bitbucket` a `GET /user` y
  `GET /user/emails`, `read:workspace:bitbucket` a `GET /workspaces/{workspace}`,
  `read:repository:bitbucket` a `GET /repositories/{workspace}`, `GET /repositories/{workspace}/{repo_slug}`
  y `GET /repositories/{workspace}/{repo_slug}/commits/{revision}`, y `read:pullrequest:bitbucket` a
  `GET /repositories/{workspace}/{repo_slug}/pullrequests` (campo `x-atlassian-oauth2-scopes`).

En la práctica, las cuatro lecturas de la prueba de identidad y de las capacidades necesitan
exactamente esos cuatro alcances, y ninguno cubre a otro.

### Identidad y workspace

- `GET /user`: «Returns the currently logged in user.»; el esquema `user` incluye `account_id`,
  `uuid`, `display_name` y `nickname` (especificación OpenAPI).
- `GET /user/emails`: «Returns all the authenticated user's email addresses. Both confirmed and
  unconfirmed.» La especificación solo documenta la respuesta de error, no la forma de la respuesta
  correcta.
- `GET /workspaces/{workspace}`: «Returns the requested workspace.», con respuestas 200 y 404.
- El esquema `error` define `error.message`, `error.detail` como texto y `error.data` como «Optional
  structured data that is endpoint-specific.»

Lo anterior implica que la forma de la lista de correos (`email`, `is_primary`, `is_confirmed`) y la
lista de alcances de un 403 (`required` y `granted`) no están garantizadas por la especificación. El
código las lee de forma defensiva: una forma desconocida de la lista de correos falla cerrada, y del
403 solo cita nombres que tengan la forma de un alcance de Bitbucket.

### Paginación y filtros

- «Globally, the minimum length is 10 and the maximum is 100.», «This link should be treated as an
  opaque location that is not to be constructed by clients or even assumed to be predictable.» y
  «Lack of a next link in the response indicates the end of the collection.» (*The Bitbucket Cloud
  REST API*, «Pagination»).
- «Filter queries can be added to the URL using the q= query parameter. To sort the response, add
  sort=.», con `/2.0/repositories/{username}/{slug}/pullrequests` entre los recursos admitidos
  (misma página, «Filter and sort API objects»).
- `GET /repositories/{workspace}`, parámetro `role`: «**member**: returns repositories to which the
  user has explicit read access» (especificación OpenAPI).
- `GET /repositories/{workspace}/{repo_slug}/pullrequests`: «To retrieve pull requests that are in
  one of multiple states, repeat the `state` parameter for each individual state.», con los valores
  `OPEN`, `MERGED`, `DECLINED` y `SUPERSEDED` (especificación OpenAPI).
- `GET /repositories/{workspace}/{repo_slug}/commits/{revision}`: «They are paginated and returned in
  reverse chronological order, similar to the output of `git log`.» El endpoint no declara ningún
  parámetro de fecha (especificación OpenAPI).
- La especificación OpenAPI publicada ya no incluye `GET /repositories` (la lista entre workspaces).
  La investigación del 2026-10-26 registró su retiro el 14-04-2026; la página actual del changelog ya
  no muestra esa entrada.

En la práctica, los repositorios se leen por workspace, y las pull requests se filtran en el servidor
con `q` y se verifican otra vez en el cliente. Los commits siguen el orden del grafo, como `git log`,
así que sus fechas no son estrictamente decrecientes (rebases, cherry-picks o relojes desfasados). Por
eso la ventana se filtra en el cliente y la parada exige una página completa anterior al inicio menos
siete días.

### Límites de solicitudes

- «Any access to /2.0/repositories/* such as: List a user or team's repositories
  (/2.0/repositories/{workspace}) … List a repository's commits
  (/2.0/repositories/{workspace}/{slug}/commits)», con el límite «1,000 - 10,000 per hour»
  (*API request limits*, «Authenticated Request limits»).
- Los límites escalados exigen, entre otros requisitos, «Must use workspace, project, repository
  access tokens for authentication» (misma página, «Scaled rate limits»).

Lo anterior implica que un API token personal queda en 1.000 solicitudes por hora sobre
`/2.0/repositories/*`. Un 429 se reporta como `rate_limited`.

## Alternativas descartadas o diferidas

- **OAuth 2.0 con un consumidor de Bitbucket (diferida).** «OAuth needs a key and secret, together
  these are know as an OAuth consumer.» y el consumidor se crea en «Workspace settings» → «OAuth
  consumers» (*Use OAuth on Bitbucket Cloud*). El intercambio del código usa
  `curl -X POST -u "client_id:secret"` (misma página, «Authorization code grant»), y la documentación
  no describe PKCE. Lo anterior implica que la conexión exigiría guardar un secreto de cliente y que
  un administrador del workspace cree el consumidor. Se reconsidera si una entidad no admite API
  tokens.
- **Tokens de acceso de workspace o de proyecto (descartados).** «Access tokens for a workspace are a
  **Premium** feature.» y «Access tokens for a workspace are not tied to a user's account but are
  connected to a Bitbucket workspace» (*Access tokens for a workspace*). No prueban a una persona, que
  es la base de la decisión 4.1 del registro de plataforma, y exigen el plan Premium.
- **App passwords (descartados).** Quedaron retirados el 28-07-2026, según la cita anterior.
- **Ruta de navegador (descartada).** La API oficial cubre las tres lecturas; un navegador añadiría
  una superficie sin necesidad.
- **Bearer en lugar de Basic (diferida).** Bitbucket acepta ambos. Basic con el correo liga el token a
  la cuenta en la propia autenticación, igual que en Jira.

## Riesgos

- **Expiración del token.** Un token dura como máximo un año. Al vencer, `status` y las lecturas
  responden `invalid_credentials` o `action_required` con el comando `connect` exacto; nada se lee.
- **Límite de solicitudes.** 1.000 por hora sobre `/2.0/repositories/*` con un token personal. Las
  lecturas tienen tope de páginas y la prueba de identidad usa endpoints fuera de ese grupo.
- **Orden de los commits.** El orden sigue el grafo; una rama con historia reescrita o muy antigua
  puede exigir más páginas que el tope. En ese caso la lectura falla con `result_incomplete` en lugar
  de truncar.
- **Formas no garantizadas.** La lista de correos y el detalle de los 403 no están en la
  especificación; su validación viva queda a cargo de Camilo.
- **Workspace no visible.** Bitbucket responde 404 tanto para un workspace inexistente como para uno
  al que la cuenta no pertenece; ambos casos se reportan como `workspace_not_found`.

## Consecuencias

- Los agentes leen repositorios, pull requests y commits de Bitbucket con el mismo sobre, procedencia
  y recibo que el resto de Connect, sin reimplementar la autenticación ni la paginación.
- El modo proyecto funciona sin cambios: el token se guarda en el archivo privado de secretos y
  `--token-env BITBUCKET_API_TOKEN` lo lee de `connect.env`.
- Cada lectura hace tres solicitudes de identidad antes de leer datos.
- Las pruebas son offline. La validación viva contra `idartes-tecnologia` queda pendiente.

## Plan de reversión

Retirar el target `bitbucket` de `TARGET_SPECS` y el adaptador del arranque deja sin ruta las tres
capacidades; no hay migraciones ni datos compartidos que deshacer. Localmente,
`solercia-connect disconnect bitbucket <cuenta>` borra el perfil y el token guardado. El token se
revoca en id.atlassian.com → Security → API tokens, porque Connect no revoca nada en el proveedor.

## Referencias oficiales

- [The Bitbucket Cloud REST API — Authentication, scopes, pagination and filtering](https://developer.atlassian.com/cloud/bitbucket/rest/intro/)
- [Create an API token — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/)
- [API token permissions — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/)
- [API request limits — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/api-request-limits/)
- [Access tokens for a workspace — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/workspace-access-tokens/)
- [Use OAuth on Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/use-oauth-on-bitbucket-cloud/)
- [Bitbucket Cloud changelog](https://developer.atlassian.com/cloud/bitbucket/changelog/)
- [Users API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-users/)
- [Workspaces API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-workspaces/)
- [Repositories API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/)
- [Pullrequests API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/)
- [Commits API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/)
- [Especificación OpenAPI de Bitbucket Cloud](https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json)
