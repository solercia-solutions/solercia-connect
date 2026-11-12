# Decisión: envío de correo de Gmail en dos pasos

- **Estado:** Aceptado
- **Fecha de decisión:** 2026-10-25
- **Decidido por:** Camilo Calderón, propietario del producto (decisión D5). La separación de la
  conexión de redacción la decidió el orquestador de la feature, por mínimo privilegio.
- **Ámbito:** Regla de escritura acotada, ruta oficial de la API de Gmail, conexiones OAuth de
  Google, ruta de navegador de Google y servicio HTTP
- **Aplicación:** Obligatoria para el target `gmail-compose` y para las capacidades
  `mail.draft.create` y `mail.draft.send`
- **Actualiza:** [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisión 3.9), [`SECURITY.md`](../../SECURITY.md), [`AGENTS.md`](../../AGENTS.md) y el skill
  [`agents/skills/solercia-connect/SKILL.md`](../../agents/skills/solercia-connect/SKILL.md)
- **Complementa:** [`2026-10-19-escritura-categorias-outlook.md`](2026-10-19-escritura-categorias-outlook.md)
  y [`2026-10-25-escritura-transiciones-jira.md`](2026-10-25-escritura-transiciones-jira.md), las dos
  primeras escrituras del producto

## Contexto

Antes de esta decisión, Solercia Connect admitía exactamente dos escrituras. Las reglas vigentes lo
dicen de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| `AGENTS.md`, encabezado | «It has exactly two bounded writes: assigning Outlook categories to one message (`mail.categories.set`) […] and the status transition of one Jira issue (`external.issues.transition`)» |
| `AGENTS.md`, convenciones | «Writes need an explicit per-invocation `--allow-write` flag, off by default, never run over HTTP, never retry, and touch nothing but either the categories of one Outlook message or the status of one Jira issue through one transition that requires no fields. Any other write needs a new ADR.» |
| `SECURITY.md`, encabezado | «It has exactly two bounded writes: assigning Outlook categories to one message, and the status transition of one Jira issue» |
| Registro de plataforma, decisión 3.9 | «Ninguna otra escritura queda autorizada.» |

El pedido que origina esta decisión quedó registrado en el documento de la feature
(`odd/tasks/google-jira-browser-fallback.md`, fase 9): «conectar para leer y enviar tambien correos
a […]» (Camilo, 2026-10-25). La parte omitida es la dirección de una cuenta institucional de
IDARTES; esta decisión no la repite. La decisión D5 de ese documento la aceptó el mismo día con la
nota «Dentro de solercia-connect».

Solercia Connect ya lee el correo enviado de Gmail (`mail.sent.list`) por la ruta oficial, con el
alcance `gmail.readonly` y la identidad verificada en cada llamada. Enviar correo contradice las
cuatro reglas anteriores y, a diferencia de las dos escrituras existentes, produce un efecto hacia
terceros que no se puede deshacer. Por eso la escritura necesita esta decisión antes de existir en
el código, y se divide en dos pasos con una revisión humana entre ellos.

## Decisión

1. **Tercera excepción, en dos capacidades.** Crear un borrador en Gmail (`mail.draft.create`) y
   enviar un borrador existente (`mail.draft.send`) son la tercera escritura que Solercia Connect
   hace en un proveedor, después de las categorías de Outlook y de la transición de Jira. No se
   envía correo sin borrador previo (`users.messages.send` no se usa), no se responde ni se reenvía
   un mensaje, no se adjuntan archivos, no se escribe HTML, no se modifican ni se borran borradores
   ni mensajes, y no se tocan etiquetas, filtros, firmas ni la configuración del buzón.
2. **Dos pasos con revisión humana.** El agente crea el borrador y muestra a la persona usuaria una
   vista previa. La persona lo revisa en Gmail, en la carpeta Borradores. Solo después de que la
   persona apruebe de forma expresa ese identificador de borrador, el agente lo envía en una
   invocación aparte. El código no puede comprobar la aprobación; la garantía técnica es que crear y
   enviar son dos órdenes distintas, cada una con su propio `--allow-write`, y que el envío exige el
   identificador exacto del borrador. El skill obliga al agente a esperar la aprobación.
3. **Conexión separada, de mínimo privilegio.** La redacción usa un target propio,
   `gmail-compose`, con los alcances `openid`, `email` y
   `https://www.googleapis.com/auth/gmail.compose`, y su propia clave de token
   `gmail-compose:<cuenta>`. El target `gmail` sigue siendo de solo lectura, con sus alcances sin
   cambios, así que las conexiones de Gmail existentes siguen funcionando y ninguna concesión de
   lectura gana la capacidad de enviar.
