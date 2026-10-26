# Usar Solercia Connect con un agente de IA

Guía para quien va a leer su propio correo, calendario o actas de reunión desde
su equipo, con Claude Code u otro agente.

Lo primero que conviene entender: Connect **corre en tu máquina**, con tu sesión
y tus credenciales. No es un servicio al que te conectas ni hay una cuenta
central. Nadie más ve tus datos.

## 1. Instalar el runtime

Requiere Python 3.12 o superior.

```bash
git clone https://github.com/solercia-solutions/solercia-connect.git
cd solercia-connect
uv tool install .
solercia-connect --help
```

Si no tienes `uv`, sirve `pipx install .`.

## 2. Configurar

Crea tu propio archivo de entorno, fuera del repositorio:

```bash
mkdir -p ~/.solercia
cat > ~/.solercia/connect.env <<'EOF'
# Ruta por navegador: no necesita registrar ninguna aplicación.
SOLERCIA_CONNECT_BROWSER_FALLBACK=true
# Opcional: permite que el servicio local (solercia-connect-service) abra ese
# mismo navegador supervisado cuando otro programa se lo pide por HTTP.
# Requiere además SOLERCIA_CONNECT_API_KEY. Por defecto está apagado.
# SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN=true
EOF

export SOLERCIA_CONNECT_ENV_FILE=~/.solercia/connect.env
```

Agrega ese `export` a tu `~/.zshrc` para no repetirlo.

> El archivo guarda configuración, nunca contraseñas. Connect no pide ni
> almacena tu contraseña: eso ocurre en tu navegador, contra Microsoft.

> `SOLERCIA_CONNECT_HTTP_INTERACTIVE_LOGIN` solo tiene sentido si otro
> programa de tu máquina (por ejemplo, Solercia Command) consume el servicio
> local y necesita iniciar la sesión de Microsoft sin pasar por la terminal.
> Con el interruptor encendido, `POST /v1/connections/connect` abre la
> ventana del navegador en tu equipo; la contraseña y el MFA siguen
> ocurriendo en la página de Microsoft, nunca viajan por HTTP.

### Las dos rutas

| Ruta | Qué necesita | Cuándo usarla |
|---|---|---|
| **Navegador** | Nada más que tu sesión | Empieza por aquí. Funciona sin trámites |
| **Oficial (OAuth)** | Una aplicación registrada en Entra y, para actas, el consentimiento del administrador del tenant | Cuando exista esa aplicación |

La ruta por navegador abre una ventana **una vez**, inicias sesión y el perfil
queda guardado. No vuelve a pedirlo hasta que la sesión caduque.

## 3. Conectar la cuenta

```bash
solercia-connect connect outlook tu-correo@entidad.gov.co --allow-browser-fallback
solercia-connect status  outlook tu-correo@entidad.gov.co --allow-browser-fallback
```

## 4. Leer

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability mail.sent.list \
  --start '2026-08-01T00:00:00-05:00' \
  --end   '2026-09-01T00:00:00-05:00' \
  --account tu-correo@entidad.gov.co \
  --allow-browser-fallback \
  --output correos.json
```

Cambia `--capability` por `mail.inbox.list`, `calendar.events.list` o
`meeting.transcript.get` según lo que necesites. Con `mail.inbox.list`, si el
número de registros devueltos es igual a `--limit` (hasta 1000), la lectura
pudo quedar truncada: estrecha la ventana y vuelve a leer.

Cada correo de Outlook trae `categories`, con los nombres de las categorías que
tiene el mensaje (lista vacía si no tiene ninguna). Sirve para conocer el estado
que el equipo le asignó a un caso. El catálogo de categorías del buzón no está
disponible.

## 5. Asignar categorías (la única escritura)

`mail.categories.set` cambia las categorías de un correo. Es la única operación
que escribe en el buzón y exige `--allow-write` en cada invocación:

```bash
solercia-connect run \
  --provider microsoft.graph \
  --capability mail.categories.set \
  --account tu-correo@entidad.gov.co \
  --message-id '<campo id de un registro de mail.inbox.list>' \
  --add 'Resuelto' --remove 'Pendiente' \
  --allow-write \
  --allow-browser-fallback
