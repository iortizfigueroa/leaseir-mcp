# Desplegar el MCP de Leaseir en Render — guía paso a paso

Tiempo: ~15 min. Los pasos marcados con 🔑 son tuyos (credenciales/OAuth/secretos);
el resto te lo puedo conducir yo en Chrome.

---

## Requisitos previos
- Una cuenta de GitHub (la tuya, `iortizfigueroa`).
- Una cuenta de Render (https://render.com) — gratis para empezar.
- La `CLOUDMED_MONGO_URI` read-only (la misma que usa elha-portal en Vercel).

---

## Paso 1 · 🔑 Subir el código a un repo de GitHub

Descomprime `leaseir-mcp.zip`. Dentro de la carpeta `leaseir-mcp`:

```bash
cd leaseir-mcp
git init
git add .
git commit -m "MCP de Leaseir"
```

Crea un repo VACÍO y PRIVADO en GitHub (https://github.com/new), por ejemplo
`leaseir-mcp`. Luego conéctalo y sube:

```bash
git remote add origin https://github.com/iortizfigueroa/leaseir-mcp.git
git branch -M main
git push -u origin main
```

> El `.gitignore` ya excluye `.env`, así que no subes ningún secreto. Perfecto.

---

## Paso 2 · 🔑 Crear el servicio en Render (Blueprint)

1. Entra en https://dashboard.render.com
2. **New +** → **Blueprint**.
3. **Connect GitHub** → autoriza a Render a ver tu repo `leaseir-mcp` (🔑 tu OAuth).
4. Selecciona el repo. Render detecta el `render.yaml` que ya viene incluido.
5. **Apply** / **Create**.

---

## Paso 3 · 🔑 Rellenar las variables de entorno (secretos)

Render te pedirá los valores marcados como *sync:false*. Rellénalos tú:

| Variable | Valor |
|---|---|
| `MCP_AUTH_TOKEN` | Invéntate uno largo: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `CLOUDMED_MONGO_URI` | La URI read-only del Mongo `cloudmed` (la de Vercel) |
| `AIRTABLE_API_KEY` | Token de https://airtable.com/create/tokens (scopes read+write sobre base Leaseir) |
| `AIRTABLE_DRAFT_STATUS` | `Pendiente de Aprobación` |
| `JIRA_BASE_URL` | `https://leaseir.atlassian.net` |
| `JIRA_EMAIL` | `iortiz@leaseir.com` |
| `JIRA_API_TOKEN` | Token de https://id.atlassian.com/manage-profile/security/api-tokens |
| `JIRA_PROJECT_KEY` | La clave del proyecto de SAT (mírala en cualquier ticket: `XXX-123`) |

El resto (`CLOUDMED_DB`, `CLOUDMED_COLLECTION`, etc.) ya vienen con su valor por defecto.

---

## Paso 4 · Deploy y comprobación

1. Render construye e inicia el servicio (~2-3 min). Verás el log.
2. Te da una URL tipo `https://leaseir-mcp.onrender.com`.
3. Comprueba salud:
   ```bash
   curl https://leaseir-mcp.onrender.com/health   # -> {"status":"ok",...}
   ```
4. Tu endpoint MCP es: `https://leaseir-mcp.onrender.com/mcp`

---

## Paso 5 · 🔑 Conectarlo en Claude

1. Claude (web o escritorio) → **Settings → Connectors → Add custom connector**.
2. **URL**: `https://leaseir-mcp.onrender.com/mcp`
3. **Header** de autorización: `Authorization: Bearer <tu MCP_AUTH_TOKEN>`
4. Guarda. Una vez añadido desde el ordenador, te aparece también en el móvil.

En ChatGPT sería lo mismo desde el **Developer Mode**; en Gemini vía CLI/API.

---

## Prueba de fuego (paridad con Cloudmed)

En el chat de Claude, con el conector puesto:

> "Con el MCP de Leaseir, dame la actividad del manípulo H03346 el 2026-07-20."

Compara el resultado con lo que marca el portal Cloudmed. Deberían cuadrar
(mismo motor: `pulses_handpiece_2026` + high-water). Otras pruebas útiles:

- "¿Qué equipos tiene Elha en el parque?" → `equipos_de_cliente_parque`
- "¿Cómo está de salud el equipo 00461?" → `mantenimiento_equipo`
- "¿Qué pedidos abiertos tiene Smart Duck?" → `buscar_pedidos`

---

## Notas
- El plan gratis de Render "duerme" el servicio tras inactividad; la primera
  llamada tras dormir tarda unos segundos en despertar. Para uso serio, el plan
  de pago más básico lo mantiene despierto.
- Si cambias el código: `git push` y Render redespliega solo.
- El token `MCP_AUTH_TOKEN` es la llave de todo: no lo repartas a clientes (esta
  es la v1 interna).