4. **Autorización por invocación.** Cada invocación de cualquiera de las dos capacidades exige la
   bandera `--allow-write`, apagada por defecto. Sin ella la orden falla con
   `write_not_authorized` antes de construir el registro de proveedores o llamar a Google. Ninguna
   variable de entorno ni perfil la sustituye. Quien usa la biblioteca directamente debe marcar la
   petición como autorizada; si no lo hace, el runtime la rechaza sin invocar ningún adaptador.
5. **Solo la ruta oficial.** Las dos capacidades usan únicamente la API REST de Gmail. Ninguna entra
   en la lista de capacidades del navegador de la familia Google, y la orden rechaza
   `--allow-browser-fallback` con `invalid_input`.
6. **Cuenta obligatoria e identidad.** La orden exige `--account`. Antes de leer o escribir se
   verifica la identidad con `GET /gmail/v1/users/me/profile`: `emailAddress` debe coincidir con la
   cuenta, sin distinguir mayúsculas y minúsculas. Una identidad distinta produce cero lecturas y
   cero escrituras.
7. **Remitente fijo.** El remitente (`From`) de un borrador creado es siempre la cuenta conectada;
   no se aceptan alias ni direcciones «enviar como». Al enviar, el `From` del borrador debe ser
   exactamente la cuenta conectada; si falta o es otra dirección, la orden falla cerrada.
8. **Contenido explícito y acotado.** Los destinatarios llegan solo por `--to` (al menos uno),
   `--cc` y `--bcc`, cada uno repetible, con un máximo de 20 en total, sin repetidos y validados con
   la misma regla de direcciones de correo del resto del producto. El asunto (`--subject`) es una
   sola línea no vacía. El cuerpo es texto plano UTF-8 de hasta 200 KB y llega desde un archivo
   (`--body-file`) o desde la entrada estándar (`--body-stdin`), nunca como argumento de la línea
   de órdenes.
9. **Verificación de duplicados antes de enviar.** Salvo que la orden incluya `--allow-duplicate`,
   antes de enviar se buscan en Enviados de los últimos 30 días los mensajes con el mismo asunto.
   Si alguno tiene el mismo asunto normalizado y el mismo conjunto normalizado de destinatarios, la
   orden falla cerrada con `duplicate_suspected` e informa la fecha de envío de la coincidencia,
   nunca su cuerpo. La búsqueda usa la conexión de solo lectura `gmail` de la misma cuenta, porque
   `users.messages.list` y `users.messages.get` no aceptan el alcance `gmail.compose` (ver
   «Rutas, endpoints y alcances»). Si esa conexión no existe o no prueba la misma identidad, la
   orden falla cerrada con `duplicate_check_unavailable`. `--allow-duplicate` solo se usa con la
   aprobación expresa de la persona usuaria.
10. **Sin HTTP.** El servicio HTTP en loopback rechaza las dos capacidades por diseño, con o sin el
    interruptor de inicio de sesión interactivo: responde 403 sin invocar el servicio de aplicación,
    y el servicio de aplicación también las rechaza cuando se ejecuta en modo no interactivo.
11. **Sin reintentos.** Cada orden emite como máximo un `POST` y nunca lo repite. Si después del
    `POST` no llega una confirmación legible (error `5xx`, tiempo de espera, red no disponible o
    respuesta ilegible), la orden falla con `write_unverified` y advierte que el borrador pudo
    haberse creado o que el mensaje pudo haberse enviado. Antes de cualquier nuevo intento se revisa
    Gmail, en Borradores o en Enviados.
12. **Alcance verificado.** Si la concesión guardada de `gmail-compose` no incluye `gmail.compose`,
    la orden falla cerrada con `write_scope_missing` antes de cualquier llamada, con la remediación
    `solercia-connect connect gmail-compose <cuenta>`.

## Contrato de las capacidades

### Conexión `gmail-compose`

```bash
solercia-connect connect gmail-compose <cuenta>
solercia-connect status  gmail-compose <cuenta>
solercia-connect disconnect gmail-compose <cuenta>
```

