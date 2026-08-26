# Especificación: mail-query-limits

## ADDED Requirements

### Requirement: `MailQuery.limit` acepta 1..1000
`MailQuery.limit` DEBE aceptar valores entre 1 y 1000 inclusive; valores fuera de rango DEBEN seguir lanzando `ValueError`. El rango anterior era 1..100.

#### Scenario: Límite de 1000 es válido
- DADO `MailQuery(window, limit=1000)`
- CUANDO se construye la consulta
- ENTONCES no se lanza excepción

#### Scenario: Límite de 1001 sigue rechazado
- DADO `MailQuery(window, limit=1001)`
- CUANDO se construye la consulta
- ENTONCES el sistema lanza `ValueError`

### Requirement: Alineación del recorte en la aplicación
`application._query` DEBE recortar el límite de correo con `min(limit, 1000)`, aplicable tanto a `mail.sent.list` como a `mail.inbox.list`. El recorte anterior era `min(limit, 100)`.

#### Scenario: Límite de 1000 no se recorta
- DADO una solicitud de `mail.sent.list` con `limit=1000`
- CUANDO `application._query` construye la `MailQuery`
- ENTONCES el `limit` resultante es 1000, no 100

### Requirement: El techo estructural del navegador no cambia
El nuevo tope de 1000 DEBE coincidir con el techo estructural de la ruta navegador (20 páginas × `$top` 50); el sistema NO DEBE subir `_OUTLOOK_PAGE_SIZE` ni `_OUTLOOK_MAX_PAGES`.

#### Scenario: Lectura paginada hasta el tope por navegador
- DADO una ventana con 1000 o más mensajes disponibles
- CUANDO se ejecuta `mail.inbox.list` con `limit=1000` por navegador
- ENTONCES el sistema pagina hasta 20 veces y entrega exactamente 1000 registros

### Requirement: Truncación determinista y silenciosa
Al alcanzar `limit` en una ventana con más mensajes, el sistema DEBE devolver el prefijo temporal más antiguo (orden ascendente en bandeja) de forma silenciosa; el sobre de respuesta NO incluye un indicador de truncación.

#### Scenario: Ventana con más mensajes que el límite
- DADO una ventana con 1500 mensajes y `limit=1000`
- CUANDO se ejecuta `mail.inbox.list`
- ENTONCES el sistema devuelve los 1000 mensajes más antiguos de la ventana, con estado exitoso y sin campo de truncación
- Y el operador puede estrechar la ventana para leer el resto de forma reanudable

### Requirement: `mail.sent.list` no cambia de orden
El nuevo tope se aplica también a `mail.sent.list`, pero su `$orderby` (descendente en Graph, ascendente en navegador) permanece sin cambio.

#### Scenario: Enviados con límite alto conserva su orden
- DADO una solicitud `mail.sent.list` con `limit=500`
- CUANDO se construye la petición por Graph
- ENTONCES el `$orderby` sigue siendo `sentDateTime desc`
