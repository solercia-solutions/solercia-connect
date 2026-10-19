# Decisiones de plataforma: Solercia Connect y Solercia Command

- **Estado:** Aceptado
- **Fecha de corte:** 2026-08-23
- **Última actualización:** 2026-10-19
- **Ámbito:** Identidad de producto, independencia operativa, integraciones, autenticación, límites de seguridad, persistencia y convenciones de datos
- **Aplicación:** Obligatoria para cambios nuevos dentro de este ámbito

## Propósito y regla de lectura

Este registro consolida las decisiones vigentes para que Solercia Connect y Solercia Command
puedan evolucionar sin reconstruir el contexto ni introducir acoplamientos accidentales. Debe
leerse antes de diseñar o modificar arquitectura, persistencia, integraciones, autenticación,
identidad de proveedor, modelado de datos o relaciones entre productos.

Las decisiones aceptadas de este documento son normativas. Si un cambio las contradice, no se
debe aplicar una excepción implícita: hace falta una decisión o ADR nuevo, con sus consecuencias,
y la actualización de este registro. Los documentos históricos conservan su valor como evidencia;
este registro fija la línea base aceptada al 2026-08-23.

## Mapa rápido de responsabilidades

| Producto | Responsabilidad | Límite |
| --- | --- | --- |
| **Solercia Connect** | Runtime conector transversal para Outlook/Microsoft 365, Teams, Gmail, Google Calendar, Jira y GLPI. | Obtiene y normaliza hechos externos; no decide prioridades ni estados de trabajo. |
| **Solercia Command** | Producto de trabajo, proyectos, tareas, calendario y priorización; es el nombre aprobado del checkout anteriormente llamado `gestion_proyectos`. | Es propietario del comportamiento operativo de trabajo; no reimplementa conectores. |
| **Solercia Security** | Producto separado que puede consumir contratos de integración. | No es una dependencia de runtime de Connect ni de Command. |

## Decisiones normativas aceptadas

### 1. Identidad y propiedad funcional

1. Solercia Connect concentra la integración determinista con los proveedores admitidos.
2. Solercia Command es el nombre de producto aprobado y `solercia-command` es el checkout local
   aprobado. Los identificadores históricos de base de datos, API, paquetes, despliegue, remotos,
   dominios o marca jurídica no se renombran por inferencia.
3. Command es propietario de proyectos, tareas, calendarios de trabajo y decisiones de
   priorización. Connect entrega hechos externos normalizados; no los convierte por sí mismo en
   tareas internas.
4. Security conserva su dominio y ciclo de vida propios. Su ausencia no impide iniciar ni operar
   el núcleo de Connect o Command.

### 2. Despliegue independiente e integración opcional

1. Connect y Command deben poder iniciar, probarse, desplegarse y entregar su comportamiento
   principal aunque el otro repositorio, servicio o base de datos no exista.
2. Se prohíben la base de datos compartida, las entidades ORM compartidas, las claves foráneas
   entre bases, las importaciones directas del repositorio hermano y las dependencias obligatorias
   de arranque.
3. La integración se realiza mediante puertos de aplicación, adaptadores y contratos explícitos.
   Un fallo de Connect degrada las capacidades respaldadas por conectores; no invalida el núcleo
   de Command.
4. Cada producto conserva su estado, transacciones, migraciones y despliegue. Los cruces usan
   contratos versionables y consistencia explícita, no acceso lateral a tablas.

### 3. Enrutamiento y autenticación de conectores

1. La ruta primaria es siempre la API oficial con OAuth oficial o el mecanismo oficial de
   autenticación del proveedor.
2. Playwright es únicamente un fallback para Microsoft. Debe ser explícito, tipado, incluido en
   listas permitidas, determinista y fail-closed mientras no estén disponibles los permisos del
   tenant. La configuración global solo habilita su disponibilidad: cada intento, incluidos
   status, health y los alias m365, requiere `--allow-browser-fallback` en esa invocación.
