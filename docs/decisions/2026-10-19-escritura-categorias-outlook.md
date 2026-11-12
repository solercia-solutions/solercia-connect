# Decisión: escritura de categorías de Outlook

- **Estado:** Aceptado
- **Fecha de decisión:** 2026-10-19
- **Decidido por:** Camilo Calderón, propietario del producto
- **Ámbito:** Regla de solo lectura, alcances OAuth de Microsoft, ruta de navegador de Microsoft y
  servicio HTTP
- **Aplicación:** Obligatoria para la capacidad `mail.categories.set`
- **Actualiza:** [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisión 3.9), [`SECURITY.md`](../../SECURITY.md) y [`AGENTS.md`](../../AGENTS.md)

## Contexto

Solercia Connect se definió como un runtime de lectura. Las reglas vigentes antes de esta decisión
lo dicen de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| `AGENTS.md`, encabezado | «It normalizes read operations into typed records with provenance and deterministic execution receipts.» |
| `AGENTS.md`, convenciones | «Browser operations are read-only and use a dedicated profile, never a personal Chrome profile.» |
| `SECURITY.md`, OAuth | «Use least-privilege read scopes: Microsoft `Mail.Read`, `Calendars.Read`, and the separate Teams `OnlineMeetingTranscript.Read.All` …» |
| `SECURITY.md`, fallback de navegador | «It is used only for read-only calls to documented provider endpoints.» |

El producto `soporte-kb` cambia el estado de un caso de soporte desde su propio front, lo registra en
GLPI y lo refleja como categoría en el correo de Outlook del buzón de soporte. Solercia Connect ya
lee esas categorías (`categories` en `mail.inbox.list` y `mail.sent.list`); lo que falta es
asignarlas, y eso contradice las cuatro reglas anteriores. El registro de plataforma no admite
excepciones implícitas, así que la escritura necesita esta decisión antes de existir en el código.

## Decisión

1. **Única excepción.** Asignar categorías a un correo existente (`mail.categories.set`) es la
   única escritura que Solercia Connect hace en un proveedor. No se envían, mueven, marcan como
   leídos, crean ni borran correos; no se modifica ninguna otra propiedad del mensaje, ni carpetas,
   calendarios, archivos, tickets o incidencias; tampoco se crean ni editan las categorías maestras
   del buzón.
2. **Autorización por invocación.** Cada invocación exige la bandera `--allow-write`, apagada por
   defecto. Sin ella la orden falla con `write_not_authorized` antes de construir el registro de
   proveedores, abrir el navegador o llamar al proveedor. Ninguna variable de entorno ni perfil la
   sustituye. Quien usa la biblioteca directamente debe marcar la petición como autorizada; si no lo
   hace, el runtime la rechaza sin invocar ningún adaptador.
3. **Doble llave en el navegador.** La ruta de navegador sigue exigiendo `--allow-browser-fallback`
   en la misma invocación. Son dos autorizaciones distintas: una abre la ruta y la otra permite
   escribir.
4. **Cuenta obligatoria.** La orden exige `--account`. Antes de leer o escribir se verifica la
   identidad del buzón con la misma prueba de la decisión 4.1 del registro de plataforma; una
   identidad distinta produce cero llamadas de lectura o escritura.
5. **Escritura acotada.** La única llamada de escritura es un `PATCH` sobre un solo mensaje cuyo
   cuerpo contiene únicamente la colección de categorías. El código no construye ningún otro cuerpo
   de escritura.
6. **Sin HTTP.** El servicio HTTP en loopback rechaza la capacidad por diseño, con o sin el
   interruptor de inicio de sesión interactivo: responde 403 sin invocar el servicio de aplicación,
   y el servicio de aplicación también la rechaza cuando se ejecuta en modo no interactivo. La
   escritura solo existe en la CLI canónica y en la biblioteca.
7. **Sin reintentos.** Una escritura fallida no se reintenta de forma automática. Quien la invoca
   repite la orden, que vuelve a leer el estado y calcula de nuevo el resultado.

## Contrato de la capacidad

### Entrada

| Opción | Regla |
| --- | --- |
| `--message-id` | Obligatoria. Es el identificador del proveedor que llega en el campo `id` de cada registro de `mail.inbox.list` y `mail.sent.list` (Graph `id`, Outlook REST `Id`). No es `internet_message_id` ni `conversation_id`. Distingue mayúsculas y minúsculas. |
| `--add` | Repetible. Agrega cada nombre que el mensaje no tenga. |
| `--remove` | Repetible. Quita cada nombre que el mensaje tenga. Se puede combinar con `--add` para cambiar un estado en una sola escritura; un mismo nombre no puede ir en las dos. |
| `--replace` | Repetible. Fija la lista completa. Excluye `--add` y `--remove`. |
| `--allow-write` | Obligatoria para escribir; apagada por defecto. |
| `--account` | Obligatoria. |

