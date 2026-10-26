# ODD — Conector de Bitbucket Cloud (solo lectura)

- **Feature:** `bitbucket-connector`
- **Rama:** `feat/bitbucket-connector` (worktree
  `~/Proyectos/SOLERCIA/solercia-connect-worktrees/bitbucket-connector`)
- **Base:** `feat/google-jira-fallback` en `7c2b8fb`, que todavía no está integrada en `main`.
- **Espejo en Engram:** `odd/bitbucket-connector/tasks` (proyecto `solercia-connect`).

## Objetivo

Solercia Connect lee Bitbucket Cloud por la API oficial, con cualquier cuenta y en modo proyecto,
igual que Jira. El primer caso de uso es el workspace `idartes-tecnologia`: el `origin` de
`IDARTES/pandora_proxy` es `bitbucket.org/idartes-tecnologia/pandora_proxy`.

## Pedido

Camilo, 2026-10-26: «dime si bitbucket tiene una integración similar para hacerla tambien con
solercia-connect» y, después, «tu preocupate por conectarte a bitbucket».

El primer corte es de solo lectura: conexión e identidad, repositorios, pull requests y commits. Se
lo propuso el orquestador a Camilo, quien no lo objetó. Cualquier escritura en Bitbucket necesita su
propia ADR.

## Evidencia (investigación del 2026-10-26)

| Hecho | Fuente |
| --- | --- |
| La autenticación es Basic con el correo de Atlassian y un API token. Desde el 18-08-2026 también acepta Bearer. | developer.atlassian.com/cloud/bitbucket/rest/intro; changelog |
| El token debe tener alcances de Bitbucket, así que el token clásico de Jira no sirve. | support.atlassian.com/bitbucket-cloud/docs/create-an-api-token |
| Alcances de lectura: `read:user:bitbucket`, `read:workspace:bitbucket`, `read:repository:bitbucket` y `read:pullrequest:bitbucket`. No se implican entre sí. | especificación OpenAPI `x-atlassian-oauth2-scopes` |
| Los app passwords quedaron retirados del todo el 28-07-2026. | changelog del 29-05-2026 |
| OAuth no admite PKCE (no está documentado), exige secreto y el consumidor lo crea un administrador del workspace. | intro; OAuth 2.0 changes (04-05-2026) |
| Los tokens de acceso de workspace o proyecto no van ligados a una persona y requieren el plan Premium. | support.atlassian.com/bitbucket-cloud/docs/workspace-access-tokens |
| Identidad: `GET /2.0/user` devuelve `account_id` y `uuid`; `GET /2.0/user/emails` lista los correos. | api-group-users |
| `GET /2.0/repositories` (entre workspaces) se retiró el 14-04-2026. Ahora se usa `/2.0/repositories/{workspace}`. | changelog |
| Las PR aceptan `state` repetible y `q=updated_on >= … AND updated_on < …`. | intro, sección «Filtering» |
| Los commits no tienen filtro de fecha: el orden es topológico y la ventana se filtra en el cliente. | especificación |
| Paginación con el enlace `next` y `pagelen` de máximo 100. Límite de 1.000 solicitudes por hora en `/2.0/repositories/*`. | intro; api-request-limits |

## Decisiones

| ID | Decisión | Estado |
| --- | --- | --- |
| B-D0 | Primer corte de solo lectura y solo por la ruta oficial. Sin respaldo por navegador. | Propuesto por el orquestador el 2026-10-26 y no objetado |
| B-D1 | Autenticación con un API token con alcances de Bitbucket, por Basic `email:token`, guardado por cuenta como en Jira. OAuth y los tokens de acceso quedan diferidos. | Decidido por el orquestador a partir de la evidencia |
| B-D2 | La prueba de identidad combina tres comprobaciones: `/2.0/user` con `account_id` fijo, `/2.0/user/emails` con el correo de la cuenta confirmado, y acceso a `/2.0/workspaces/{workspace}`. | Decidido por el orquestador |

## Restricciones

- No se leen ni se imprimen `.env`, `connect.env`, tokens ni secretos.
- Las pruebas no hacen llamadas de red. La validación en vivo la hace Camilo.
- TDD estricto, con un commit por tarea en español, scope `solercia` y sin atribución. Sin push ni
  PR.
- Se trabaja en paralelo con T8.3 (`feat/jira-open-issues`, de otra sesión). Se integra primero
  T8.3 y después se rebasa esta rama.

## Modo TDD

- **Modo:** estricto (`openspec/config.yaml:11`, `:31-33`).
- **Runner:** `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`.
- **Compuertas:** `ruff format --check`, `ruff check`, `mypy src` y cobertura ≥ 80.

## Tareas

- [ ] **B0** Escribir la ADR `docs/decisions/2026-10-26-conector-bitbucket.md`.
  - Registra la familia nueva `BITBUCKET`, de solo lectura, oficial y sin navegador.
  - Actualizar el registro de plataforma (§1 y la lista de productos), `AGENTS.md`, `SECURITY.md` y
    el skill.
- [ ] **B1** Conexión por cuenta: `connect bitbucket <cuenta> --workspace <ws>`, con
  `--token-env`, prompt oculto o `--token-stdin`.
  - El token se guarda en el almacén de secretos con la clave `bitbucket:<email>`. El perfil guarda
    el workspace y el `account_id`.
  - La identidad se prueba según B-D2.
  - `status` y `disconnect` también funcionan.
  - El modo proyecto funciona sin cambios.
- [ ] **B2** `scm.repositories.list`: los repositorios del workspace con `role=member`, paginados con
  tope.
- [ ] **B3** `scm.pullrequests.list --repo <slug>`: estados repetibles y ventana por `updated_on` con
  `q`, verificada también en el cliente.
- [ ] **B4** `scm.commits.list --repo <slug> [--branch]`.
  - La ventana se filtra en el cliente, con un margen de 7 días y un tope de páginas.
  - Si se alcanza el tope, devuelve `complete=false`.
- [ ] **B5** Documentación y cierre:
  - `docs/runbooks/bitbucket.md`, matriz, README y skill;
  - doctor;
  - plantilla de `project init` con `BITBUCKET_API_TOKEN`.

## Criterios de aceptación

- Con un token sin alcances o con alcances que no alcanzan, la conexión falla con un error tipado
  (401 o 403) y no guarda nada.
- Un correo que no coincida o no esté confirmado, o un `account_id` distinto del guardado, falla de
  forma cerrada.
- Las lecturas siguen solo el enlace `next`, tienen tope de páginas y salen en orden determinista,
  sin truncar en silencio.
- Ningún token aparece en perfiles, salidas o mensajes.

## Progreso y evidencia

| Tarea | Estado | Evidencia |
| --- | --- | --- |
| Investigación | Hecha | Fuentes oficiales, 2026-10-26 |

## Siguiente paso

B0 a B5, a cargo de un redactor.

Mientras tanto, Camilo crea el token en id.atlassian.com con la opción «Create API token with
scopes», eligiendo la app Bitbucket y los cuatro alcances de lectura.
