# ODD — Creación de repositorios en Bitbucket Cloud

- **Feature:** `bitbucket-repo-create`
- **Rama:** `feat/bitbucket-repo-create` (worktree
  `~/Proyectos/SOLERCIA/solercia-connect-worktrees/bitbucket-connector`)
- **Base:** `feat/bitbucket-connector` en `4411a92`, que todavía no está integrada en `main`.
- **Espejo en Engram:** `odd/bitbucket-repo-create/tasks` (proyecto `solercia-connect`).

## Objetivo

Solercia Connect crea repositorios privados en Bitbucket Cloud por la API oficial, como una
escritura acotada más. El primer uso es la sesión `migracion-angular-pandora`, que necesita crear
repositorios en `idartes-tecnologia` y luego hacerles push.

## Pedido

Camilo, 2026-10-25: «mandale las instrucciones para conectarte a bitbucket al agente llamado
migracion-angular-pandora para que cree unos repos y luego les haga push».

El conector de Bitbucket es de solo lectura (B-D0 de `bitbucket-connector`). Ante la pregunta de
cómo crear los repositorios, Camilo eligió «Agregarlo a solercia-connect».

## Evidencia

| Hecho | Fuente |
| --- | --- |
| `POST /2.0/repositories/{workspace}/{repo_slug}` exige el alcance `admin:repository:bitbucket`. | Especificación OpenAPI de Bitbucket Cloud, `x-atlassian-oauth2-scopes`, consultada el 2026-10-25 |
| Los alcances de Bitbucket no se implican entre sí. | Investigación de `bitbucket-connector` |
| El cuerpo admite `scm`, `is_private`, `project.key` y `fork_policy`. Si se omite el proyecto, Bitbucket asigna uno por su cuenta. | Referencia REST «Create a repository» |
| El push no pasa por la API: `pandora_proxy` usa HTTPS con el usuario `x-bitbucket-api-token-auth` y `credential.helper=store`. | `git remote get-url origin` y `git config`, revisados sin imprimir credenciales |

## Decisiones

| ID | Decisión | Estado |
| --- | --- | --- |
| R-D0 | La escritura nueva `scm.repositories.create` crea un repositorio git privado, vacío, en un proyecto de un workspace. No borra, no cambia permisos ni configuración y no hace push. | Aceptada por Camilo el 2026-10-25 |
| R-D1 | Usa una conexión aparte, `bitbucket-admin`, con su propio token. Ese token lleva cinco alcances: `read:user:bitbucket`, `read:workspace:bitbucket`, `read:repository:bitbucket`, `read:project:bitbucket` y `admin:repository:bitbucket`. La conexión `bitbucket` sigue siendo de solo lectura, igual que `gmail` frente a `gmail-compose`. | Decidido por el orquestador, siguiendo ese precedente; `read:project:bitbucket` se agregó en R0 para la consulta previa del proyecto |
| R-D2 | Reglas de la escritura: ver el detalle debajo de esta tabla. | Decidido por el orquestador |
| R-D3 | El push queda fuera de Solercia Connect: lo hace `git` con la credencial que ya tiene guardada. | Decidido por el orquestador |

Reglas de la escritura (R-D2):

- Se requiere `--allow-write` en cada invocación. Nunca corre por HTTP y nunca se reintenta.
- `--repo` (el slug) es obligatorio y se valida en minúsculas.
- La clave del proyecto de Bitbucket es obligatoria y no tiene valor por defecto.
- Siempre se fuerzan `is_private=true`, `scm=git` y `fork_policy=no_public_forks`.
- Antes de crear se consulta si el repositorio existe. Si existe, falla con `repository_exists` y no
  lo toca.
- Antes de crear se consulta el proyecto con `GET /workspaces/{workspace}/projects/{project_key}`,
  que exige `read:project:bitbucket`. Si no existe, falla con `project_not_found`.
- El slug se valida tal como llega: `[a-z0-9._-]+`, sin `.` ni `..`, sin mayúsculas, espacios, `/`
  ni llaves, y sin longitud máxima, porque la especificación OpenAPI no declara patrón ni longitud.
  La clave de proyecto es solo una clave (letras, dígitos y `_`), nunca un UUID.