```

- `--message-id` es el campo `id` del registro, no `internet_message_id`. Cambia
  si alguien mueve el correo de carpeta: en ese caso vuelve a leer la bandeja.
- `--add` y `--remove` se repiten y se combinan en una sola escritura;
  `--replace` fija la lista completa.
- Si el correo ya tiene esas categorías no se escribe nada y la respuesta dice
  `changed: false`. Si alguien del equipo lo cambió al mismo tiempo, la orden
  falla con `write_conflict` y no pisa ese cambio.
- Hoy solo funciona por la ruta de navegador y nunca por el servicio HTTP.
  Pruébala primero con una categoría que el equipo no use.

## 6. Instalar el skill para tu agente

El skill le enseña al agente cómo invocar el runtime: qué comandos existen, qué
capacidad corresponde a cada necesidad, y qué NO debe improvisar. Sin él, el
agente adivina los comandos.

### Opción A — para todos tus proyectos (recomendada)

```bash
mkdir -p ~/.claude/skills
ln -s "$(pwd)/agents/skills/solercia-connect" ~/.claude/skills/solercia-connect
```

Queda disponible en cualquier carpeta donde abras Claude Code, y `git pull`
sobre el clon lo actualiza solo.

### Opción B — solo en un proyecto

```bash
mkdir -p /ruta/a/tu-proyecto/.claude/skills
ln -s /ruta/al/clon/agents/skills/solercia-connect \
      /ruta/a/tu-proyecto/.claude/skills/solercia-connect
```

### Opción C — sin skill

Si prefieres no instalar nada, agrega esto al `CLAUDE.md` de tu proyecto:

```markdown
## Lectura de correo, calendario y actas

Usa el comando `solercia-connect run` con `--capability`, `--start`, `--end`,
`--account`, `--allow-browser-fallback` y `--output`. Nunca amplíes el rango de
fechas que te den: si no hay resultados, son cero. Escribe siempre a archivo con
`--output`; no vuelques el contenido en la conversación.
```

Verifica que el agente lo reconoce preguntándole qué skills tiene disponibles.

## 7. Por proyecto

Cuando un agente trabaja dentro de un repositorio y necesita Gmail o Jira sin que
vuelvas a ingresar las credenciales en cada sesión, activa el **modo proyecto**.
Connect guarda entonces todo su estado en una sola carpeta del proyecto: la
configuración, los perfiles, las sesiones de navegador, los secretos y
`connect.env`. La decisión está en
[`decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md`](decisions/2026-10-25-modo-proyecto-secretos-en-archivo.md).

> **Riesgo que aceptas.** En modo proyecto los tokens quedan en texto plano en
> `.solercia-connect/secrets`. Cualquier agente o proceso con permiso de lectura
> sobre esa carpeta (en la práctica, cualquier programa que corra con tu
> usuario) puede leerlos. Connect impide que entren a git, pero no los protege
> de copias, respaldos ni carpetas sincronizadas. No uses el modo proyecto en
> una carpeta compartida o sincronizada con la nube.

### Flujo recomendado

1. Instala el runtime de forma global (sección 1) y el skill para todos tus
   proyectos (sección 6, opción A).
2. Activa el descubrimiento **una sola vez**, en tu perfil de shell (por
   ejemplo, `~/.zshrc`):

   ```bash
   export SOLERCIA_CONNECT_PROJECT_DISCOVERY=1
   ```

3. En cada proyecto, desde cualquier carpeta del repositorio:

   ```bash
   solercia-connect project init
   ```

   Crea `.solercia-connect` en la raíz git con permisos `0700`, su `.gitignore`
   y un `connect.env` con permisos `600` que solo trae los nombres de las
   variables, comentados y sin valores. Si `connect.env` ya existe, no lo toca.
   La respuesta JSON indica la carpeta, lo que creó y los pasos siguientes.
4. Llena `connect.env` (ver más abajo).
5. Conecta una vez cada cuenta.
6. Desde ese momento, los agentes solo llaman `solercia-connect …` dentro del
   repositorio. No hace falta exportar nada más ni usar lanzadores por
   proyecto.

| Ruta dentro de la carpeta | Contenido |
|---|---|
| `config/` | Perfiles de conexión, sin secretos |
| `data/` | Sesiones de navegador: `data/m365`, `data/google` y `data/jira` |
| `secrets/` | Tokens OAuth y API tokens, un archivo por clave |
| `connect.env` | Tus variables de entorno para este proyecto |
| `.gitignore` | Contiene `*`: git ignora toda la carpeta |

No hace falta tocar el `.gitignore` del repositorio. Si git ya rastrea algún
archivo dentro de la carpeta, Connect se detiene y te indica qué ruta sacar del
índice.

### Cómo encuentra Connect la carpeta

- Si `SOLERCIA_CONNECT_HOME` está definida, siempre gana.
- Si no, y el descubrimiento está activo, Connect sube desde el directorio de
  trabajo hasta el primer directorio que contiene `.git` (carpeta o archivo, así
  que los worktrees cuentan) y usa `.solercia-connect` de esa raíz.
- Si esa carpeta no existe o no hay raíz git, Connect trabaja en modo global,
  como siempre. Nunca usa una carpeta `.solercia-connect` que esté por debajo de
  la raíz git.
- Si la carpeta es un enlace simbólico, no es un directorio, no es privada o git
  rastrea algo dentro de ella, Connect se detiene con un error tipado antes de
  leer cualquier variable. Así, una carpeta `.solercia-connect` que venga dentro
  de un repositorio clonado nunca se usa.
- `SOLERCIA_CONNECT_PROJECT_DISCOVERY` solo se lee del entorno del proceso o de
  `SOLERCIA_CONNECT_ENV_FILE`, nunca de `connect.env`. Sin ella, Connect no
  busca ninguna carpeta.

`doctor` indica cómo se resolvió la carpeta en `project_home_source`:
`explicit` o `discovered`.

### Alternativa: `SOLERCIA_CONNECT_HOME` explícita

Si prefieres no activar el descubrimiento, nombra la carpeta tú mismo en cada
sesión:

```bash
cd /ruta/a/tu-proyecto
export SOLERCIA_CONNECT_HOME="$PWD/.solercia-connect"
```

La ruta debe ser absoluta y sin enlaces simbólicos. `solercia-connect project
init` también sirve para crearla. Sin esta variable y sin el descubrimiento,
Connect sigue usando las carpetas XDG y el llavero del sistema, como siempre.

### Llenar `connect.env`

`project init` deja el archivo creado, con permisos `600`. Ábrelo con tu editor,
quita el `#` de las variables que vayas a usar y escribe su valor, una por línea
con la forma `NOMBRE=valor`:

