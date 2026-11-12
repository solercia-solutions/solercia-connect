# Decisión: creación de repositorios en Bitbucket Cloud

- **Estado:** Aceptado
- **Fecha de decisión:** 2026-10-25
- **Decidido por:** Camilo Calderón, propietario del producto (R-D0), y el orquestador, que aplicó
  los precedentes ya aceptados (R-D1, R-D2 y R-D3)
- **Ámbito:** Regla de escritura acotada, conexión `bitbucket-admin`, API REST oficial de Bitbucket
  Cloud y servicio HTTP
- **Aplicación:** Obligatoria para el target `bitbucket-admin` y la capacidad
  `scm.repositories.create`
- **Actualiza:** [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisiones 1.1 y 3.9), [`SECURITY.md`](../../SECURITY.md), [`AGENTS.md`](../../AGENTS.md) y el
  skill [`agents/skills/solercia-connect/SKILL.md`](../../agents/skills/solercia-connect/SKILL.md)
- **Complementa:** [`2026-10-26-conector-bitbucket.md`](2026-10-26-conector-bitbucket.md), el
  conector de solo lectura, y [`2026-10-25-envio-correo-gmail-dos-pasos.md`](2026-10-25-envio-correo-gmail-dos-pasos.md),
  el precedente de una conexión separada para escribir

## Contexto

Antes de esta decisión, Solercia Connect admitía exactamente tres escrituras, y Bitbucket Cloud era
de solo lectura. Las reglas vigentes lo dicen de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| `AGENTS.md`, encabezado | «It has exactly three bounded writes […] Bitbucket Cloud is read-only and official-only» |
| `2026-10-26-conector-bitbucket.md`, B-D0 | «No escribe nada en Bitbucket. […] cualquier escritura en Bitbucket necesita su propia ADR.» |
| Registro de plataforma, decisión 3.9 | «Ninguna otra escritura queda autorizada.» |

El pedido quedó registrado en el documento de la funcionalidad
(`odd/tasks/bitbucket-repo-create.md`, sección «Pedido»):

> Camilo, 2026-10-25: «mandale las instrucciones para conectarte a bitbucket al agente llamado
> migracion-angular-pandora para que cree unos repos y luego les haga push».

Ante la pregunta de cómo crear los repositorios, Camilo eligió «Agregarlo a solercia-connect». El
primer uso es la sesión `migracion-angular-pandora`, que necesita repositorios nuevos en el
workspace `idartes-tecnologia`. El push ya funciona sin Connect: el checkout de `pandora_proxy` usa
HTTPS con el usuario `x-bitbucket-api-token-auth` y la credencial que `git` guarda por su cuenta,
según se revisó con `git remote get-url origin` y `git config`, sin imprimir credenciales.

## Decisión

### R-D0. Una escritura nueva y acotada

1. `scm.repositories.create` crea un repositorio git privado y vacío dentro de un proyecto explícito
   de un workspace. Es la cuarta escritura acotada de Solercia Connect, después de las categorías de
   Outlook, la transición de Jira y el envío de Gmail en dos pasos.
2. No borra, no renombra, no transfiere ni modifica repositorios existentes. No cambia permisos,
   ramas, restricciones, webhooks, proyectos ni la configuración del workspace, y no hace push.
3. Cualquier otra escritura en Bitbucket sigue necesitando su propia ADR.

### R-D1. Conexión separada `bitbucket-admin`

1. La escritura corre solo sobre un target propio, `bitbucket-admin`, del proveedor
   `bitbucket.cloud`, que ofrece una sola capacidad: `scm.repositories.create`. El target
   `bitbucket` sigue siendo de solo lectura, con sus tres capacidades, igual que `gmail` frente a
   `gmail-compose`.
2. La conexión usa su propio API token de la cuenta de Atlassian, creado con «Create API token with
   scopes» para la app Bitbucket, con exactamente cinco alcances: `read:user:bitbucket`,
   `read:workspace:bitbucket`, `read:repository:bitbucket`, `read:project:bitbucket` y
   `admin:repository:bitbucket`. Viaja por Basic, con el correo de la cuenta como usuario.
3. El token se guarda en el almacén de secretos con el servicio de llavero
   `solercia-connect.api-token` y la clave `bitbucket-admin:<email>`; en modo proyecto, en el archivo
   privado equivalente. La clave no colisiona con `bitbucket:<email>`, así que cada conexión conserva
   su token. El perfil no secreto guarda el workspace por defecto y el `account_id` observado.