3. El usuario realiza el inicio de sesión interactivo, la contraseña y el MFA. Los prompts y logs
   de agentes nunca solicitan ni registran credenciales, cookies o tokens.
4. Ningún LLM elige rutas, selectores, reintentos, identidad de cuenta o clasificación de fallos.
   Esas decisiones pertenecen a código y contratos deterministas.
5. Las pruebas offline no demuestran estabilidad de APIs reales, permisos de tenant, selectores
   web, sesiones de navegador ni contratos vivos con proveedores.
6. El fallback de navegador selecciona el destino por `ConnectionType`: Outlook abre Outlook y
   Teams abre Teams. La ventana visible se usa únicamente para login/MFA y se cierra de forma
   intencional para persistir la sesión en el perfil privado y acotado por cuenta.
7. No existe un daemon de navegador. Después del cierre visible, Connect reabre inmediatamente el
   mismo perfil en modo headless y exige la misma identidad observada antes de guardar
   `connected_browser`; los estados posteriores repiten una apertura headless acotada y la cierran.
   El cierre final libera el perfil y el comando síncrono devuelve el control a la consola; no queda
   un proceso retenido ni un trabajador en segundo plano.
8. La prueba de identidad del fallback es específica por producto. Outlook usa exclusivamente el
   `EmailAddress` canónico del `GET https://outlook.office.com/api/v2.0/me` autenticado por la propia
   sesión web; no usa selectores DOM de identidad. El Bearer se acepta solo desde hosts HTTPS
   Outlook/Substrate permitidos, permanece transitorio, no se registra ni persiste y se elimina en
   éxito o error. Teams conserva su estrategia DOM versionada y acotada. Esta prueba de identidad no
   habilita extracción de buzón ni modifica el estado `not_implemented` del navegador.
9. Connect es de solo lectura con una única excepción, aceptada el 2026-10-19 en
   [`2026-10-19-escritura-categorias-outlook.md`](2026-10-19-escritura-categorias-outlook.md):
   asignar categorías de Outlook a un mensaje existente (`mail.categories.set`). Cada invocación
   exige `--allow-write`, apagada por defecto; la ruta de navegador exige además
   `--allow-browser-fallback`. La única escritura es un `PATCH` cuyo cuerpo contiene solo las
   categorías, condicionado a la versión leída, después de verificar la identidad del buzón. El
   servicio HTTP la rechaza por diseño. La ruta oficial necesita `Mail.ReadWrite` y queda pendiente
   de una decisión de alcances, porque agregarlo al target `outlook` invalida las concesiones
   existentes. Ninguna otra escritura queda autorizada.

### 4. Frontera de seguridad e identidad de cuenta

1. Antes de cualquier lectura de datos debe observarse y probarse una única identidad autenticada
   canónica. Se compara sin distinguir mayúsculas y minúsculas con todas las cuentas configuradas
   o solicitadas aplicables.
2. Una identidad ausente, malformada, ambigua o distinta produce cero lecturas de datos del
   proveedor o navegador y un fallo tipado.
3. La procedencia usa la identidad observada y verificada; nunca sustituye esa evidencia por una
   identidad solicitada sin verificar.
4. OAuth usa endpoints oficiales fijos, PKCE, rotación o preservación correcta de refresh tokens
   y un almacén seguro de tokens inyectado. No existe fallback a texto plano.
5. El polling de dispositivo queda acotado por cantidad de intentos, demora máxima local y tiempo
   restante de expiración.
6. El servicio de conexión usado por la CLI es propietario del listener loopback acotado, la
   correlación exacta de `state`, PKCE S256 y verifier, y el cierre del callback. El adaptador HTTP
   nunca inicia ni espera un flujo interactivo.
7. Los perfiles locales no secretos se escriben de forma atómica y privada bajo XDG. Tokens y
   refresh tokens permanecen exclusivamente en un `OAuthTokenStore` seguro inyectado; las
   variables de access token se conservan solo como bootstrap de desarrollo explícito.