| Variable | Para qué |
|---|---|
| `SOLERCIA_CONNECT_GOOGLE_CLIENT_ID` | Cliente OAuth de escritorio de Google, para Gmail |
| `SOLERCIA_CONNECT_GOOGLE_CLIENT_SECRET` | Solo si tu cliente de Google lo exige |
| `JIRA_API_TOKEN` (o el nombre que elijas) | API token de Jira, que lee `--token-env` |
| `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_ID` y `SOLERCIA_CONNECT_ATLASSIAN_CLIENT_SECRET` | Solo para Jira por OAuth |
| `SOLERCIA_CONNECT_MICROSOFT_CLIENT_ID` y `SOLERCIA_CONNECT_MICROSOFT_TENANT` | Solo para la ruta oficial de Microsoft 365 |

- Si el archivo no tiene permisos `600`, Connect se detiene y te indica el
  `chmod 600` que falta. Nunca muestra los valores.
- Una variable del entorno del proceso gana sobre `SOLERCIA_CONNECT_ENV_FILE`, y
  esta gana sobre `connect.env`.
- `connect.env` no puede definir `SOLERCIA_CONNECT_HOME`,
  `SOLERCIA_CONNECT_ENV_FILE` ni `SOLERCIA_CONNECT_PROJECT_DISCOVERY`.
- Nunca pegues estos valores en la conversación con el agente: los escribes tú,
  en el archivo.

### Conectar una vez

```bash
solercia-connect connect gmail tu-correo@entidad.gov.co
solercia-connect connect jira tu-correo@entidad.gov.co \
  --site https://tu-sitio.atlassian.net --token-env JIRA_API_TOKEN
```

Gmail abre el navegador para que autorices el acceso. Para Jira, `--token-env`
lee el API token de la variable que nombras y nunca lo muestra; no se combina
con `--token-stdin`. Si prefieres Jira por OAuth, con la aplicación de
Atlassian configurada en `connect.env`:

```bash
solercia-connect connect jira tu-correo@entidad.gov.co \
  --site https://tu-sitio.atlassian.net --token-kind oauth
```

Revisa el estado con `doctor`. Su informe trae `project_mode`: la carpeta, si
cada parte existe y es privada, el `.gitignore` y el resultado de la revisión de
git; y `project_home_source`, que dice si la carpeta se nombró de forma
explícita o se descubrió. Nunca incluye valores secretos.

```bash
solercia-connect doctor jira tu-correo@entidad.gov.co
```

### Usar desde un agente

Con el descubrimiento activo en tu perfil de shell y el skill instalado, el
agente no necesita nada más: invoca `solercia-connect …` desde cualquier carpeta
del repositorio y Connect usa la carpeta del proyecto. Los comandos no cambian:

```bash
solercia-connect run \
  --provider jira.cloud \
  --capability external.issues.search \
  --start '2026-08-01T00:00:00-05:00' \
  --end   '2026-09-01T00:00:00-05:00' \
  --account tu-correo@entidad.gov.co \
  --output incidencias.json
```