Usa el mismo cliente OAuth de escritorio de Google que las demás conexiones de Google
(`SOLERCIA_CONNECT_GOOGLE_CLIENT_ID`) y el mismo flujo con retorno a loopback. La identidad se
prueba con `users.getProfile`, que acepta `gmail.compose`. El target no ofrece capacidades de
lectura ni ruta de navegador.

### Escritura: `mail.draft.create`

| Opción | Regla |
| --- | --- |
| `--to` | Obligatoria y repetible. Una dirección por ocurrencia. |
| `--cc`, `--bcc` | Opcionales y repetibles. |
| `--subject` | Obligatoria. Una línea, sin saltos de línea. |
| `--body-file` o `--body-stdin` | Exactamente una. Texto plano UTF-8, hasta 200 KB. |
| `--account` | Obligatoria. |
| `--allow-write` | Obligatoria para escribir; apagada por defecto. |

Comportamiento determinista:

1. Valida las direcciones, el límite de 20 destinatarios, el asunto y el tamaño del cuerpo.
2. Comprueba que la concesión guardada incluya `gmail.compose`.
3. Verifica la identidad con `users.getProfile`.
4. Construye un mensaje RFC 822 de texto plano UTF-8 con `From` igual a la cuenta conectada y los
   encabezados `To`, `Cc`, `Bcc` y `Subject` pedidos.
5. Emite un solo `POST /gmail/v1/users/me/drafts` con el cuerpo
   `{"message": {"raw": "<mensaje en base64url>"}}`.

El registro `solercia.connect/record/v1` lleva el identificador del borrador, el identificador y el
hilo del mensaje, el remitente, los destinatarios, el asunto, una vista previa de los primeros 200
caracteres del cuerpo y `provenance`. Esa vista previa es la que el agente muestra a la persona
usuaria antes de pedir la aprobación.

### Escritura: `mail.draft.send`

| Opción | Regla |
| --- | --- |
| `--draft-id` | Obligatoria. El identificador devuelto por `mail.draft.create` o visible en Gmail. |
| `--account` | Obligatoria. |
| `--allow-write` | Obligatoria para escribir; apagada por defecto. |
| `--allow-duplicate` | Opcional. Omite la verificación de duplicados; solo con aprobación expresa. |

Comportamiento determinista:

1. Comprueba que la concesión guardada incluya `gmail.compose` y verifica la identidad.
2. Lee el borrador con `users.drafts.get` en formato `metadata`. Un borrador inexistente falla con
   `draft_not_found`. Lee los encabezados `From`, `To`, `Cc`, `Bcc` y `Subject`; el `From` debe ser
   la cuenta conectada, debe haber entre 1 y 20 destinatarios y el asunto no puede estar vacío.
3. Si no se pasó `--allow-duplicate`, verifica la identidad de la conexión `gmail` de la misma
   cuenta, lista con `users.messages.list` y la consulta `in:sent subject:"<asunto>" after:<hace 30
   días>`, con paginación acotada, y lee los encabezados de cada candidato con `users.messages.get`
   en formato `metadata`. Una coincidencia de asunto normalizado y de conjunto de destinatarios
   falla con `duplicate_suspected` antes del envío.
4. Emite un solo `POST /gmail/v1/users/me/drafts/send` con el cuerpo exacto `{"id": "<borrador>"}`.
5. Devuelve el identificador del mensaje enviado, su hilo, los destinatarios, el asunto y la fecha
   del envío.

### Errores tipados

| Código | Cuándo |
| --- | --- |
| `invalid_input` | Falta un destinatario, una dirección no es válida, se supera el límite, falta el asunto o el cuerpo, o se pidió la ruta de navegador. |
| `write_not_authorized` | Falta `--allow-write`; no se hizo ninguna llamada al proveedor. |
| `write_not_available_over_http` | La escritura llegó por el servicio HTTP. |
| `write_scope_missing` | La concesión guardada de `gmail-compose` no incluye `gmail.compose`. |
| `authorization_boundary_violation` | La identidad observada o el `From` del borrador no es la cuenta conectada. |
| `draft_not_found` | Gmail responde `404` al leer o al enviar el borrador. |
| `duplicate_suspected` | Enviados tiene un mensaje con el mismo asunto y los mismos destinatarios. |
| `duplicate_check_unavailable` | Falta la conexión `gmail` de solo lectura que permite revisar Enviados. |
| `write_unverified` | Después del `POST` no llegó una confirmación legible; la escritura pudo aplicarse. |
| `invalid_credentials` / `authorization_denied` | Google responde `401` o `403`. |
| `malformed_provider_data` | Una lectura de Gmail no tiene la forma documentada. |