8. La lectura de perfiles rechaza directorios o archivos públicos, enlaces simbólicos, tipos no
   regulares y ownership ajeno cuando la plataforma lo expone. Nunca corrige permisos inseguros de
   forma silenciosa. Doctor y Playwright comparten el mismo auditor de rutas de perfil.

### 5. Arquitectura y contratos de integración

1. Los scripts, la biblioteca Python y la CLI Typer instalada son el runtime fuente de Connect. La
   CLI es la interfaz canónica para OAuth y login interactivo; el servicio FastAPI en loopback es
   un adaptador no interactivo sobre los mismos servicios de aplicación. Skills y Markdown
   documentan y orquestan; nunca sustituyen el comportamiento determinista del código.
2. Los proveedores se implementan como adaptadores detrás de puertos semánticos. Los consumidores
   reciben DTOs normalizados y neutrales al proveedor, procedencia y recibos explícitos.
3. Los rangos temporales son exactos y con fin exclusivo. Reintentos, fallback y fallos usan
   códigos de razón deterministas.
4. Los identificadores de proveedor son opacos y quedan calificados por su fuente en las
   fronteras de integración.
5. La ingestión debe usar claves de negocio o idempotencia que impidan duplicados ante reintentos
   o relecturas.
6. La lista y descarga oficial de transcripciones de Teams para un meeting ID conocido están
   implementadas contra Graph v1.0 y probadas offline. Sus scopes, políticas de tenant y contenido
   siguen pendientes de evidencia viva. La extracción por navegador no está implementada y una
   sesión conectada no constituye una promesa de extracción.
7. CLI y HTTP comparten el sobre versionado `solercia.connect/v1`, errores estructurados y DTOs
   normalizados con schema propio; ningún adaptador expone cuerpos crudos del proveedor.

### 6. Convención de persistencia

1. La mecánica CRUD reutilizable puede usar genéricos locales al producto y al stack.
2. Los puertos expuestos a la aplicación son específicos por agregado, expresan operaciones
   semánticas y pueden extender la mecánica CRUD sin reducir el dominio a CRUD.
3. Los repositorios no ejecutan `commit` ni cierran la transacción. El caso de uso es propietario
   de la transacción.
4. `UnitOfWork` se usa solo cuando un flujo atómico coordina varios repositorios.
5. Toda lectura, actualización o eliminación multi-tenant incluye el scope de tenant.
6. PostgreSQL, SQLAlchemy, TypeORM y las capacidades SQL particulares permanecen dentro de
   adaptadores y migraciones.
7. El dominio y los contratos de integración son neutrales a la base de datos. Un adaptador futuro
   para MySQL, MariaDB u Oracle es posible, pero no se promete como migración de costo cero.
8. UUID en Command y BIGINT en Security son decisiones físicas locales válidas. Los contratos
   externos usan identificadores opacos y calificados por fuente.

### 7. Convención de nombres y datos

| Elemento | Convención aceptada |
| --- | --- |
| Tablas | Plural en `lower_snake_case`. |
| Columnas | Nombres semánticos en `lower_snake_case`, sin prefijos que codifiquen el tipo físico. |
| Clave primaria | `id`, con tipo y generador definidos localmente por cada producto. |
| Tenant | Invariantes explícitas en lecturas, escrituras y relaciones. |
| Tiempo | Instantes UTC y semántica temporal explícita. |
| Auditoría | Actores diferenciados según la acción, por ejemplo creación y actualización. |
| Ciclo de vida | Valores portables en contratos; optimizaciones del motor aisladas en adaptadores. |

## Línea base verificada al 2026-08-23

### Solercia Command

- La migración local de nombre está validada con **17/17** elementos.
- La ruta activa anterior tiene **cero referencias operativas**; las menciones históricas del
  registro de migración se conservan deliberadamente.
- Se preservaron la rama `preview` y el HEAD
  `c256634a580f8fa1308a1484783d1bbef420cb40`.
- El árbol actual contiene **28 rutas modificadas o no rastreadas**, ninguna staged. Es utilizable
  para continuar, pero no es una línea base limpia.
