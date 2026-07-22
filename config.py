"""
Configuración central del MCP de Leaseir.

Toda la configuración se lee de variables de entorno (o de un fichero .env en local).
Nada de secretos escritos en el código. Copia .env.example a .env y rellénalo.
"""
from __future__ import annotations

import os

try:
    # Carga .env en desarrollo local. En producción (Render) las variables
    # se inyectan directamente y python-dotenv simplemente no encuentra fichero.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv es opcional
    pass


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if val is not None:
        val = val.strip()
    return val or default


# --- Servidor ---------------------------------------------------------------
HOST = _get("HOST", "0.0.0.0")
PORT = int(_get("PORT", "8000"))
# Token estático para proteger el servidor en la v1 interna. Cualquiera que
# se conecte debe enviar  Authorization: Bearer <MCP_AUTH_TOKEN>.
# Déjalo vacío SOLO para pruebas locales. En Render, ponlo siempre.
MCP_AUTH_TOKEN = _get("MCP_AUTH_TOKEN", "")
# URL pública del servicio (Render la inyecta como RENDER_EXTERNAL_URL). Se usa
# como base para los endpoints OAuth que consume el conector de Claude/ChatGPT.
PUBLIC_BASE_URL = _get("RENDER_EXTERNAL_URL") or _get("PUBLIC_BASE_URL") or f"http://localhost:{PORT}"

# --- Airtable (base "Leaseir") ---------------------------------------------
AIRTABLE_API_KEY = _get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = _get("AIRTABLE_BASE_ID", "app9U5sz7YS8y9Oit")  # base Leaseir
AIRTABLE_TABLE_PEDIDOS = _get("AIRTABLE_TABLE_PEDIDOS", "Pedidos")
# Valor de Status que se pondrá a un pedido creado desde el MCP (borrador).
# Debe existir como opción en el campo Status de Pedidos, o déjalo vacío para
# no tocar el Status y marcar el borrador solo en los comentarios.
AIRTABLE_DRAFT_STATUS = _get("AIRTABLE_DRAFT_STATUS", "Pendiente de Aprobación")

# --- Jira (SAT) -------------------------------------------------------------
JIRA_BASE_URL = _get("JIRA_BASE_URL")  # p.ej. https://leaseir.atlassian.net
JIRA_EMAIL = _get("JIRA_EMAIL")
JIRA_API_TOKEN = _get("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = _get("JIRA_PROJECT_KEY", "SAT")
JIRA_ISSUE_TYPE = _get("JIRA_ISSUE_TYPE", "Task")

# --- MongoDB Cloudmed (telemetría real de los equipos) ----------------------
# Mismo Mongo Atlas que usa el portal Cloudmed. Usuario READ-ONLY.
CLOUDMED_MONGO_URI = _get("CLOUDMED_MONGO_URI")  # mongodb+srv://...  (read-only)
CLOUDMED_DB = _get("CLOUDMED_DB", "cloudmed")
# Colección derivada de tramos ya parseados (la "pulses new 2026"). El overview
# de Cloudmed lee de aquí; el día en curso se reconstruye en vivo desde messages.
CLOUDMED_COLLECTION = _get("CLOUDMED_COLLECTION", "pulses_handpiece_2026")
CLOUDMED_MESSAGES_COLLECTION = _get("CLOUDMED_MESSAGES_COLLECTION", "messages")
# Zona horaria para agrupar día/hora (los timestamps en Mongo son UTC).
CLOUDMED_TZ = _get("CLOUDMED_TZ", "Europe/Madrid")
