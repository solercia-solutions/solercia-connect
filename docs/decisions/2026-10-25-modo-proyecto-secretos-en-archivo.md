# Decisión: modo proyecto con secretos en archivo

- **Estado:** Aceptada
- **Fecha de decisión:** 2026-10-25
- **Decidido por:** Camilo Calderón, propietario del producto
- **Ámbito:** Ubicación de la configuración, los perfiles de conexión, las sesiones de navegador,
  los secretos locales y las variables de entorno cuando Connect trabaja dentro de un proyecto
- **Aplicación:** Obligatoria cuando `SOLERCIA_CONNECT_HOME` está definida o cuando
  `SOLERCIA_CONNECT_PROJECT_DISCOVERY` está activa y encuentra la carpeta en la raíz git (sección
  [Descubrimiento opcional (D3.1)](#descubrimiento-opcional-d31)). Sin ninguna de las dos el
  comportamiento no cambia
- **Actualiza:**
  [`2026-08-23-solercia-platform-decisions.md`](2026-08-23-solercia-platform-decisions.md)
  (decisiones 4.4 y 4.7), [`SECURITY.md`](../../SECURITY.md) y el docstring de
  `KeyringOAuthTokenStore` en `src/solercia_connect/oauth.py`
- **Documento de trabajo:**
  [`odd/tasks/google-jira-browser-fallback.md`](../../odd/tasks/google-jira-browser-fallback.md)
  (decisión D3 y fase 7)

## Contexto

Hasta esta decisión, Connect guarda sus datos locales en tres lugares fijos: la configuración y
los perfiles de conexión bajo las carpetas XDG del usuario, los tokens OAuth y los API tokens en el
llavero del sistema operativo, y las variables de entorno solo en el archivo que nombra
`SOLERCIA_CONNECT_ENV_FILE`. Las reglas vigentes lo dicen de forma expresa:

| Fuente | Texto vigente hasta esta decisión |
| --- | --- |
| Registro de plataforma, decisión 4.4 | «OAuth usa endpoints oficiales fijos, PKCE, rotación o preservación correcta de refresh tokens y un almacén seguro de tokens inyectado. No existe fallback a texto plano.» |
| Registro de plataforma, decisión 4.7 | «Los perfiles locales no secretos se escriben de forma atómica y privada bajo XDG. Tokens y refresh tokens permanecen exclusivamente en un `OAuthTokenStore` seguro inyectado» |
| `SECURITY.md`, almacenamiento | «`KeyringOAuthTokenStore` is the local default; plaintext token files are forbidden.» |
| `SECURITY.md`, almacenamiento | «Load environment files only through the explicit `SOLERCIA_CONNECT_ENV_FILE` path. Never infer a current-directory `.env`.» |
| `src/solercia_connect/oauth.py` | «Store OAuth state in an OS keyring instead of plaintext project files.» |

El pedido de Camilo del 2026-10-25 fue: «que esas claves […] se pueda llamar desde la variable de
entorno para que no haya que ponerle las claves a cada rato […] y que esos archivos de callback y
de sesión […] se guarden […] dentro del proyecto». Ante las opciones de almacenamiento eligió
«Archivo en el proyecto».

### Caso de uso

Un agente de IA que trabaja dentro del repositorio git `IDARTES/pandora_proxy` necesita leer Gmail
y Jira sin que el usuario vuelva a ingresar las credenciales en cada sesión. El agente puede correr
en arneses distintos (terminal, subprocesos, entornos aislados), y en ellos el llavero del sistema
operativo no siempre está disponible.

## Decisión

1. **Activación.** El modo proyecto se activa con
   `SOLERCIA_CONNECT_HOME=<proyecto>/.solercia-connect`. El valor debe ser una ruta absoluta
   después de expandir `~`; una ruta relativa se rechaza con un error tipado, de modo que la carpeta
   nunca depende del directorio de trabajo. Ningún componente de la ruta puede ser un enlace
   simbólico.
2. **Una sola carpeta.** Esa carpeta concentra todo el estado local de Connect para el proyecto:

   | Ruta | Contenido |
   | --- | --- |
   | `config/` | Perfiles de conexión no secretos |
   | `data/` | Perfiles de navegador por familia: `data/m365`, `data/google` y `data/jira` |
   | `secrets/` | Tokens OAuth y API tokens, un archivo por clave |
   | `connect.env` | Variables de entorno del proyecto |
   | `.gitignore` | Contenido exacto `*` |

   Con el modo activo, `XDG_CONFIG_HOME` y `XDG_DATA_HOME` no se usan para las rutas de Connect.
   Las variables explícitas `SOLERCIA_CONNECT_M365_PROFILE_DIR`,
   `SOLERCIA_CONNECT_GOOGLE_PROFILE_DIR` y `SOLERCIA_CONNECT_JIRA_PROFILE_DIR` siguen prevaleciendo.
3. **Secretos en archivo.** Solo en modo proyecto, un almacén en archivo reemplaza al llavero del
   sistema operativo. Implementa el mismo protocolo `KeyringBackend`, de modo que
   `KeyringOAuthTokenStore` y `KeyringSecretStore` lo usan sin cambios. Cada par
   (servicio, usuario) ocupa un archivo cuyo nombre es el resumen SHA-256 de `servicio\0usuario`.
4. **Variables del proyecto.** Si existe `<home>/connect.env`, se carga como la capa de menor
   precedencia: primero el entorno del proceso, luego `SOLERCIA_CONNECT_ENV_FILE` y por último
   `connect.env`. Se aplican las mismas reglas de lectura y validación del archivo explícito. El
   comando `connect jira … --token-env NOMBRE` lee el API token de esa variable y nunca lo muestra.
5. **Sin la variable, nada cambia.** Connect sigue usando XDG, el llavero del sistema operativo y
   las mismas reglas del archivo de entorno. Solercia Command y los demás consumidores no cambian.

## Protecciones

- La carpeta y sus subcarpetas tienen permisos 0700, se comprueba que pertenezcan al usuario y se
  rechazan los enlaces simbólicos.
- Los archivos tienen permisos 0600 y se escriben de forma atómica: archivo temporal en la misma
  carpeta, `fsync` y `os.replace`. Se rechazan los archivos que son enlaces simbólicos o que
  pertenecen a otro usuario.
- Connect crea `<home>/.gitignore` con el contenido `*` cuando no existe. Si existe con otro
  contenido, falla cerrado con una indicación de cómo corregirlo y no sobrescribe el archivo.
- Guarda de git, antes de escribir cualquier secreto y al conectar:
  - si algún ancestro de la carpeta contiene `.git` (carpeta o archivo, de modo que los worktrees
    cuentan), Connect ejecuta `git ls-files` sobre la carpeta con un tiempo máximo acotado;
  - cualquier ruta rastreada produce un fallo cerrado que lista solo rutas relativas;
  - si git no está disponible o responde con error, el resultado también es un fallo cerrado;
  - si ningún ancestro contiene `.git`, la guarda no aplica;
  - fuera del modo proyecto, es decir, sin `SOLERCIA_CONNECT_HOME` y sin una carpeta descubierta,
    Connect nunca invoca git.
- `connect.env` debe ser un archivo regular con permisos 0600, del usuario y sin enlaces
  simbólicos. En otro caso, Connect falla cerrado e indica el `chmod` necesario.
- Ningún mensaje, diagnóstico ni sobre de resultado contiene valores secretos.

## Descubrimiento opcional (D3.1)

La decisión D3.1, aceptada por el propietario, reemplaza el lanzador por proyecto por la CLI
global y el skill global. Para que un agente no tenga que exportar `SOLERCIA_CONNECT_HOME` en cada
sesión, Connect puede encontrar por sí mismo la carpeta del proyecto, siempre que el usuario lo
habilite.

1. **Variable de activación.** `SOLERCIA_CONNECT_PROJECT_DISCOVERY` es un valor booleano y se
   interpreta con las mismas reglas que los demás interruptores de Connect. Se lee del entorno del
   proceso o del archivo explícito `SOLERCIA_CONNECT_ENV_FILE`, nunca de `connect.env`. Un valor
   que no sea booleano es un error de configuración.
2. **Orden de resolución.** Es determinista:
   1. Si `SOLERCIA_CONNECT_HOME` está definida, siempre gana.
   2. Si no lo está y el descubrimiento está activo, Connect sube desde el directorio de trabajo
      hasta el primer directorio que contiene `.git`, sea carpeta o archivo, de modo que los
      worktrees cuentan. Esa es la raíz git; para encontrarla no se ejecuta git.
      - Si `<raíz>/.solercia-connect` existe y es un directorio real, esa es la carpeta del
        proyecto.
      - Si es un enlace simbólico o no es un directorio, Connect falla cerrado con un error tipado.
      - Si no existe, o si no hay raíz git, Connect trabaja en modo global. Nunca toma una carpeta
        `.solercia-connect` del directorio de trabajo que no esté en la raíz git.
   3. En cualquier otro caso, Connect trabaja en modo global.
3. **Mismas protecciones.** La carpeta descubierta pasa por las mismas guardas que la explícita:
   preparación de la estructura, permisos privados, `.gitignore` con contenido exacto `*` y guarda
   de git. Además, antes de leer `connect.env`, Connect comprueba que git no rastree nada dentro de
   la carpeta y que la carpeta sea privada.
4. **Carpeta rastreada por git.** Un repositorio clonado puede traer una carpeta
   `.solercia-connect` versionada, por ejemplo con un `connect.env` que apunte a una aplicación
   OAuth ajena. Como git la rastrea, el descubrimiento falla cerrado con
   `ProjectHomeTrackedError` antes de cargar cualquier variable de ese archivo, y el mensaje lista
   solo rutas relativas.
5. **`project init`.** `solercia-connect project init [--path <dir>]` crea
   `<raíz git>/.solercia-connect` con las mismas guardas. Escribe `connect.env` con permisos 0600
   solo si no existe, con los nombres de las variables conocidas comentados y sin valores; nunca
   sobrescribe un archivo existente. Funciona con el descubrimiento activo o inactivo y, fuera de un
   repositorio git, exige `--path` con una ruta absoluta. Su respuesta JSON indica la carpeta, lo
   que creó y los pasos siguientes.
6. **Diagnóstico.** `doctor` informa `project_home_source`, con el valor `explicit` o
   `discovered`.

### Por qué existe la variable de activación

`SECURITY.md` prohíbe inferir un `.env` del directorio actual, y el README promete que Connect no
carga en silencio un `.env` de ese directorio. Un agente cambia de directorio con frecuencia y
puede trabajar dentro de repositorios de terceros; si el descubrimiento estuviera activo por
defecto, cualquier repositorio podría ofrecer su propia carpeta de configuración. La variable deja
la decisión en manos del usuario, una sola vez y en su perfil de shell. Sin ella, el comportamiento
es exactamente el anterior: no hay descubrimiento y `SOLERCIA_CONNECT_HOME` sigue siendo explícita.

## Riesgo aceptado

Los tokens OAuth (de acceso y de renovación) y los API tokens quedan en texto plano en disco, dentro
de `<home>/secrets`. Cualquier agente o proceso con acceso de lectura a esa carpeta puede leerlos.
En la práctica, los permisos 0600 y 0700 impiden la lectura por otros usuarios del sistema, pero no
por los procesos que corren con el mismo usuario.

El `.gitignore` y la guarda de git evitan que los secretos entren a un commit, pero no cubren otras
copias de la carpeta, como respaldos, archivos comprimidos o carpetas sincronizadas con la nube. Por
eso el proyecto no debe estar en una carpeta sincronizada ni compartida.

Si se sospecha una exposición, `disconnect` borra el estado local y la revocación se hace en el
portal del proveedor, como ya indica `SECURITY.md`.

## Alternativas consideradas

- **Llavero del sistema operativo con un nombre de servicio por proyecto:** descartada por el
  propietario, porque depende de que el llavero esté disponible desde cada arnés en el que corre un
  agente.
- **Mantener el comportamiento actual:** el estado del agente quedaría ligado a las carpetas XDG y
  al llavero del usuario, y no al proyecto, que es lo contrario del pedido.

## Plan de reversión

Retirar `SOLERCIA_CONNECT_HOME` y `SOLERCIA_CONNECT_PROJECT_DISCOVERY` del entorno devuelve
Connect a XDG y al llavero del sistema operativo. Los perfiles y secretos del proyecto no se migran: las conexiones se vuelven a crear con
`connect` en el modo normal. La carpeta `.solercia-connect` permanece en disco hasta que el usuario
la retire; como contiene secretos en texto plano, conviene borrarla y revocar los tokens que ya no
se usen.

## Límites que no cambian

- El llavero del sistema operativo sigue siendo el almacenamiento por defecto.
- La API oficial sigue siendo la ruta primaria. El modo proyecto no cambia el enrutamiento, la
  prueba de identidad ni el fallback de navegador.
- La única escritura sigue siendo `mail.categories.set` de Outlook, con `--allow-write`.
- El adaptador HTTP y sus reglas no cambian.
- Nunca se registran ni se muestran credenciales, cookies, tokens ni cabeceras de autorización.

## Estado de implementación

Esta decisión no implementa código. Las tareas T7.1 a T7.4 del documento de trabajo la
implementan, y la tarea T7.6 implementa el descubrimiento opcional y `project init`. La tarea T7.5 configura `IDARTES/pandora_proxy`; como toca otro repositorio, requiere
autorización explícita, y el inicio de sesión lo hace Camilo.
