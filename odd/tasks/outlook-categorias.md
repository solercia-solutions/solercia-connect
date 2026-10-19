# ODD — Categorías de Outlook en Solercia Connect

- **Feature:** `outlook-categorias`
- **Rama:** `feat/outlook-categorias` (worktree `~/Proyectos/SOLERCIA/solercia-connect-worktrees/outlook-categorias`)
- **Base:** `feat/http-interactive-login` en `9b67f63`. Esa rama no está integrada en `main`
  (21 commits por delante y 0 por detrás de `origin/main` el 2026-10-18), así que se parte de
  ella para no perder el comportamiento que hoy usa el CLI instalado.
- **Espejo en Engram:** `odd/outlook-categorias/tasks` (proyecto `solercia-connect`).

## Objetivo

Que Solercia Connect exponga las categorías de Outlook de cada correo, para que `soporte-kb`
conozca el estado de cada caso del buzón `pandora@idt.gov.co` sin depender de las antiguas
etiquetas de Gmail (`SOPORTE/RESUELTO`, `SOPORTE/PENDIENTE`, `SOPORTE/EN ESPERA`,
`SOPORTE/NIVEL 1`, `SOPORTE/NIVEL 2`).

## Pedido

Camilo, 2026-10-18: «que Solercia Connect pudiera extraer esas categorías y tanto leerlas como
ponerlas». El pedido se descompone en tres capacidades:

1. **Leer:** `categories` en cada registro de `mail.inbox.list` y `mail.sent.list`, por Graph y por
   la ruta de navegador, como campo aditivo.
2. **Catálogo:** las categorías maestras del buzón con nombre y color (`mail.categories.list`).
3. **Asignar:** agregar, quitar o reemplazar categorías de un mensaje (`mail.categories.set`).

## Restricciones

- No se leen ni se imprimen `.env`, tokens, cookies ni perfiles de navegador.
- No se envían, mueven, marcan ni borran correos. La prueba en vivo es solo de lectura y solo
  sobre `pandora@idt.gov.co`.
- Prohibido probar la escritura en vivo.
- Sin push, sin PR y sin tocar otros repositorios.
- Commits con Conventional Commits en español, scope `solercia`, sin trailers ni atribución a IA.

## Política del repositorio que decide el alcance

| Regla | Texto | Efecto |
| --- | --- | --- |
| `AGENTS.md:3-5` | «It normalizes read operations into typed records with provenance and deterministic execution receipts.» | El producto es de lectura. |
| `AGENTS.md:46` | «Browser operations are read-only and use a dedicated profile, never a personal Chrome profile.» | La ruta de navegador, que es la que usa el IDT, no puede escribir. |
| `SECURITY.md:31-33` | «Use least-privilege read scopes: Microsoft `Mail.Read`, `Calendars.Read`, and the separate Teams `OnlineMeetingTranscript.Read.All` …» | La ruta oficial solo pide alcances de lectura; escribir exige `Mail.ReadWrite`. |
| `SECURITY.md:67-68` | «Built-in browser data extraction is enabled only for capabilities backed by documented read-only provider endpoints.» | Una capacidad de navegador necesita un endpoint documentado y de solo lectura. |
| `SECURITY.md:79` | «It is used only for read-only calls to documented provider endpoints.» | La autorización capturada no se puede usar para un `PATCH`. |
| `SECURITY.md:80-82` | «Do not call retired or undocumented provider endpoints.» | El catálogo no se puede leer por navegador: no hay endpoint documentado en Outlook REST v2.0. |
| `AGENTS.md:56-61` y `docs/decisions/2026-08-23-solercia-platform-decisions.md:16-18` | «A conflicting request requires an explicit new ADR or decision and an update to that record; never override it silently.» | La escritura y el cambio de alcances necesitan un ADR nuevo antes de escribir código. |

**Conclusión:** la lectura de `categories` en los registros está permitida y se implementa. El
catálogo y la escritura quedan bloqueados por política y se dejan como propuesta (B1 y B2).

El repositorio no exige SDD para capacidades nuevas (`AGENTS.md` no lo pide y `openspec/` solo
fija reglas para las fases cuando se usa), así que este trabajo sigue ODD.

## Modo TDD

- **Modo:** estricto, activo.
- **Fuente:** `openspec/config.yaml:11` («Modo TDD estricto activo para todo cambio nuevo») y
  `openspec/config.yaml:31-33` (`tdd: true`).
- **Runner:** `uv run --locked --extra dev --extra oauth --extra service pytest -m 'not live'`
  (`openspec/config.yaml:34`).