Si usas la alternativa explícita en lugar del descubrimiento, el agente necesita
la misma variable en su sesión. Para que no lo olvide, agrega esto al
`CLAUDE.md` del proyecto:

```markdown
## Solercia Connect en modo proyecto

Antes de usar `solercia-connect`, ejecuta
`export SOLERCIA_CONNECT_HOME="$PWD/.solercia-connect"` desde la raíz del
repositorio. Nunca leas ni muestres el contenido de `.solercia-connect`.
```

### Usar como librería

`run_application` es el punto de entrada de Python que usan la CLI y el
servicio HTTP. Recibe la misma solicitud que `POST /v1/run` y lee el mismo
entorno que la CLI, así que respeta el modo proyecto sin configuración extra:

```python
import os

from solercia_connect.application import run_application

os.environ["SOLERCIA_CONNECT_HOME"] = "/ruta/a/tu-proyecto/.solercia-connect"

resultado = run_application(
    {
        "type": "jira",
        "account": "tu-correo@entidad.gov.co",
        "capability": "external.issues.search",
        "start": "2026-08-01T00:00:00-05:00",
        "end": "2026-09-01T00:00:00-05:00",
    }
)
print(resultado.ok, resultado.status)
```

El resultado es el mismo sobre `solercia.connect/v1` de la CLI; `to_dict()` lo
convierte en un diccionario. Con el descubrimiento activo en lugar de la
variable explícita, la carpeta se busca desde el directorio de trabajo del
proceso de Python.

### Ejemplo: un repositorio como `pandora_proxy`

```bash
# Una sola vez, en ~/.zshrc:
export SOLERCIA_CONNECT_PROJECT_DISCOVERY=1

cd /ruta/a/pandora_proxy
solercia-connect project init
# Con tu editor, completa en .solercia-connect/connect.env
# SOLERCIA_CONNECT_GOOGLE_CLIENT_ID y JIRA_API_TOKEN.
solercia-connect connect gmail usuario@entidad.example
solercia-connect connect jira usuario@entidad.example \
  --site https://sitio.atlassian.net --token-env JIRA_API_TOKEN
solercia-connect doctor jira usuario@entidad.example
```

Desde ese momento, cualquier agente que trabaje en ese repositorio lee Gmail y
Jira sin volver a pedir credenciales. Para dejar de usar el modo proyecto, quita
`SOLERCIA_CONNECT_PROJECT_DISCOVERY` (o `SOLERCIA_CONNECT_HOME`) de tu entorno;
si ya no vas a volver a usarlo, borra la carpeta `.solercia-connect` y revoca
los tokens en el proveedor.

## Dos costumbres que ahorran problemas

1. **Escribe siempre a archivo con `--output`.** Un mes de correos son cientos
   de miles de caracteres. Pasarlos por el contexto del modelo es caro y casi
   nunca hace falta: que el agente procese el archivo.
2. **Fija el rango de fechas.** Connect usa rangos exactos con fin exclusivo y
   no los amplía por su cuenta. Si el resultado es cero, es cero.

## Lo que nunca se comparte

- **El perfil del navegador y el llavero del sistema.** Contienen tu sesión.
  Copiarlos a otra máquina es entregar el acceso a tu cuenta.
- **La carpeta `.solercia-connect` del modo proyecto.** Guarda tus tokens en
  texto plano.
- **Tu archivo `.env` con valores reales.** Se comparte la plantilla, no el
  archivo lleno.
- **La llave del servicio local** (`SOLERCIA_CONNECT_API_KEY`), si llegas a
  levantar el servicio HTTP. Cada persona genera la suya.

## Si algo falla

| Síntoma | Causa habitual |
|---|---|
| Pide iniciar sesión otra vez | La sesión caducó: repite `connect` |
| `browser_account_mismatch` | El navegador tiene abierta otra cuenta |
| `action_required` | Hay que completar la autorización en la CLI, no por el servicio HTTP |
| Cero registros | El rango está vacío. No lo amplíes: revisa las fechas |
| `configuration_invalid` | Falta el archivo de entorno o la variable que nombra el error |
| `configuration_invalid` que menciona `SOLERCIA_CONNECT_HOME` o `.solercia-connect` | Permisos, `.gitignore` o archivos rastreados por git: sigue la indicación del error |
| `doctor` no trae `project_mode` con el descubrimiento activo | No hay `.solercia-connect` en la raíz git: ejecuta `solercia-connect project init` |

Diagnóstico estructurado:

```bash
solercia-connect doctor outlook tu-correo@entidad.gov.co
```
