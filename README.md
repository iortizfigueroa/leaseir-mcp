# MCP de Leaseir

Servidor MCP que da acceso, en lenguaje natural desde Claude / ChatGPT / Gemini,
a los datos operativos de Leaseir:

- **Airtable** (base *Leaseir*): pedidos, y los equipos de cada cliente derivados de sus pedidos.
- **Jira**: incidencias de servicio técnico (SAT).
- **MongoDB**: telemetría / estado en tiempo real de los equipos.

Es la **v1 interna**: pensada para ti y tu equipo, protegida con un token.
El paso a multi-cliente (que cada cliente vea solo lo suyo, con OAuth) es la
siguiente fase y se construye sobre esta misma base.

> **Nota de diseño importante:** no existe una tabla global con todos los
> equipos fabricados y su dueño. Los equipos de un cliente se obtienen de sus
> **pedidos** (seriales en `ID_Console` / `ID_Handpiece`), o de un listado de
> seriales que aportes. El estado real de cada equipo se consulta por serial
> contra la telemetría de Mongo.

---

## Herramientas que expone

| Herramienta | Qué hace | Fuente |
|---|---|---|
| `equipos_de_cliente` | Equipos de un cliente derivados de sus pedidos (parcial, 2026) | Airtable |
| `equipos_de_cliente_parque` | Consolas de un cliente según el parque real (ownerMap) | ownerMap |
| `buscar_pedidos` | Busca pedidos por cliente / estado / país | Airtable |
| `crear_pedido_borrador` | **(escritura)** Crea un pedido en borrador (equipo/spray/extras) | Airtable |
| `buscar_incidencias_sat` | Busca incidencias SAT por cliente / estado / JQL | Jira |
| `detalle_incidencia_sat` | Detalle completo de una incidencia (p.ej. `SAT-123`) | Jira |
| `crear_incidencia_sat` | **(escritura)** Crea una incidencia de SAT | Jira |
| `uso_equipo` | Uso de una consola en un rango (mismos números que Cloudmed) | Mongo `cloudmed` |
| `uso_manipulo` | Uso de un manípulo (cruza consolas) en un rango | Mongo `cloudmed` |
| `actividad_dia` | Actividad por hora de un equipo/manípulo en un día (p.ej. `H03346` ayer) | Mongo `cloudmed` |
| `estado_equipo` | Disparos de hoy y última actividad de una consola | Mongo `cloudmed` |
| `estado_online` | Estado de conexión: disparando / en línea / offline (corto/largo) | Mongo `cloudmed` |
| `mantenimiento_equipo` | Térmico + condensador + salud de diodos de una consola | Mongo `cloudmed` |
| `serie_termica` | Serie diaria de temperaturas y voltaje de condensador | Mongo `cloudmed` |
| `salud_diodo` | Predicción de rotura de un diodo (calibraciones COMP) | Mongo `cloudmed` |

---

## 1. Configuración

```bash
cp .env.example .env
```

Rellena `.env`. Lo mínimo para empezar es Airtable; Jira y Mongo puedes dejarlos
para después (las herramientas de esas fuentes solo fallarán si las usas sin
configurar, el resto funciona).

**Datos que necesitas conseguir:**

- `MCP_AUTH_TOKEN`: invéntate uno largo →
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `AIRTABLE_API_KEY`: crea un token en https://airtable.com/create/tokens con
  scopes `data.records:read` y `data.records:write` sobre la base *Leaseir*.
- **Jira** — necesito que confirmes: `JIRA_BASE_URL` (p.ej. `https://leaseir.atlassian.net`),
  tu `JIRA_EMAIL`, un `JIRA_API_TOKEN`
  (https://id.atlassian.com/manage-profile/security/api-tokens) y sobre todo la
  **clave del proyecto de SAT** (`JIRA_PROJECT_KEY`: mírala en la URL de
  cualquier ticket, p.ej. `SAT-123` → `SAT`).
- **Mongo Cloudmed** — pon la misma `CLOUDMED_MONGO_URI` (read-only) que usa el
  portal Cloudmed en Vercel. La base (`cloudmed`), la colección de tramos
  (`pulses_handpiece_2026`) y la de mensajes (`messages`) ya vienen por defecto.
  La telemetría replica la agregación del portal (high-water por manípulo,
  anti-salto de contador, disparos estimados, reglas SP/Quad), leyendo el
  histórico de `pulses_handpiece_2026` y reconstruyendo el día en curso en vivo
  desde `messages`. El mapa serial→cliente del parque va embebido
  (`sources/ownerMap.json`); actualízalo cuando cambie el parque. El mantenimiento usa además la colección
  `messages` (temperaturas HPTS/I2C2 y condensador STAT) y `calibrations`
  (eventos COMP para la salud de diodos).

---

## 2. Ejecutar en local

```bash
pip install -r requirements.txt
python server.py
```

Levanta en `http://localhost:8000`. Comprueba:

```bash
curl http://localhost:8000/health      # -> {"status":"ok",...}
```

El endpoint MCP es `http://localhost:8000/mcp`.

---

## 3. Desplegar en Render (para usarlo desde el móvil / ChatGPT / Gemini)

Para que Claude en el móvil, ChatGPT o Gemini (que corren en la nube) lleguen al
servidor, tiene que estar en una URL pública. La forma más simple:

1. Sube esta carpeta a un repo de GitHub (privado).
2. En https://render.com → **New +** → **Blueprint**, apunta al repo (ya trae
   `render.yaml`).
3. Render detecta el servicio. En el panel, rellena las variables marcadas como
   *sync:false* (tokens y credenciales). **Pon siempre `MCP_AUTH_TOKEN`.**
4. Deploy. Te dará una URL tipo `https://leaseir-mcp.onrender.com`.
5. Tu endpoint MCP será `https://leaseir-mcp.onrender.com/mcp`.

---

## 4. Conectarlo a cada plataforma

En los tres casos el "secreto" va en la cabecera de autorización:
`Authorization: Bearer <MCP_AUTH_TOKEN>`.

### Claude (web o escritorio)
Settings → **Connectors** → **Add custom connector** → pega la URL
`https://.../mcp` y, en la cabecera, `Authorization: Bearer <tu token>`.
Una vez añadido desde el ordenador, queda disponible también en Claude para
iOS/Android en tu cuenta.

### ChatGPT
Requiere plan de pago y activar **Developer Mode** (Settings → Connectors →
Advanced). Luego **Add custom connector / MCP server** → misma URL y cabecera.

### Gemini
Vía **Gemini CLI** (fichero de configuración de MCP servers) o la Gemini API,
apuntando a la misma URL con la misma cabecera.

---

## 5. Seguridad y siguiente paso

- La v1 usa **un token compartido**: quien lo tenga, accede a todo. Vale para
  uso interno. **No repartas este token a clientes.**
- Para abrirlo a clientes hace falta la fase 2: **OAuth + aislamiento por
  cliente** (cada cliente autenticado ve solo sus pedidos/equipos/incidencias).
  Es más trabajo y se monta sobre este mismo servidor.
- No subas nunca el `.env` a git (ya está pensado para ir por variables de
  entorno en Render).
