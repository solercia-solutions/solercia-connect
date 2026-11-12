# ODD — Historias abiertas de Jira en Solercia Connect

- **Feature:** `jira-open-issues` (tarea T8.3 de `google-jira-browser-fallback`)
- **Rama:** `feat/jira-open-issues` (worktree `~/Proyectos/SOLERCIA/solercia-connect-worktrees/jira-open-issues`)
- **Base:** `feat/google-jira-fallback` en `7c2b8fb`.
- **Espejo en Engram:** `odd/jira-open-issues/tasks` (proyecto `solercia-connect`).

## Objetivo

Que Solercia Connect liste las historias de Jira que la cuenta conectada aún tiene abiertas, por
ejemplo las del sprint actual. Hoy `external.issues.search` solo devuelve issues resueltos dentro de
una ventana de fechas, así que no responde «qué me falta terminar».

## Alcance

- Capacidad nueva de solo lectura `external.issues.open.list` para el destino `jira`, solo por la
  ruta oficial. No entra en la lista de capacidades de navegador de Jira (`runtime.py`).
- Entradas: `--account` (opcional, igual que la búsqueda), `--project <CLAVE>` validado con
  `^[A-Z][A-Z0-9_]+$`, `--sprint current`, `--jql` (reemplaza el filtro por defecto; vacío o solo
  espacios se rechaza) y `--limit` hasta 100. Sin ventana de fechas.
- JQL por defecto, determinista: `assignee = currentUser() AND statusCategory != Done`, luego
  `project = "<CLAVE>"` y `sprint in openSprints()` si vienen, y `ORDER BY updated DESC`.
- Mismo endpoint `/rest/api/3/search/jql`, misma paginación con tope de 20 páginas y misma
  verificación de identidad (`/rest/api/3/myself`) que la búsqueda.
- Campos pedidos: `summary`, `status` (trae la categoría), `issuetype`, `priority`, `updated`,
  `assignee`.
- Registro `OpenIssue`: `key`, `summary`, `status`, `status_category`, `issue_type`, `priority`,
  `updated_at`, `assignee_account_id` y `provenance`.

## Restricciones

- Sin red: las pruebas usan `FakeTransport`; no se corre `-m live` ni se llama al Jira real.
- Las pruebas existentes no se modifican; solo se agregan líneas en el ciclo de
  `tests/test_cli_lifecycle.py` que recorre todas las capacidades.
- Un solo commit, sin push, sin PR y sin reinstalar la herramienta global.

## Decisiones

- **`--jql` no se combina con `--project` ni `--sprint`.** Como `--jql` reemplaza el filtro entero,
  mezclarlos sería ambiguo; se rechaza con `invalid_input` en lugar de ignorar una de las dos.
- **La clave de proyecto va entre comillas en la JQL** (`project = "CP"`). La expresión regular ya
  impide inyectar JQL; las comillas evitan que una clave que coincida con una palabra de JQL (por
  ejemplo `AND` o `IN`) rompa la consulta.
- **`status_category` usa la clave estable** (`new`, `indeterminate`), igual que los registros de
  transiciones, y no el nombre traducido.
- **`--assignee`, `--start` y `--end` se rechazan** en esta capacidad: la lectura es de la cuenta
  conectada y no tiene ventana. `--project` y `--sprint` se rechazan en cualquier otra capacidad.
- **`--limit` se recorta a 100**, igual que en la búsqueda.
- **La paginación se comparte** con `external.issues.search` en `JiraAdapter._search_jql`; la
  búsqueda conserva su filtro por ventana de resolución (prueba de caracterización incluida).

## Modo TDD

- **Modo:** estricto, activo (`openspec/config.yaml:11` y `:31-33`).
- **Runner:** `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`.

## Tareas

- [x] **J1** Dominio y adaptador: `OpenIssueQuery`, `OpenIssue`, `JiraAdapter.list_open_issues`
  y paginación compartida.
- [x] **J2** Superficie: destino `jira`, aplicación, CLI (`--project`, `--sprint`), ayudante
  heredado `_query_payload` y fila de la matriz con estado `implemented-offline`.
- [x] **J3** Documentación: `README.md`, `docs/runbooks/jira.md` y
  `agents/skills/solercia-connect/SKILL.md`.

## Evidencia

- **Línea base:** `1212 passed, 4 deselected`.
- **ROJO J1** (`pytest -m 'not live' -q tests/test_jira_open_issues.py`):
  `ImportError: cannot import name 'OpenIssue' from 'solercia_connect.domain'`.
- **VERDE J1:** `157 passed` entre las pruebas nuevas y las de búsqueda de Jira existentes.
- **ROJO J2** (`pytest ... tests/test_jira_open_issues.py tests/test_cli_lifecycle.py`):
  `25 failed, 65 passed`; por ejemplo
  `Capability external.issues.open.list is not available for jira` y
  `solercia_connect.cli._InputError: Unsupported capability: external.issues.open.list`.
- **VERDE J2:** `90 passed`.
- **Compuertas finales:** `1288 passed, 4 deselected`; cobertura de ramas 88,58 % (mínimo 80);
  `ruff format --check .`, `ruff check .` y `mypy src` sin hallazgos.

## RDD

`gentle-ai review mode status`: apagado (decidido por la configuración global). Rigen la política
ordinaria del repositorio y las compuertas funcionales.

## Brechas

- La API HTTP (`POST /v1/run`) atiende la capacidad con el filtro por defecto o `jql`, pero su
  modelo aún no expone `project` ni `sprint`.
- Sin prueba en vivo: el estado queda en `implemented-offline`.

## Siguiente paso

Prueba en vivo de solo lectura con la cuenta de Jira de Camilo, cuando él la autorice, y decidir si
el modelo HTTP expone `project` y `sprint`.