## Rutas, endpoints y alcances

Las citas siguientes son textuales y conservan el idioma de la fuente; el análisis va aparte.

### Crear el borrador

- «POST https://gmail.googleapis.com/gmail/v1/users/{userId}/drafts» y «Creates a draft with the
  `DRAFT` label.» (*Gmail API*, «Method: users.drafts.create»).
- Alcances: «Requires one of the following OAuth scopes: `https://mail.google.com/`,
  `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.compose`»
  (misma operación, «Authorization scopes»).
- «MESSAGE: the RFC 2822 formatted MIME message, encoded as base64URL.» (*Create and send draft
  emails*, ejemplo con cURL).

### Leer el borrador

- «GET https://gmail.googleapis.com/gmail/v1/users/{userId}/drafts/{id}» y «Gets the specified
  draft.» («Method: users.drafts.get»). Sus alcances incluyen `gmail.compose` y `gmail.readonly`.
- Formato `metadata`: «Returns only email message ID, labels, and email headers.» (enumeración
  *Format*).

### Enviar el borrador

- «POST https://gmail.googleapis.com/gmail/v1/users/{userId}/drafts/send» y «Sends the specified,
  existing draft to the recipients in the `To`, `Cc`, and `Bcc` headers.» («Method:
  users.drafts.send»).
- «If successful, the response body contains an instance of `Message`.» (misma operación,
  «Response body»). Sus alcances son los mismos de `users.drafts.create`.

### Revisar Enviados

- `q`: «Only return messages matching the specified query. Supports the same query format as the
  Gmail search box.» («Method: users.messages.list», «Query parameters»).
- `metadataHeaders[]`: «When given and format is `METADATA`, only include headers specified.»
  («Method: users.messages.get»).
- Alcances de las dos operaciones: «`https://mail.google.com/`,
  `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.readonly`,
  `https://www.googleapis.com/auth/gmail.metadata`» («Authorization scopes» de cada una).
- Operadores de búsqueda: `subject:` «Find emails by a word or phrase in the subject line.» y
  `after:`/`before:` «Search for emails received during a certain time period.» (*Refine searches
  in Gmail*, ayuda de Gmail).

Lo anterior implica que la conexión `gmail-compose` no puede listar ni leer mensajes enviados. La
verificación de duplicados se apoya en la conexión `gmail`, que ya tiene `gmail.readonly`, en lugar
de ampliar los alcances de la conexión de redacción.

### Identidad y fecha de los mensajes

- «Gets the current user's Gmail profile.» («Method: users.getProfile»). Sus alcances incluyen
  `gmail.compose` y `gmail.readonly`.
- `internalDate`: «The internal message creation timestamp (epoch ms), which determines ordering in
  the inbox. For normal SMTP-received email, this represents the time the message was originally
  accepted by Google, which is more reliable than the `Date` header.» (recurso *Message*).

### Clasificación del alcance

- En la tabla «Restricted scopes» de *Choose Gmail API scopes* figura
  «`https://www.googleapis.com/auth/gmail.compose` | Manage drafts and send emails.», junto con
  `gmail.readonly`, `gmail.modify` y `https://mail.google.com/`.

Lo anterior implica que `gmail.compose` es el alcance más estrecho que permite enviar, pero también
permite gestionar cualquier borrador de la cuenta. Como en la transición de Jira, el límite real lo
pone el código (puntos 1, 7 y 8 de la decisión), no el alcance. Al ser un alcance restringido, un
cliente OAuth publicado para usuarios externos necesita la verificación de Google; un cliente propio
en modo de prueba o interno no la necesita para su titular.

## Estado de implementación

Las dos capacidades se implementan con pruebas fuera de línea y quedan como `implemented-offline`
en la matriz de capacidades hasta una prueba viva controlada, que exige la aprobación expresa del
propietario, una cuenta dedicada y un destinatario de prueba propio. En la misma fase se agrega la
lectura `mail.inbox.list` al target `gmail`, solo por la ruta oficial.

## Límites que no cambian

- El target `gmail` conserva sus alcances de solo lectura y no gana capacidades de escritura.
- La identidad se prueba antes de cualquier llamada de datos; una cuenta distinta produce cero
  lecturas y cero escrituras.