Los nombres se recortan, no pueden quedar vacíos y se comparan sin distinguir mayúsculas y
minúsculas, para que asignar `resuelto` a un correo que ya tiene `Resuelto` no duplique la
categoría.

### Comportamiento

1. Lee el mensaje con su colección de categorías y su versión (`@odata.etag`, que refleja el
   `ChangeKey`).
2. Calcula la lista final: con `--replace`, la lista dada; con `--add`/`--remove`, la lista actual
   sin los nombres a quitar y con los nombres a agregar al final, en el orden dado.
3. Si la lista final contiene los mismos nombres que la actual, no escribe y responde
   `changed: false`. Repetir la misma orden es idempotente.
4. Si cambia, emite el `PATCH` con `If-Match` igual a la versión leída. Si el proveedor responde
   `412 Precondition Failed` o `409 Conflict`, la orden falla con `write_conflict`, sin reintentar,
   para no pisar una edición simultánea del equipo.
5. Comprueba que la respuesta del `PATCH` confirme la lista calculada; si no la confirma, falla
   cerrada con `malformed_provider_data`.

### Salida

Un registro `solercia.connect/record/v1` con `message_id`, `categories` (lista final),
`previous_categories` (lista leída antes de escribir), `changed` y `provenance`. El sobre
`solercia.connect/v1` lleva el recibo determinista, como cualquier otra capacidad.

## Rutas, endpoints y alcances

Las citas siguientes son textuales y conservan el idioma de la fuente; el análisis va aparte.

### Ruta oficial: Microsoft Graph

- «PATCH /me/messages/{id}» (Microsoft Graph v1.0, *Update message*, sección «HTTP request»).
- «Delegated (work or school account) | Mail.ReadWrite» (misma página, sección «Permissions»).
- «categories | String collection | The categories associated with the message.» (misma página,
  sección «Request body»).
- «Allows the app to create, read, update, and delete email in user mailboxes. Does not include
  permission to send mail.» y «AdminConsentRequired | … | No» para el permiso delegado
  (*Microsoft Graph permissions reference*, entrada `Mail.ReadWrite`).

Lo anterior implica que no existe un alcance de Graph más estrecho que `Mail.ReadWrite` para asignar
categorías: el permiso autoriza también crear y borrar correo. El límite real lo pone el código
(punto 5 de la decisión), no el alcance.

### Ruta de navegador: Outlook REST v2.0

- «PATCH https://outlook.office.com/api/v2.0/me/messages/{message_id}» y «Change writable
  properties on a draft or existing message. Only the properties that you specify are changed.»
  (*Outlook Mail REST API reference (version 2.0)*, «Update a message»).
- Alcance mínimo: «https://outlook.office.com/mail.readwrite» (misma sección, «Minimum required
  scope»).
- «Categories | Collection (String) | The categories associated with the message. | Yes» y
  «ChangeKey | String | The version of the message.» (*Resource reference … (version 2.0)*,
  recurso «Message»; la columna «Yes» es «Writable?»).
- «Version 2.0 of the Outlook REST API is deprecated.» y «The v2.0 REST endpoint will be fully
  decommissioned in March 2024» (nota al inicio de la referencia v2.0).

En la práctica, la ruta de navegador ya depende de esa misma referencia para sus lecturas, con
evidencia viva del 2026-10-19 sobre el buzón de soporte. La escritura usa el endpoint documentado en
esa referencia y la autorización de la propia sesión de Outlook Web; su prueba viva es la prueba
controlada del plan de trabajo. Si Microsoft retira el endpoint, la capacidad falla cerrada por la
ruta de navegador y Graph queda como única vía.

### Control de concurrencia

- «The value of the If-Match request header MUST be an ETag value previously retrieved for the
  resource» y «If present, the request MUST only be processed if the specified ETag value matches
  the current ETag value of the target resource.» (*OData Version 4.01. Part 1: Protocol*, §8.2.4
  «Header If-Match»).
- «412 Precondition Failed indicates that the client has performed a conditional request and the
  resource fails the condition. The service MUST ensure that no observable change occurs as a
  result of the request.» (misma especificación, §9.2.5).
- Las respuestas de ejemplo de la referencia v2.0 incluyen `@odata.etag` con el mismo valor del
  `ChangeKey` del mensaje («Update a message», «Sample response»).

Lo anterior implica que el control de concurrencia se apoya en la semántica estándar de OData sobre
la versión que el propio proveedor publica. Las páginas de Microsoft para actualizar un mensaje no
listan `If-Match` entre sus encabezados, así que su efecto real queda pendiente de evidencia viva.

### Identificador del mensaje

- «their IDs change. It doesn't happen often, only if the item is moved» y «Immutable identifiers,
  like all identifiers in Microsoft Graph, are case-sensitive.» (*Obtain immutable identifiers for
  Outlook resources*).

