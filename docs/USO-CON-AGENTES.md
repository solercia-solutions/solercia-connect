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

## Dos costumbres que ahorran problemas

1. **Escribe siempre a archivo con `--output`.** Un mes de correos son cientos
   de miles de caracteres. Pasarlos por el contexto del modelo es caro y casi
   nunca hace falta: que el agente procese el archivo.
2. **Fija el rango de fechas.** Connect usa rangos exactos con fin exclusivo y
   no los amplía por su cuenta. Si el resultado es cero, es cero.

## Lo que nunca se comparte

- **El perfil del navegador y el llavero del sistema.** Contienen tu sesión.
  Copiarlos a otra máquina es entregar el acceso a tu cuenta.
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

Diagnóstico estructurado:

```bash
solercia-connect doctor outlook tu-correo@entidad.gov.co
```