4. La identidad se prueba igual que en B-D2, al conectar, al consultar el estado y antes de la
   escritura: `GET /2.0/user`, `GET /2.0/user/emails` y `GET /2.0/workspaces/{workspace}`.
5. El token se lee de una variable del entorno efectivo (`--token-env`, por ejemplo
   `BITBUCKET_ADMIN_API_TOKEN` en el `connect.env` del modo proyecto), del prompt oculto o de una
   línea de stdin (`--token-stdin`). Nunca es un argumento, nunca viaja por HTTP y nunca se imprime.

### R-D2. Reglas de la escritura

1. **Autorización por invocación.** Cada invocación exige `--allow-write`, apagada por defecto. Sin
   ella la orden falla con `write_not_authorized` antes de construir el registro de proveedores o
   llamar a Bitbucket. Ninguna variable de entorno ni perfil la sustituye.
2. **Solo la CLI y la ruta oficial.** El servicio HTTP la rechaza con 403 antes de ejecutar el
   servicio de aplicación. No tiene ruta de navegador y `--allow-browser-fallback` se rechaza como
   entrada inválida. Con la conexión de lectura `bitbucket` la capacidad no existe.
3. **Repositorio explícito.** `--repo` es obligatorio. El slug se valida tal como llega, sin
   convertirlo: solo minúsculas, dígitos, `.`, `_` y `-`, sin `.` ni `..` como slug completo, y sin
   mayúsculas, espacios, `/` ni llaves, así que nunca se acepta un UUID entre llaves. No se impone
   una longitud máxima, porque la especificación no la declara.
4. **Proyecto explícito.** `--project` es obligatorio y no tiene valor por defecto. Es una clave de
   proyecto de letras, dígitos y `_`; no se acepta el UUID del proyecto.
5. **Orden fijo y cerrado ante fallos.** Primero se prueba la identidad. Después se consulta el
   repositorio: si responde 200, la orden falla con `repository_exists` y no toca nada; si responde
   404, continúa. Luego se consulta el proyecto: un 404 falla con `project_not_found`, y una clave
   devuelta distinta de la pedida falla antes de escribir. Solo entonces se envía un único `POST`.
6. **Cuerpo exacto.** El `POST` lleva solo
   `{"scm": "git", "is_private": true, "fork_policy": "no_public_forks", "project": {"key": "<clave>"}}`.
   No se puede crear un repositorio público, uno con bifurcaciones públicas ni uno sin proyecto.
7. **Verificación posterior.** La respuesta debe traer el mismo slug, `is_private` verdadero, la
   misma clave de proyecto y el mismo workspace. Si algo no coincide, la orden falla cerrada con
   `write_unverified` y su mensaje dice que el repositorio sí quedó creado y que se debe revisar en
   Bitbucket.
8. **Sin reintentos.** El `POST` nunca se repite. Un error `5xx`, un tiempo de espera, una red no
   disponible o una respuesta ilegible después del `POST` fallan con `write_unverified`, que advierte
   que el repositorio pudo haberse creado. Antes de cualquier nuevo intento se revisa Bitbucket.
9. **Errores saneados.** Un 400 del `POST` se informa como `request_rejected` con un mensaje fijo;
   ningún error cita el cuerpo que devuelve Bitbucket. Un 401 es `invalid_credentials`, un 403 es
   `authorization_denied` (con los nombres de alcance que traiga la respuesta, que no son secretos)
   y un 404 del workspace es `workspace_not_found`.

### R-D3. El push queda fuera de Solercia Connect

1. Connect no ejecuta `git`, no guarda credenciales de `git` y no hace push.
2. Después de crear el repositorio, el push lo hace `git` con la credencial que ya tiene guardada:
   `git remote add origin https://x-bitbucket-api-token-auth@bitbucket.org/<workspace>/<slug>.git`
   y `git push -u origin <rama>`.
3. La URL de clonación HTTPS del registro se entrega sin usuario ni contraseña, aunque Bitbucket la
   devuelva con usuario.

## Contrato de la capacidad

### Conexión `bitbucket-admin`

```bash
solercia-connect connect bitbucket-admin <cuenta> --workspace <workspace> --token-env BITBUCKET_ADMIN_API_TOKEN
solercia-connect status bitbucket-admin <cuenta>
solercia-connect disconnect bitbucket-admin <cuenta>
solercia-connect doctor bitbucket-admin <cuenta>
```