- Los tokens siguen en el almacén seguro o, en modo proyecto, en la carpeta privada del proyecto;
  nunca se registran ni se imprimen. Los mensajes de error nunca incluyen el cuerpo del correo ni el
  cuerpo de una respuesta del proveedor.
- La ruta de navegador de Google sigue siendo de solo lectura.
- `mail.categories.set` y `external.issues.transition` conservan intactas sus reglas.

## Riesgos

- **Envío irreversible.** Un correo enviado no se puede recuperar. La mitigación es la revisión
  humana del borrador en Gmail y la aprobación expresa del identificador antes del envío.
- **Destinatario equivocado.** Un agente podría escribir una dirección errada o inventarla. Los
  destinatarios solo llegan por opciones explícitas, se validan y se muestran en la vista previa; el
  skill prohíbe inventar destinatarios y exige confirmar la cuenta remitente con la persona usuaria.
- **Duplicados.** La búsqueda de Gmail es aproximada y solo cubre 30 días. La comparación final se
  hace sobre los encabezados leídos, no sobre el resultado de la búsqueda; aun así, un duplicado
  con otro asunto o con otros destinatarios no se detecta.
- **Resultado incierto.** Un error `5xx` o un corte de red después del `POST` no prueba que el envío
  falló. La orden lo informa como `write_unverified` y nunca reintenta.
- **Alcance amplio dentro de la conexión.** `gmail.compose` permite gestionar todos los borradores
  de la cuenta. El código solo crea borradores nuevos y solo envía el borrador nombrado.
- **Uso por agentes.** Un agente podría enviar en ciclos. El skill prohíbe los envíos en lote, los
  ciclos y el uso de `--allow-duplicate` sin aprobación expresa.

## Consecuencias

- Un agente puede preparar un correo de Gmail con recibo y vista previa, y enviarlo solo después de
  la aprobación de la persona, sin reimplementar OAuth ni llamadas a Gmail.
- Para enviar con la verificación de duplicados, la cuenta necesita las dos conexiones: `gmail`
  para leer Enviados y `gmail-compose` para redactar y enviar.
- La persona usuaria autoriza una vez el alcance `gmail.compose` con `connect gmail-compose`.

## Plan de reversión

Retirar `mail.draft.create` y `mail.draft.send` de `WRITE_CAPABILITIES`, del `TargetSpec` de
`gmail-compose` y del descriptor del adaptador deja la bandera `--allow-write` sin capacidad de
Gmail que autorizar. `solercia-connect disconnect gmail-compose <cuenta>` borra el perfil y el
token locales; la revocación remota la hace la persona en la configuración de seguridad de su cuenta
de Google. Los borradores creados quedan en Gmail y se borran desde allí. Los correos enviados no se
pueden revertir.

## Alternativas descartadas

- **El conector de Gmail de claude.ai:** crea borradores y envía correo, pero solo funciona dentro
  de sesiones de Claude. No sirve a los agentes que invocan `solercia-connect`, no deja el recibo
  determinista ni liga la escritura a la identidad verificada de la conexión guardada.
- **Agregar `gmail.compose` al target `gmail`:** invalidaría todas las concesiones de Gmail
  existentes y daría a la conexión de lectura la capacidad de enviar.
- **`gmail.modify` o `https://mail.google.com/`:** también permiten enviar, pero otorgan además
  lectura, modificación o borrado de todo el buzón.
- **Enviar sin borrador (`users.messages.send`):** elimina la revisión humana en Gmail.
- **Ampliar `gmail-compose` con `gmail.readonly` o `gmail.metadata` para la verificación de
  duplicados:** duplicaría en la conexión de redacción un privilegio que la conexión `gmail` ya
  tiene.
- **Exponer la escritura por HTTP o por la ruta de navegador:** un cliente de loopback o una sesión
  web podrían enviar correo sin una persona en la terminal ni la identidad de la API.

## Referencias oficiales

- [Method: users.drafts.create — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/create)
- [Method: users.drafts.get — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/get)
- [Method: users.drafts.send — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/send)
- [Method: users.messages.list — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list)
- [Method: users.messages.get — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get)
- [Method: users.getProfile — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile)
- [REST Resource: users.messages — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- [Format — Gmail API](https://developers.google.com/workspace/gmail/api/reference/rest/v1/Format)
- [Create and send draft emails — Gmail API](https://developers.google.com/workspace/gmail/api/guides/drafts)
- [Choose Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Refine searches in Gmail — Gmail Help](https://support.google.com/mail/answer/7190?hl=en)
