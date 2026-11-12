# Especificación: capability-advertisement

## ADDED Requirements

### Requirement: Declaración en los siete puntos fail-closed
`mail.inbox.list` DEBE declararse en los siete puntos de Outlook: enum `Capability`, `_CAPABILITY_RECORD_TYPES`, `TARGET_SPECS[OUTLOOK]`, descriptor de `MicrosoftGraphAdapter`, descriptor de `Microsoft365BrowserAdapter`, `RoutePolicy.browser_fallback_capabilities` y `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]`. Omitir cualquiera DEBE producir el fallo tipado propio de esa capa, no un error genérico.

#### Scenario: Falta declaración en la política de fallback
- DADO que `mail.inbox.list` no está en `RoutePolicy.browser_fallback_capabilities`
- CUANDO se solicita la capacidad por navegador tras fallar la ruta oficial
- ENTONCES el sistema responde `browser_fallback_denied`, no un error genérico

#### Scenario: Falta declaración en el descriptor de navegador
- DADO que `mail.inbox.list` no está en `_BROWSER_EXTRACTION_CAPABILITIES[OUTLOOK]`
- CUANDO se solicita la capacidad por la ruta de navegador
- ENTONCES el sistema responde `browser_capability_not_implemented`

### Requirement: `doctor` anuncia la capacidad por ambas rutas
`solercia-connect doctor outlook <cuenta>` DEBE listar `mail.inbox.list` en `capabilities`, propagado desde `TARGET_SPECS[OUTLOOK]`.

#### Scenario: Diagnóstico de Outlook lista la capacidad nueva
- DADO una cuenta de Outlook configurada
- CUANDO se ejecuta `doctor`
- ENTONCES `mail.inbox.list` aparece en `capabilities` del reporte

### Requirement: `providers` anuncia la capacidad en la matriz
`solercia-connect providers` DEBE incluir filas de `CAPABILITY_MATRIX` para `mail.inbox.list` en la ruta oficial y en la ruta de navegador.

#### Scenario: Matriz de capacidades lista ambas rutas
- DADO el comando `providers`
- CUANDO se lista `CAPABILITY_MATRIX`
- ENTONCES existen dos filas para `mail.inbox.list`: una con canal `official` y otra con canal `browser`

### Requirement: Perfiles de conexión persisten la capacidad
El perfil de conexión de Outlook DEBE persistir `mail.inbox.list` dentro de `spec.capabilities`.

#### Scenario: Perfil recién conectado incluye la capacidad
- DADO una cuenta de Outlook recién conectada
- CUANDO se inspecciona el perfil persistido
- ENTONCES `mail.inbox.list` está presente en sus capacidades

### Requirement: `cli._query_payload` falla cerrado para capacidades no cubiertas
Para cualquier capacidad del enum `Capability` sin rama explícita en `cli._query_payload`, el sistema DEBE fallar de forma tipada en vez de devolver silenciosamente un tipo de consulta de otra capacidad.

#### Scenario: Capacidad sin rama explícita
- DADO una capacidad del enum sin rama en `_query_payload`
- CUANDO se construye su consulta de prueba
- ENTONCES el sistema falla de forma explícita, nunca entrega un tipo de consulta incorrecto

### Requirement: Documentación declara la capacidad en ambas rutas
`README.md`, `docs/runbooks/outlook.md`, `docs/USO-CON-AGENTES.md` y `agents/skills/solercia-connect/SKILL.md` (con su symlink en `.claude/skills/`) DEBEN documentar `mail.inbox.list` para la ruta oficial y la de navegador.

#### Scenario: Prueba de documentación verifica la capacidad
- DADO `tests/test_cli_and_docs.py`
- CUANDO se ejecuta la prueba
- ENTONCES confirma que `README.md` y el `SKILL.md` del agente mencionan `mail.inbox.list`