### Escritura: `scm.repositories.create`

```bash
solercia-connect run --provider bitbucket.cloud --capability scm.repositories.create \
  --account <cuenta> --repo <slug> --project <CLAVE> --allow-write [--workspace <workspace>]
```

| Opción | Regla |
| --- | --- |
| `--account` | Obligatoria. |
| `--repo` | Obligatoria. Un slug en minúsculas. |
| `--project` | Obligatoria. Una clave de proyecto, sin valor por defecto. |
| `--workspace` | Opcional. Sin ella se usa el workspace de la conexión. |
| `--allow-write` | Obligatoria para escribir; apagada por defecto. |

El registro devuelto contiene `workspace`, `slug`, `full_name`, `project_key`, `is_private`,
`web_url`, `clone_https` (sin usuario), `created_at` y la procedencia, junto con el recibo de
ejecución habitual.

### Errores tipados

| Código | Cuándo |
| --- | --- |
| `write_not_authorized` | Falta `--allow-write`; no se hizo ninguna llamada. |
| `invalid_input` | Slug, clave de proyecto o cuenta inválidos, o una clave de proyecto distinta de la que Bitbucket registra. |
| `repository_exists` | Ya existe un repositorio con ese slug; no se tocó. |
| `project_not_found` | El proyecto no existe en el workspace o la cuenta no lo ve. |
| `workspace_not_found` | El workspace no existe o la cuenta no pertenece a él. |
| `invalid_credentials` | El token venció o no es un API token con alcances de Bitbucket. |
| `authorization_denied` | Faltan alcances, o la cuenta no puede leer un repositorio privado que ya ocupa ese slug. |
| `authorization_boundary_violation` | La identidad no coincide con la conexión. |
| `request_rejected` | Bitbucket rechazó la creación (entrada inválida o sin privilegio para crear). |
| `write_unverified` | No hubo confirmación legible, o la respuesta no coincide con lo pedido. |

## Rutas, endpoints y alcances

Las citas siguientes son textuales, en el idioma de la fuente, tomadas de la especificación OpenAPI
publicada de Bitbucket Cloud (campos `description` y `x-atlassian-oauth2-scopes`) el 2026-10-25. El
análisis va aparte.

### Crear el repositorio

- `POST /repositories/{workspace}/{repo_slug}`, alcance `admin:repository:bitbucket`: «Creates a
  new repository.»
- «The project must be assigned for all repositories. If the project is not provided,
  the repository is automatically assigned to the oldest project in the workspace.»
- Cuerpo: «The repository that is to be created. Note that most object elements are optional.
  Elements "owner" and "full_name" are ignored as the URL implies them.»
- Respuestas documentadas: 200, «The newly created repository.»; 400, «If the input document was
  invalid, or if the caller lacks the privilege to create repositories under the targeted
  account.»; 401, «If the request was not authenticated.»
- `fork_policy`: «**no_public_forks**: restrict forking to private forks (forks cannot be made
  public later)».
- Parámetro `repo_slug`: «This can either be the repository slug or the UUID of the repository,
  surrounded by curly-braces». El parámetro es una cadena sin patrón ni longitud máxima declarados.

### Consultar el repositorio antes de crear

- `GET /repositories/{workspace}/{repo_slug}`, alcance `read:repository:bitbucket`: «Returns the
  object describing this repository.»
- 403: «If the repository is private and the authenticated user does not have access to it.»
- 404: «If no repository exists at this location.»

### Consultar el proyecto antes de crear

- `GET /workspaces/{workspace}/projects/{project_key}`, alcance `read:project:bitbucket`: «Returns
  the requested project.»
- Parámetro `project_key`: «The project in question. This is the actual `key` assigned to the
  project.»
- 403: «The requesting user isn't authorized to access the project.»; 404: «A project isn't hosted
  at this location.»

### Identidad

La prueba de identidad reutiliza los endpoints de B-D2, con los alcances `read:user:bitbucket` y
`read:workspace:bitbucket`. La ADR del conector cita además: «Unlike OAuth 2.0 scopes, Forge app and
API token scopes do not implicitly grant access to other scopes» (*The Bitbucket Cloud REST API*,
«Forge app and API token scopes»).

