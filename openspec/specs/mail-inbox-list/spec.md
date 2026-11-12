# Especificación: mail-inbox-list

## ADDED Requirements

### Requirement: Enrutamiento oficial-primero con fallback de navegador explícito
El sistema DEBE despachar `mail.inbox.list` primero por la ruta oficial (Graph). Sólo si la ruta oficial falla con una razón allowlisteada para fallback Y la invocación de CLI incluye `--allow-browser-fallback`, el sistema DEBE reintentar por la ruta de navegador supervisado.

#### Scenario: Ruta oficial disponible
- DADO que Graph responde con éxito para `mail.inbox.list`
- CUANDO se ejecuta la solicitud
- ENTONCES el sistema no invoca la ruta de navegador

#### Scenario: Ruta paralela de pruebas construye el tipo correcto
- DADO `cli._query_payload` invocado con `Capability.MAIL_INBOX_LIST`
- CUANDO se construye la consulta de prueba
- ENTONCES el resultado es una `MailQuery`, nunca otro tipo

### Requirement: Ventana de recepción con fin exclusivo e identidad atestada
El sistema DEBE filtrar la bandeja por instante de recepción (`receivedDateTime` en Graph, `ReceivedDateTime` en navegador) sobre una ventana horaria con fin exclusivo, y DEBE atestar la identidad de la cuenta antes de cualquier lectura.

#### Scenario: Filtro de recepción aplicado por Graph
- DADO una ventana `[2026-06-01T00:00:00-05:00, 2026-09-01T00:00:00-05:00)`
- CUANDO se ejecuta `mail.inbox.list` sobre `GET {base}/me/mailFolders/inbox/messages`
- ENTONCES el `$filter` enviado es `receivedDateTime ge <inicio> and receivedDateTime lt <fin>`

#### Scenario: Identidad de cuenta no coincide
- DADO que la cuenta autenticada difiere de `query.account_id`
- CUANDO se ejecuta `mail.inbox.list` por cualquiera de las dos rutas
- ENTONCES el sistema devuelve cero registros con `authorization_boundary_violation`

### Requirement: Proyección de campos y orden ascendente
El `$select` de bandeja DEBE incluir, en ambas rutas, el instante de envío, el instante de recepción, `internetMessageId`/`InternetMessageId` y `conversationId`/`ConversationId`, además de los campos ya proyectados en enviados. El `$orderby` DEBE ser ascendente por instante de recepción en ambas rutas.

#### Scenario: Proyección completa
- DADO una solicitud `mail.inbox.list`
- CUANDO se construye la petición en Graph o en navegador
- ENTONCES el `$select` contiene las cuatro claves nuevas y el `$orderby` ordena por recepción de forma ascendente

#### Scenario: Falta el instante de envío en la proyección
- DADO un `$select` de bandeja que omite el instante de envío
- CUANDO se normaliza un mensaje de la colección
- ENTONCES el sistema falla cerrado con `malformed_provider_data`

### Requirement: Paginación acotada con verificación de origen
El sistema DEBE seguir `@odata.nextLink` sólo tras verificar mismo origen HTTPS en Graph o host allowlisteado en navegador, con un techo de 20 páginas por invocación en ambas rutas.

#### Scenario: `nextLink` no confiable
- DADO un `@odata.nextLink` que apunta a un host distinto del origen configurado
- CUANDO el sistema evalúa seguir esa página
- ENTONCES rechaza la solicitud antes de enviar credenciales

### Requirement: Validación por registro en el runtime
El runtime DEBE validar cada registro de bandeja contra el tipo `MailMessage`, el `provider_id`, la `family`, la `route` y la frontera de cuenta, igual que para `mail.sent.list`.

#### Scenario: Registro fuera de la frontera de cuenta
- DADO un registro de bandeja con `account_id` distinto del de la solicitud
- CUANDO el runtime lo valida
- ENTONCES el sistema rechaza la ejecución

### Requirement: Vacío permanece vacío
Cuando la ventana no contiene mensajes en Inbox, el sistema DEBE reportar cero registros con estado exitoso, sin ampliar el rango ni reintentar.

#### Scenario: Ventana sin mensajes
- DADO una ventana horaria sin mensajes en Inbox
- CUANDO se ejecuta `mail.inbox.list`
- ENTONCES el sistema responde con cero registros y estado exitoso, sin ampliar la ventana

### Requirement: Fallback de navegador opt-in por invocación
La ruta de navegador para `mail.inbox.list` DEBE requerir `--allow-browser-fallback` en cada invocación de CLI, y el servicio HTTP DEBE rechazarla por diseño.

#### Scenario: Servicio HTTP solicita bandeja por navegador
- DADO una solicitud al servicio HTTP para `mail.inbox.list` con ruta de navegador
- CUANDO se procesa la solicitud
- ENTONCES el sistema responde `action_required` con el comando de CLI exacto a ejecutar

### Requirement: Gmail queda fuera de alcance
El sistema NO DEBE anunciar ni enrutar `mail.inbox.list` para `gmail`.

#### Scenario: Solicitud de bandeja para Gmail
- DADO una solicitud de `mail.inbox.list` con destino `gmail`
- CUANDO se resuelve el target contra `TARGET_SPECS`
- ENTONCES el sistema responde `invalid_input` porque la capacidad no está declarada para Gmail