- Un 400 del `POST` se informa como `request_rejected` con un mensaje fijo, sin citar el cuerpo.
- Después de crear se verifica la respuesta. Si no coincide, falla de forma cerrada con
  `write_unverified` e informa que el repositorio sí quedó creado.

## Restricciones

- No se leen ni se imprimen `.env`, `connect.env`, `~/.git-credentials`, tokens ni secretos.
- Las pruebas no hacen llamadas de red. La creación en vivo la hacen Camilo o el agente con su
  autorización.
- TDD estricto, con un commit por tarea en español, scope `solercia` y sin atribución. Sin push ni
  PR. La integración en `main` es decisión de Camilo.

## Modo TDD

- **Modo:** estricto (`openspec/config.yaml:11`, `:31-33`).
- **Runner:** `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`.
- **Compuertas:** `ruff format --check`, `ruff check`, `mypy src` y cobertura ≥ 80.
- **RDD:** apagado (global), así que no se abre revisión nativa.

## Tareas

- [x] **R0** ADR `docs/decisions/2026-10-25-escritura-creacion-repositorios-bitbucket.md`.
  - Enmendar el registro de plataforma, `AGENTS.md` (de tres a cuatro escrituras acotadas),
    `SECURITY.md` y el skill.
  - Agregar la prueba de política de documentos.
- [x] **R1** Conexión `bitbucket-admin`: `connect`, `status`, `disconnect` y `doctor`.
  - El token se guarda con la clave `bitbucket-admin:<email>`.
  - La identidad se prueba igual que en B-D2.
  - La plantilla de `project init` incluye `BITBUCKET_ADMIN_API_TOKEN`.
- [x] **R2** Capacidad `scm.repositories.create --repo <slug> --project <clave> --allow-write`, con
  las reglas de R-D2.
  - Errores tipados: `repository_exists`, proyecto inexistente y permiso denegado.
  - Se rechaza por HTTP.
- [x] **R3** Runbook, matriz, README y skill, con el ejemplo de push por `git`. Después, compuertas
  completas, reinstalación global e instrucciones a `migracion-angular-pandora`.

## Criterios de aceptación

- Sin `--allow-write`, con la conexión de lectura o por HTTP, no se hace ninguna llamada de
  escritura.
- Un repositorio existente nunca se modifica.
- No se puede crear un repositorio público ni uno sin proyecto explícito.
- Ningún token aparece en perfiles, salidas, URL de clonación ni mensajes.

## Entrega

- Tamaño previsto: entre 700 y 900 líneas, con pruebas y documentos.
- Tamaño real: 2.747 líneas agregadas y 129 quitadas en 32 archivos. La mayor parte corresponde a R2
  y a sus pruebas de errores.
- Un commit por tarea en esta rama, sin PR, igual que en las ramas anteriores. Si Camilo decide
  abrir PR, conviene dividirla en partes encadenadas.

## Progreso y evidencia

| Tarea | Estado | Evidencia |
| --- | --- | --- |
| Exploración | Hecha | El push ya funciona por git; el alcance se verificó en OpenAPI |
| R0 | Hecha | `df0d706`: ADR y documentos de política; rojo con 3 fallos en `test_bitbucket_repo_create_policy_docs.py` (ADR inexistente, AGENTS.md sin «four» y skill sin las reglas nuevas) |
| R1 | Hecha | `026533b`: conexión `bitbucket-admin`; rojo por `ImportError` de `BITBUCKET_TARGETS` en `test_bitbucket_admin_connection.py` y 2 fallos en las líneas agregadas a pruebas existentes |
| R2 | Hecha | `6897b12`: capacidad `scm.repositories.create`; rojo por `ImportError` de `ScmRepositoryCreateRequest` en `test_bitbucket_repo_create.py`; cobertura de `bitbucket_admin.py` al 100 % |
| R3 | Hecha | `d3a6612`: runbook, matriz, README, guía de uso, runbook de Outlook y skill; rojo con 4 fallos en `test_bitbucket_repo_create_wiring.py`. Compuertas finales: 1.520 pruebas, cobertura 90 %, `ruff` y `mypy` limpios |
| Verificación del orquestador | Hecha | Repitió las compuertas: 1.520 pruebas, cobertura 89,52 %, `ruff` y `mypy` limpios. Las únicas líneas quitadas de pruebas existentes son las seis autorizadas y el renombre. Sin atribución en los commits. Reinstaló la CLI global y el skill. Desde `pandora_proxy`, `bitbucket-admin` queda en `action_required` y las demás conexiones en `connected_official` |

