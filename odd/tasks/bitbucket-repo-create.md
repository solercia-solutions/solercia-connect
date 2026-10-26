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
- [ ] **R2** Capacidad `scm.repositories.create --repo <slug> --project <clave> --allow-write`, con
  las reglas de R-D2.
  - Errores tipados: `repository_exists`, proyecto inexistente y permiso denegado.
  - Se rechaza por HTTP.
- [ ] **R3** Runbook, matriz, README y skill, con el ejemplo de push por `git`. Después, compuertas
  completas, reinstalación global e instrucciones a `migracion-angular-pandora`.

## Criterios de aceptación

- Sin `--allow-write`, con la conexión de lectura o por HTTP, no se hace ninguna llamada de
  escritura.
- Un repositorio existente nunca se modifica.
- No se puede crear un repositorio público ni uno sin proyecto explícito.
- Ningún token aparece en perfiles, salidas, URL de clonación ni mensajes.

## Entrega

- Tamaño previsto: entre 700 y 900 líneas, con pruebas y documentos.
- Un commit por tarea en esta rama, sin PR, igual que en las ramas anteriores.

## Progreso y evidencia

| Tarea | Estado | Evidencia |
| --- | --- | --- |
| Exploración | Hecha | El push ya funciona por git; el alcance se verificó en OpenAPI |
| R0 | Hecha | `df0d706`: ADR y documentos de política; rojo con 3 fallos en `test_bitbucket_repo_create_policy_docs.py` (ADR inexistente, AGENTS.md sin «four» y skill sin las reglas nuevas) |
| R1 | Hecha | Conexión `bitbucket-admin`; rojo por `ImportError` de `BITBUCKET_TARGETS` en `test_bitbucket_admin_connection.py` y 2 fallos en las líneas agregadas a pruebas existentes |

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

## Siguiente paso

R2, la capacidad `scm.repositories.create`.