Lo anterior implica que un `id` leído deja de servir si alguien mueve el correo a otra carpeta. La
orden responde entonces `invalid_input` y el consumidor vuelve a leer la bandeja para obtener el
`id` vigente.

## Estado de implementación: ruta oficial bloqueada por alcances

La ruta oficial no se habilita con esta decisión. Agregar `Mail.ReadWrite` al target `outlook`
invalida todas las concesiones oficiales existentes, por el mismo mecanismo que bloqueó el catálogo
de categorías con `MailboxSettings.Read`:

- `src/solercia_connect/config.py:88` fija los alcances del target `outlook`:
  `("openid", "profile", "User.Read", "Mail.Read", "Calendars.Read")`.
- `src/solercia_connect/application.py:113` y `src/solercia_connect/connections.py:282-283` exigen
  que la concesión guardada contenga todos los alcances del target. Una concesión obtenida con
  `Mail.Read` dejaría de cumplirlo: `status` respondería `invalid_credentials` y el servicio HTTP
  `action_required` hasta reconectar.
- `src/solercia_connect/oauth.py:250` pide exactamente esos alcances al refrescar el token.

Mientras no exista otra decisión, el adaptador oficial no expone `mail.categories.set`. El runtime
registra `official_capability_unavailable` y solo la ruta de navegador, con `--allow-browser-fallback`,
atiende la orden. Habilitar la ruta oficial exige decidir entre reconectar todas las cuentas de
Outlook con `Mail.ReadWrite`, lo que retira el mínimo privilegio también para las lecturas, o un
modelo de alcances por capacidad, que es la misma propuesta pendiente del catálogo.

## Límites que no cambian

- La identidad se prueba antes de cualquier llamada de datos; una cuenta distinta produce cero
  lecturas y cero escrituras.
- La autorización capturada en la sesión del navegador sigue en memoria, se borra después de cada
  uso, nunca se escribe en disco, nunca se registra y solo viaja a hosts de la lista permitida.
- Cada intento por navegador exige `--allow-browser-fallback`; la configuración global solo habilita
  la ruta.
- El catálogo de categorías maestras sigue fuera de alcance. El consumidor usa los nombres que ya
  observa en los registros.

## Consecuencias

- `soporte-kb` puede reflejar en Outlook el estado que su front registra en GLPI, sin reimplementar
  OAuth, rutas ni selectores.
- El código, no el alcance, limita la escritura a la colección de categorías de un mensaje. Una
  prueba fija que el cuerpo de la escritura contiene solo esa colección.
- La ruta de navegador depende de un endpoint que Microsoft declara obsoleto; la ruta oficial queda
  pendiente de la decisión de alcances.
- El efecto de `If-Match` sobre mensajes queda pendiente de evidencia viva.

## Plan de reversión

Retirar `mail.categories.set` de `TARGET_SPECS` y del descriptor del adaptador de navegador deja la
bandera `--allow-write` sin capacidad que autorizar. No hay estado local ni migración que deshacer.
Las categorías ya escritas permanecen en el buzón y se corrigen desde Outlook.

## Alternativas descartadas

- **Que `soporte-kb` escriba directamente en Outlook:** reimplementaría OAuth, rutas y captura de
  sesión, contra la decisión 5.1 del registro de plataforma.
- **Una variable de entorno que habilite la escritura:** dejaría autorizada cualquier invocación
  futura. Se usa una bandera por invocación, igual que `--allow-browser-fallback`.
- **Exponer la escritura por HTTP:** un cliente de loopback podría cambiar el buzón sin una persona
  en la terminal.
- **Leer el catálogo o escribir por Graph con una autorización capturada en el navegador:** exige
  ampliar la lista de hosts de la decisión 3.8; sigue como propuesta aparte.

## Referencias oficiales

- [Update message — Microsoft Graph v1.0](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0)
- [Microsoft Graph permissions reference — Mail.ReadWrite](https://learn.microsoft.com/en-us/graph/permissions-reference#mailreadwrite)
- [Outlook Mail REST API reference (version 2.0) — Update a message](https://learn.microsoft.com/en-us/previous-versions/office/office-365-api/api/version-2.0/mail-rest-operations#update-messages)
- [Resource reference for the Mail, Calendar, Contacts, and Task REST APIs (version 2.0) — Message](https://learn.microsoft.com/en-us/previous-versions/office/office-365-api/api/version-2.0/complex-types-for-mail-contacts-calendar)
- [Obtain immutable identifiers for Outlook resources](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- [OData Version 4.01. Part 1: Protocol — Header If-Match](https://docs.oasis-open.org/odata/odata/v4.01/odata-v4.01-part1-protocol.html#sec_HeaderIfMatch)