Lo anterior implica que la conexión necesita exactamente los cinco alcances de R-D1, y que
`admin:repository:bitbucket` no permite por sí solo consultar el repositorio ni el proyecto. Como un
proyecto omitido termina en el proyecto más antiguo, la clave es obligatoria. Como el 400 mezcla una
entrada inválida con la falta de privilegio, se informa con un mensaje fijo y sin el cuerpo. Un 403
en la consulta previa puede significar que ya existe un repositorio privado que la cuenta no ve; en
ese caso no se crea nada.

## Estado de implementación

Las pruebas son offline y no hacen llamadas de red. La creación en vivo contra
`idartes-tecnologia` la hace Camilo, o el agente con su autorización expresa. Quedan por validar en
vivo la forma exacta del 400, la sensibilidad a mayúsculas de la clave de proyecto y el enlace de
clonación que devuelve Bitbucket.

## Límites que no cambian

- El target `bitbucket` sigue siendo de solo lectura, con los cuatro alcances de B-D1 y sin cambios
  en sus perfiles, tokens ni capacidades.
- Ninguna escritura corre por HTTP ni por navegador, y ninguna se reintenta.
- Connect no hace push, no administra permisos y no borra repositorios.

## Riesgos

- **Token más poderoso.** `admin:repository:bitbucket` permite administrar repositorios, más de lo
  que Connect usa. El código solo emite el `POST` de creación con el cuerpo fijo de R-D2, y el token
  vive aparte del de lectura, bajo `bitbucket-admin:<email>`. Dura como máximo un año.
- **Carrera entre la consulta y la creación.** Si otra persona crea el mismo slug entre la consulta
  y el `POST`, Bitbucket lo rechaza y la orden falla sin modificar el repositorio ajeno.
- **Reglas de Bitbucket no declaradas.** La especificación no fija el patrón del slug ni el de la
  clave de proyecto. Las reglas de R-D2 pueden ser más estrictas de lo necesario; un slug
  válido para Bitbucket pero fuera de esa regla se rechaza antes de llamar.
- **Respuesta que no coincide.** Si Bitbucket crea algo distinto de lo pedido, la orden falla con
  `write_unverified` y pide revisarlo; Connect no intenta corregirlo ni borrarlo.

## Consecuencias

- Los agentes crean repositorios privados con el mismo sobre, procedencia y recibo que el resto de
  Connect, y hacen el push con `git`.
- Una creación hace seis solicitudes: tres de identidad, dos consultas previas y el `POST`.
- El modo proyecto funciona sin cambios: el token se guarda en el archivo privado de secretos y
  `--token-env BITBUCKET_ADMIN_API_TOKEN` lo lee de `connect.env`.

## Plan de reversión

Retirar el target `bitbucket-admin` de `TARGET_SPECS` y su adaptador del arranque deja sin ruta la
capacidad; no hay migraciones ni datos compartidos que deshacer. Localmente,
`solercia-connect disconnect bitbucket-admin <cuenta>` borra el perfil y el token guardado. El token
se revoca en id.atlassian.com → Security → API tokens. Los repositorios ya creados no se borran desde
Connect.

## Alternativas descartadas

- **Agregar `admin:repository:bitbucket` al token de `bitbucket`.** Rompería el solo lectura de B-D0
  y daría poder de escritura a cada lectura. El precedente de `gmail-compose` separa la escritura.
- **Hacer el push desde Connect.** Exigiría guardar o invocar credenciales de `git`; `git` ya lo hace
  con su propio almacén (R-D3).
- **Proyecto por defecto.** Bitbucket asignaría el proyecto más antiguo, que casi nunca es el
  correcto.
- **Visibilidad o política de bifurcación configurables.** Abrirían la puerta a un repositorio
  público; se fijan en privado y `no_public_forks`.
- **Tokens de acceso de workspace o de proyecto.** Exigen el plan Premium y no prueban a una persona,
  como ya registró la ADR del conector.
- **Ruta de navegador.** La API oficial cubre la creación; un navegador añadiría superficie sin
  necesidad.

## Referencias oficiales

- [Especificación OpenAPI de Bitbucket Cloud](https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json)
- [Repositories API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-repositories/)
- [Projects API group](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-projects/)
- [The Bitbucket Cloud REST API — Authentication and scopes](https://developer.atlassian.com/cloud/bitbucket/rest/intro/)
- [Create an API token — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/)
- [API token permissions — Bitbucket Cloud](https://support.atlassian.com/bitbucket-cloud/docs/api-token-permissions/)