- **Checks estáticos:** `ruff format --check .`, `ruff check .`, `mypy src`
  (`openspec/config.yaml:37`); cobertura de ramas mínima de 80 %.

## Tareas

### C1. Worktree aislado y entorno propio

- [x] Worktree en `~/Proyectos/SOLERCIA/solercia-connect-worktrees/outlook-categorias`, rama
      `feat/outlook-categorias`.
- [x] `uv sync --locked --extra dev --extra oauth --extra service --extra browser` en el worktree.
- [x] Línea base: 465 pruebas aprobadas, 4 vivas excluidas.

### C2. Documento ODD y espejo en Engram

- [x] Este documento.
- [x] Copia en Engram por CLI (el MCP de Engram está caído): observación #5678, tema
      `odd/outlook-categorias/tasks`.

### C3. `categories` en los registros de correo (TDD)

- [x] RED: pruebas de normalización (`categories` y `Categories`, ausencia, valores vacíos o no
      textuales, duplicados), de proyección (`$select` de bandeja y enviados en ambas rutas), de
      paridad entre rutas y de serialización. Evidencia: 17 fallidas por las razones esperadas
      (`AttributeError: 'MailMessage' object has no attribute 'categories'`, `DID NOT RAISE`,
      `$select` sin `categories`/`Categories`).
- [x] GREEN: campo aditivo `MailMessage.categories: tuple[str, ...] = ()`, lectura en
      `normalize_mail_message` con `microsoft_categories` y `categories`/`Categories` en los
      `$select`.
- [x] REFACTOR y suite completa en verde: 479 aprobadas, 4 vivas excluidas.
- **Commit:** `7becee0 feat(solercia): expone las categorías de outlook en los correos`.

### C4. Documentación del contrato

- [x] `README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md`,
      `agents/skills/solercia-connect/SKILL.md` y `openspec/specs/mail-record-contract/spec.md`.
- [x] La prueba `test_docs_declare_categories_on_mail_records` cubre los cinco documentos.
- **Commit:** el mismo `7becee0`, porque la documentación va con el comportamiento.

### C5. Verificación estática y cobertura

- [x] `ruff format --check` (107 archivos formateados), `ruff check` sin hallazgos, `mypy src` sin
      errores en 38 archivos, cobertura de ramas 82,48 % (mínimo 80 %).

### C6. Prueba en vivo de solo lectura

- [x] `status outlook pandora@idt.gov.co --allow-browser-fallback` respondió `connected_browser`.
- [x] `mail.inbox.list` del 2026-10-11 al 2026-10-19 (fin exclusivo, −05:00), salida en
      `~/pandora/correo-idt/soporte-kb/m365/categorias-prueba/inbox-2026-10-11_2026-10-19.json`
      (modo `0600`, ruta `browser`, estado `success`).
- [x] Conteos: 18 correos; los 18 traen el campo `categories`; 3 traen alguna categoría. Único
      nombre observado: `Resuelto` (3 correos). El catálogo no se pudo consultar (B1).
- **Hallazgo para `soporte-kb`:** la categoría se llama `Resuelto`, no `SOPORTE/RESUELTO`. El mapeo
  de estados debe usar los nombres de Outlook, no los de las antiguas etiquetas de Gmail.

### B1. Catálogo `mail.categories.list` — bloqueada

- **Ruta oficial:** Graph `GET /me/outlook/masterCategories` exige `MailboxSettings.Read`
  (documentación de Microsoft Graph, «List masterCategories», sección Permissions). Agregar ese
  alcance a `TARGET_SPECS[OUTLOOK].oauth_scopes` invalida todas las concesiones oficiales de
  Outlook existentes, porque `application.py:113` y `connections.py:282-283` exigen que el token
  contenga todos los alcances del target. No es aditivo.
- **Ruta de navegador:** Outlook REST v2.0 documenta `Categories` en el mensaje, pero no publica
  un endpoint de categorías maestras; el catálogo nació en Graph. Usarlo violaría
  `SECURITY.md:80-82`.
- **Propuesta para decidir:**
  1. *Provisional, sin cambios en el conector:* `soporte-kb` deriva el catálogo de los nombres
     distintos que traen los registros. No trae colores.
  2. *Oficial:* modelo de alcances por capacidad, para que `MailboxSettings.Read` se pida solo al
     usar el catálogo y no rompa las concesiones actuales. Requiere ADR y actualizar
     `SECURITY.md:31-33`.
  3. *Navegador:* capturar una autorización de audiencia Graph emitida por la propia sesión de
     Outlook Web y llamar el endpoint documentado de Graph. Requiere ampliar la lista de hosts de
     la decisión 3.8 y `SECURITY.md:77-78`, con ADR.