Detalle:

- **Pruebas existentes modificadas en R0, con autorización del orquestador:** la ADR cambia el
  conteo de escrituras de tres a cuatro, y esas pruebas verificaban el texto anterior.
  - `tests/test_bitbucket_policy_docs.py`, líneas 61 a 63: «three» pasó a «four» en AGENTS.md,
    SECURITY.md y el skill. La función `test_policy_documents_add_bitbucket_without_a_fourth_write`
    se renombró a `test_policy_documents_keep_bitbucket_reads_without_a_write`, porque su nombre
    dejó de ser cierto.
  - `tests/test_gmail_send_policy_docs.py`, líneas 48, 50 y 62: el mismo cambio de «three» a
    «four».
  - La prueba nueva de R0 verifica además que «exactly three bounded writes» ya no aparezca en
    AGENTS.md. Ninguna otra línea de esos archivos cambió.
- **Pruebas existentes en R1:** solo se agregaron líneas: `"bitbucket-admin"` en la lista de
  `test_standalone_foundation` y `BITBUCKET_ADMIN_API_TOKEN` en `TEMPLATE_NAMES` de
  `test_project_discovery`.
- **Conexión `bitbucket-admin` (R1):** reutiliza `connect_bitbucket` con el parámetro
  `connection_type`. Los mensajes del target `bitbucket` no cambiaron. El target nuevo no declara
  capacidades hasta R2.
- **Pruebas existentes en R2:** solo se agregaron 3 líneas en el ciclo de
  `test_legacy_query_helpers_cover_all_capability_shapes_and_provider_mapping`
  (`test_cli_lifecycle`), que salta la escritura nueva como salta las demás.
- **Capacidad (R2):**
  - El adaptador `BitbucketAdminAdapter` (`providers/bitbucket_admin.py`) hereda la prueba de
    identidad y solo ofrece la creación. El de lectura no la declara.
  - Una creación hace seis solicitudes: tres de identidad, `GET` del repositorio, `GET` del
    proyecto y un `POST`.
  - Un 403 en la consulta del repositorio es `authorization_denied` y advierte que puede existir un
    repositorio privado que la cuenta no ve. Una clave de proyecto que Bitbucket escribe distinto
    falla con `invalid_input` antes de escribir.
  - Después del `POST`, 5xx, tiempo de espera, red o respuesta ilegible dan `write_unverified`
    («may have been created»). Una respuesta que no coincide también da `write_unverified`, pero
    con «was created … review it in Bitbucket».
  - Los enlaces `web_url` y `clone_https` se reconstruyen como `https://bitbucket.org/<ruta>`; se
    descartan los de otro host o esquema, y nunca llevan usuario ni contraseña.
  - `--project` sirve para `external.issues.open.list` y para `scm.repositories.create`. `--sprint`
    sigue siendo solo de Jira.
- **Documentos (R3):** el runbook y el skill están en inglés, como el resto de esos archivos; la
  guía `docs/USO-CON-AGENTES.md` está en español. La fila de la matriz usa el proveedor
  «Bitbucket Cloud REST 2.0 (bitbucket-admin)», así que la prueba existente de las tres lecturas no
  cambió.

## Siguiente paso

- Camilo crea el token `bitbucket-admin` con los cinco alcances, lo agrega como
  `BITBUCKET_ADMIN_API_TOKEN` y conecta.
- `migracion-angular-pandora` ya recibió las instrucciones. Creará `pandora_auth` y
  `pandora_auth_guard` en el proyecto `PAN` y les hará push con git.
- Falta confirmar en vivo:
  - la forma del 400;
  - si la clave de proyecto distingue mayúsculas;
  - el enlace de clonación.