- El ADR 0009 conserva el contexto detallado de separación entre Command, Connect y Security. El
  registro de migración conserva la evidencia del cambio de nombre local.

### Solercia Connect

- El roadmap actual está en **36/62** elementos completados; los 26 elementos abiertos conservan
  explícitamente la validación viva y el endurecimiento externo pendiente.
- La última puerta offline registrada pasó **248 pruebas**, omitió de forma segura **4 pruebas
  vivas** sin opt-in y alcanzó **84,20 %** de cobertura de ramas. El build soportado es offline y
  sin aislamiento después de `make setup`; un setup de caché vacía todavía requiere red,
  artefactos disponibles o un wheelhouse. El wheelhouse vendorizado sigue pendiente.
- Ruff format/check, mypy, `uv lock --check`, build externo, instalación fresca de wheel,
  smoke de CLI/wrapper/Make/servicio, gitleaks con redacción, escaneo de rutas/credenciales y el
  manifiesto final sin eliminaciones inesperadas quedaron en verde para esta corrección.
- El repositorio continúa sin commits ni remotos y todos sus archivos siguen sin rastrear. Git no
  puede ofrecer un diff histórico independiente mientras no exista un commit base.
- Esta evidencia es offline: no acredita permisos, credenciales, sesiones, selectores ni contratos
  reales de proveedores.

### Entrega de este registro

- No se autorizaron commits, staging, push ni cambios de remotos para este trabajo.
- Se recomienda crear commits base separados para Command y Connect antes de mezclar trabajo no
  relacionado. Esta recomendación no autoriza realizarlos.

## Trabajo diferido y validación viva pendiente

- Obtener permisos de tenant o administración y validar contratos vivos de Microsoft y Google.
- Demostrar los scopes oficiales requeridos para transcripciones de Teams.
- Validar los selectores de identidad de Teams y la extracción real de Microsoft después de que el
  usuario complete login y MFA. La identidad de Outlook ya no depende de selectores DOM; la forma
  REST `/api/v2.0/me` cuenta con evidencia viva para UAECD, pero cada comando vivo de Connect sigue
  sujeto a su validación explícita por tenant.
- Añadir, cuando el despliegue lo requiera, un `OAuthTokenStore` de gestor de secretos externo;
  keyring ya cubre el almacenamiento seguro local.
- Validar Jira y GLPI contra instalaciones reales admitidas.
- Implementar la ingestión e integración efectiva de Connect en Command, incluidos puertos,
  adaptadores, idempotencia y degradación operativa.

Estos puntos siguen pendientes hasta contar con evidencia viva o una implementación específica.
No deben presentarse como capacidades terminadas.

## No objetivos

- Crear una suite monolítica o una base de datos compartida.
- Hacer que Connect, Command o Security sean dependencias obligatorias entre sí.
- Reemplazar APIs oficiales por automatización de navegador.
- Trasladar lógica determinista a prompts, skills o documentos.
- Renombrar por arrastre esquemas, tablas, APIs, paquetes, despliegues, remotos, dominios o marca
  jurídica de Command.
- Prometer portabilidad de base de datos sin costo, estabilidad de UI externa o disponibilidad de
  capacidades no validadas.
- Implementar en este registro la ingestión de Command, conectores adicionales, migraciones o
  cambios de código.

## Referencias locales del repositorio

- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md): límites de dominio, puertos, runtime, OAuth y fallback.
- [`SECURITY.md`](../../SECURITY.md): identidad autenticada, secretos, logging y política fail-closed.
- [`docs/ROADMAP.md`](../ROADMAP.md): estado implementado frente a validación viva pendiente.
- [`2026-10-19-escritura-categorias-outlook.md`](2026-10-19-escritura-categorias-outlook.md):
  excepción de escritura para las categorías de Outlook.

Las referencias anteriores amplían este registro. Si una descripción histórica queda menos
precisa que una decisión aceptada aquí, debe corregirse mediante un cambio documental explícito,
no mediante una excepción silenciosa.