### B2. Escritura `mail.categories.set` — aprobada por Camilo el 2026-10-19

- Contradice `AGENTS.md:46`, `SECURITY.md:31-33`, `SECURITY.md:79` y el propósito de lectura de
  `AGENTS.md:3-5`. Requiere ADR nuevo antes de escribir código.
- **Decisión de Camilo (2026-10-19):** el estado del caso se cambia desde el front de `soporte-kb`,
  que lo escribe en GLPI y lo refleja como categoría en Outlook. Se habilita la escritura con el
  contrato de abajo. B1 sigue por la vía provisional 1.
- **Tareas:**
  - [x] C7 ADR que actualiza `docs/decisions/2026-08-23-solercia-platform-decisions.md`,
        `SECURITY.md` y `AGENTS.md`: la escritura de categorías como única excepción a la regla de
        solo lectura, con la bandera `--allow-write` por invocación.
  - [x] C8 Implementación con TDD de `mail.categories.set` por la ruta de navegador; el servicio
        HTTP la rechaza. La ruta oficial quedó bloqueada por alcances (ver C8).
  - [ ] C9 Prueba controlada en vivo sobre un correo de prueba, con confirmación de Camilo y una
        categoría de prueba que no use el equipo.
- **Contrato propuesto para el ADR:**
  - Entrada: `--message-id`, uno de `--add`, `--remove` o `--replace` (repetibles), y una bandera
    explícita de escritura (`--allow-write`) por invocación, apagada por defecto, igual que
    `--allow-browser-fallback`.
  - Comportamiento: lee el estado actual, calcula la lista final y solo emite el `PATCH` si
    cambia; asignar una categoría que ya está no produce llamada de escritura (idempotente).
    Control de concurrencia con `ChangeKey`/ETag para no pisar una edición simultánea del equipo.
  - Salida: un registro con `message_id`, `categories` final, `changed: true|false` y el recibo.
  - Ruta oficial: `PATCH /me/messages/{id}` con `Mail.ReadWrite` (Graph, «Update message»).
  - Ruta de navegador: `PATCH https://outlook.office.com/api/v2.0/me/messages/{id}`, documentado
    en la referencia v2.0 («Update a message», alcance mínimo `mail.readwrite`); exige retirar la
    regla de solo lectura del navegador.
  - El servicio HTTP la rechaza por diseño, como cualquier operación interactiva.
- **Ajuste del contrato en C8:** `--add` y `--remove` se pueden combinar en una sola escritura,
  porque cambiar el estado de un caso es quitar uno y poner otro; `--replace` sigue siendo
  excluyente. El registro trae además `previous_categories`, para auditar y deshacer.

### C7. ADR de la escritura de categorías

- [x] `docs/decisions/2026-10-19-escritura-categorias-outlook.md`: única excepción, `--allow-write`
      por invocación, rechazo en HTTP, `PATCH` solo de categorías con `If-Match`, sin reintentos,
      citas textuales de Microsoft Graph, Outlook REST v2.0 (marcada obsoleta por Microsoft) y
      OData 4.01 para `If-Match`.
- [x] Decisión 3.9 en el registro de plataforma; `SECURITY.md` (sección «Write exception») y
      `AGENTS.md` actualizados.
- [x] Suite en verde tras el cambio documental: 479 aprobadas, 4 vivas excluidas.
- **Commit:** `ff41f84 docs(solercia): acepta la escritura de categorías de outlook`.

### C8. `mail.categories.set` (TDD)

- **Bloqueo de la ruta oficial, con evidencia:** `config.py:88` fija los alcances de `outlook` con
  `Mail.Read`; `application.py:113` y `connections.py:282-283` exigen que la concesión guardada
  contenga todos los alcances del target, y `oauth.py:250` los pide al refrescar. Agregar
  `Mail.ReadWrite` invalidaría todas las concesiones oficiales de Outlook, igual que
  `MailboxSettings.Read` en B1. No se inventó un modelo de alcances: el adaptador oficial no expone
  la capacidad, el runtime registra `official_capability_unavailable` y solo la ruta de navegador,
  con `--allow-browser-fallback`, la atiende. Una prueba fija los alcances sin `Mail.ReadWrite`.
- **`--message-id`:** el campo `id` de los registros de `mail.inbox.list` y `mail.sent.list`
  (`normalize_mail_message` lo lee de `id`/`Id` en `providers/_microsoft.py`). Distingue mayúsculas
  y cambia si el correo se mueve de carpeta.
- [x] RED 1: `ImportError: cannot import name 'MailCategoriesUpdate' from 'solercia_connect.domain'`
      al recolectar las pruebas.
