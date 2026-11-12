# Especificación: mail-record-contract

## ADDED Requirements

### Requirement: Campos aditivos en `MailMessage`
`MailMessage` DEBE ganar tres campos opcionales con valor por defecto, ubicados después de `attachments`: `internet_message_id: str | None = None`, `folder: str | None = None` (`"inbox"` o `"sent"`) y `received_at: datetime | None = None`. `sent_at` permanece obligatorio y sin cambio de tipo.

#### Scenario: Construcción posicional existente no se rompe
- DADO los tres sitios que construyen `MailMessage` posicionalmente (`_microsoft.py`, `google.py`, `tests/fakes.py`)
- CUANDO se omite cualquiera de los tres campos nuevos
- ENTONCES el registro se construye con sus valores por defecto, sin error de construcción

#### Scenario: Registro de bandeja trae los tres campos con valores concretos
- DADO un mensaje leído por `mail.inbox.list`
- CUANDO se normaliza el registro
- ENTONCES `internet_message_id`, `folder="inbox"` y `received_at` tienen valores concretos, ninguno es `None`

### Requirement: `folder` explícito, no derivado de la procedencia
`normalize_mail_message` DEBE recibir el valor de `folder` de forma explícita; el sistema NO DEBE derivarlo de `provenance.endpoint`.

#### Scenario: Bandeja por navegador con `endpoint` de identidad
- DADO un registro de bandeja leído por navegador, cuyo `provenance.endpoint` es la evidencia de identidad `"outlook-rest-v2-me"`
- CUANDO se normaliza el mensaje
- ENTONCES `folder` vale `"inbox"`, independiente del valor de `endpoint`

### Requirement: `internetMessageId` en el `$select` de enviados
El `$select` de `mail.sent.list` DEBE incluir `internetMessageId`/`InternetMessageId` en ambas rutas.

#### Scenario: Registro de enviados trae `internet_message_id`
- DADO un mensaje de `mail.sent.list` con `InternetMessageId` en la respuesta del proveedor
- CUANDO se normaliza el registro
- ENTONCES `internet_message_id` no es `None`

### Requirement: `received_at` es `None` fuera de la bandeja de Outlook
`received_at` DEBE quedar `None` en registros de `mail.sent.list` y en todo registro de Gmail.

#### Scenario: Registro de enviados sin instante de recepción
- DADO un registro producido por `mail.sent.list`
- CUANDO se construye el `MailMessage`
- ENTONCES `received_at` es `None`

### Requirement: Compatibilidad de esquema sin subida de versión
La adición de campos DEBE ser aditiva pura: `solercia.connect/v1` y `solercia.connect/record/v1` NO DEBEN cambiar de versión, y `to_primitive` DEBE exponer las claves nuevas automáticamente.

#### Scenario: Serialización de un registro de bandeja
- DADO un `MailMessage` con los tres campos nuevos poblados
- CUANDO se serializa con `to_primitive`
- ENTONCES el JSON incluye las claves nuevas y conserva `schema: "solercia.connect/record/v1"`

### Requirement: Categorías de Outlook en el registro
`MailMessage` DEBE ganar el campo aditivo `categories: tuple[str, ...] = ()`, ubicado después de `received_at`. Los registros de `mail.inbox.list` y `mail.sent.list` de Outlook DEBEN poblarlo con los nombres de categoría del mensaje, en el orden del proveedor, sin espacios sobrantes y sin duplicados. El `$select` DEBE incluir `categories` en Graph y `Categories` en la ruta de navegador. Un mensaje sin categorías DEBE reportar una lista vacía; un valor que no sea una lista de nombres no vacíos DEBE fallar cerrado con `malformed_provider_data`. Los registros de Gmail DEBEN conservar la lista vacía.

#### Scenario: Correo de soporte con categorías
- DADO un mensaje de Inbox con las categorías `SOPORTE/PENDIENTE` y `SOPORTE/NIVEL 1`
- CUANDO se normaliza por cualquiera de las dos rutas
- ENTONCES `categories` vale `("SOPORTE/PENDIENTE", "SOPORTE/NIVEL 1")` y los dos registros son iguales salvo `provenance`

#### Scenario: Categorías malformadas
- DADO un mensaje cuyo valor de categorías es una cadena o contiene un nombre vacío
- CUANDO se normaliza el registro
- ENTONCES el sistema falla cerrado con `malformed_provider_data`, nunca reporta una lista vacía

### Requirement: Paridad de registro entre rutas
Un mensaje leído por Graph y el mismo mensaje leído por navegador DEBEN normalizar a registros idénticos en todos los campos salvo `provenance`, incluidos los tres campos nuevos con valores concretos.

#### Scenario: Fixtures gemelos de bandeja
- DADO fixtures gemelos de un mismo mensaje de Inbox, uno por cada ruta
- CUANDO se comparan los dos registros normalizados
- ENTONCES son iguales campo a campo salvo `provenance`, y ninguno de los tres campos nuevos es `None` en ambos