- [x] RED 2, con los tipos de dominio: 42 fallidas y 492 aprobadas por las razones esperadas
      (`'PlaywrightBrowserDriver' object has no attribute 'set_mail_categories'`,
      `Capability mail.categories.set is not available for outlook`, HTTP 500 en vez de 403 porque
      el servicio llegaba a la aplicación, `DID NOT RAISE`).
- [x] GREEN: runtime (autorización y sin reintentos), adaptador y driver de navegador (`GET` de
      `Id,Categories`, `PATCH` solo de `Categories` con `If-Match` y sin redirecciones), aplicación,
      CLI, servicio HTTP (403), `TARGET_SPECS`, matriz y documentación. Se actualizaron tres
      expectativas de contrato: capacidades del doctor, carga útil de la CLI y la ayuda heredada.
- [x] Suite completa: 536 aprobadas, 4 vivas excluidas. `ruff format --check` (109 archivos),
      `ruff check` sin hallazgos, `mypy src` sin errores en 38 archivos, cobertura de ramas 83,14 %.
- **Tamaño:** unas 1 680 líneas, de ellas 1 010 de pruebas; la escritura toca dominio, runtime,
  adaptador, driver, aplicación, CLI, servicio y cinco documentos, y no se partió de forma
  artificial.
- **Commit:** `121b64a feat(solercia): asigna categorías de outlook con --allow-write`.

### C9. Prueba controlada en vivo (pendiente de Camilo)

Prohibido ejecutarla sin la confirmación de Camilo. Usa la categoría `Prueba soporte-kb`, que el
equipo no usa; nunca `SOPORTE/*` ni `Resuelto`.

1. Leer la bandeja y escoger un correo de prueba; su `--message-id` es el campo `id` del registro.
2. Agregar la categoría de prueba:

   ```bash
   SOLERCIA_CONNECT_BROWSER_FALLBACK=true \
     ~/Proyectos/SOLERCIA/solercia-connect-worktrees/outlook-categorias/.venv/bin/solercia-connect \
     run --provider microsoft.graph --capability mail.categories.set \
     --account pandora@idt.gov.co --message-id '<ID_DE_UN_CORREO_DE_PRUEBA>' \
     --add 'Prueba soporte-kb' --allow-write --allow-browser-fallback \
     --output ~/pandora/correo-idt/soporte-kb/m365/categorias-prueba/set-prueba.json
   ```

3. Repetir el mismo comando con otro `--output` y comprobar `changed: false`.
4. Devolver el correo a su estado original con `--remove 'Prueba soporte-kb'` y otro `--output`;
   comprobar que `categories` coincide con el `previous_categories` del paso 2.
5. Repetir el paso 2 sin `--allow-write` y comprobar `write_not_authorized` sin llamada al
   proveedor.

El efecto real de `If-Match` sobre mensajes no tiene prueba viva segura; queda anotado en el ADR.

## Propuestas fuera de alcance (no se implementan)

- Cabeceras `In-Reply-To` y `References` para reconstruir hilos.
- Otras carpetas además de bandeja de entrada y enviados.
- Binario de los adjuntos fuera del `content_base64` actual.

## Progreso

| Tarea | Estado | Evidencia |
| --- | --- | --- |
| C1 | Hecha | Worktree y entorno creados; 465 pruebas en verde |
| C2 | Hecha | Documento y Engram #5678 |
| C3 | Hecha | RED 17 fallidas, GREEN 479 aprobadas; `7becee0` |
| C4 | Hecha | Cinco documentos y prueba de documentación; `7becee0` |
| C5 | Hecha | Ruff, mypy y cobertura 82,48 % |
| C6 | Hecha | 18 correos, 3 con `Resuelto` |
| B1 | Bloqueada por política | `SECURITY.md:31-33`, `SECURITY.md:80-82` |
| B2 | Aprobada el 2026-10-19 | Decisión de Camilo; pasa a C7, C8 y C9 |
| C7 | Hecha | ADR y registro de plataforma; `ff41f84` |
| C8 | Hecha por navegador; ruta oficial bloqueada | RED 42, GREEN 536, cobertura 83,14 %; `121b64a` |
| C9 | Pendiente | Espera la confirmación de Camilo |

## Siguiente paso

C9, la prueba controlada en vivo con `Prueba soporte-kb`, cuando Camilo la confirme. Queda abierta
una decisión de Camilo: el modelo de alcances que permitiría a la ruta oficial pedir
`Mail.ReadWrite` sin invalidar las concesiones actuales (la misma vía 2 de B1). Mientras tanto,
`soporte-kb` lee el estado desde `categories` y deriva el catálogo de los nombres observados.
