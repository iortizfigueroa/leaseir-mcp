"""
Cliente ligero de Jira Cloud (REST API v3) para las incidencias de SAT.

Autenticación básica con email + API token (https://id.atlassian.com/manage/api-tokens).
Conexión perezosa: no falla al importar, solo al usarse sin credenciales.

Los datos NO se devuelven en crudo: se enriquecen igual que en el portal de Elha
(lib/statuses.ts + lib/jira.ts). Cada incidencia se clasifica en:
  - abierta / cerrada  (según los 30 estados "abiertos" de LEAS)
  - funnel A/B/C        (Gestión en taller / externa / online)
  - fase (bucket visual del funnel)
y se extraen los campos útiles (cliente/centro, consola, manípulo, bloqueante,
máquina de sustitución, forma de resolución).
"""
from __future__ import annotations

from typing import Any

import requests

import config


def _auth() -> tuple[str, str]:
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        raise RuntimeError(
            "Falta configuración de Jira (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)."
        )
    return (config.JIRA_EMAIL, config.JIRA_API_TOKEN)


def _base() -> str:
    return config.JIRA_BASE_URL.rstrip("/")


def _jql_escape(value: str) -> str:
    return value.replace('"', '\\"')


# ===========================================================================
# CLASIFICACIÓN DE ESTADOS (portado de elha-portal/lib/statuses.ts)
# ===========================================================================

FUNNEL_LABELS: dict[str, str] = {
    "A": "Gestión en taller",
    "B": "Gestión externa",
    "C": "Gestión online",
}

# Estado Jira crudo -> {bucket (fase visual), funnel}. Copia exacta del portal.
RAW_STATUS_TO_FUNNEL: dict[str, dict[str, str]] = {
    # Comunes (Abierto / Creado / Reportado / Solicitado)
    "Abierto": {"bucket": "Abierto", "funnel": "A"},
    "Creado": {"bucket": "Abierto", "funnel": "A"},
    "Reportado": {"bucket": "Abierto", "funnel": "A"},
    "Solicitado": {"bucket": "Abierto", "funnel": "A"},
    # Funnel A — Taller
    "Recepcionado SAT": {"bucket": "Pendiente recogida", "funnel": "A"},
    "Pendiente recogida": {"bucket": "Pendiente recogida", "funnel": "A"},
    "Gestionado transporte": {"bucket": "En transporte", "funnel": "A"},
    "Material enviado": {"bucket": "En transporte", "funnel": "A"},
    "Equipo enviado": {"bucket": "En transporte", "funnel": "A"},
    "En cola taller": {"bucket": "Recibido en taller", "funnel": "A"},
    "Pendiente asignar técnico": {"bucket": "Recibido en taller", "funnel": "A"},
    "En preparación presupuesto": {"bucket": "Diagnóstico y presupuesto", "funnel": "A"},
    "Presupuesto preparado pendiente de enviar": {"bucket": "Diagnóstico y presupuesto", "funnel": "A"},
    "Presupuesto enviado": {"bucket": "Pdte confirmación presupuesto", "funnel": "A"},
    "Pendiente confirmación presupuesto": {"bucket": "Pdte confirmación presupuesto", "funnel": "A"},
    "Presupuesto aceptado": {"bucket": "Pdte confirmación presupuesto", "funnel": "A"},
    "Esperando inicio reparación": {"bucket": "En reparación", "funnel": "A"},
    "En reparación": {"bucket": "En reparación", "funnel": "A"},
    "Inspección de salida": {"bucket": "Inspección de salida", "funnel": "A"},
    "Equipo devuelto": {"bucket": "En transporte a cliente", "funnel": "A"},
    "Devuelto a cliente": {"bucket": "En transporte a cliente", "funnel": "A"},
    # Funnel B — Externa
    "Pendiente definir servicio externo": {"bucket": "Pdte definir servicio externo", "funnel": "B"},
    "Enviado a técnico externo": {"bucket": "Enviado a técnico externo", "funnel": "B"},
    "Esperando respuesta cliente a presupuesto": {"bucket": "Enviado a técnico externo", "funnel": "B"},
    # Funnel C — Online
    "Pendiente agendar llamada": {"bucket": "Pdte agendar llamada", "funnel": "C"},
    "Formulario en curso": {"bucket": "Esperando inicio reparación", "funnel": "C"},
    "Formulario enviado a calidad": {"bucket": "Esperando inicio reparación", "funnel": "C"},
    "Investigación": {"bucket": "En reparación online", "funnel": "C"},
    "Queja creada": {"bucket": "En reparación online", "funnel": "C"},
    "En préstamo": {"bucket": "En reparación online", "funnel": "C"},
    # Estados CERRADOS
    "Resuelto": {"bucket": "Resuelto", "funnel": "C"},
    "Finalizada": {"bucket": "Finalizado A", "funnel": "A"},
    "Cancelado": {"bucket": "Cancelado", "funnel": "A"},
    "Finalizado técnico externo": {"bucket": "Finalizado B", "funnel": "B"},
}

# Los 30 estados "abiertos" de LEAS (replica de OPEN_STATUSES_JQL del portal).
# Cualquier estado que NO esté aquí se considera CERRADO.
OPEN_STATUSES: set[str] = {
    "Abierto", "Creado", "Devuelto a cliente", "En preparación presupuesto",
    "En cola taller", "En préstamo", "En reparación", "Enviado a técnico externo",
    "Equipo devuelto", "Equipo enviado", "Esperando inicio reparación",
    "Esperando respuesta cliente a presupuesto", "Formulario en curso",
    "Formulario enviado a calidad", "Gestionado transporte", "Inspección de salida",
    "Investigación", "Material enviado", "Pendiente agendar llamada",
    "Pendiente asignar técnico", "Pendiente confirmación presupuesto",
    "Pendiente definir servicio externo", "Pendiente recogida", "Presupuesto aceptado",
    "Presupuesto enviado", "Presupuesto preparado pendiente de enviar", "Queja creada",
    "Recepcionado SAT", "Reportado", "Solicitado",
}


def _open_statuses_jql() -> str:
    """Cláusula JQL 'status IN (...)' con los 30 estados abiertos."""
    return ", ".join('"' + _jql_escape(s) + '"' for s in sorted(OPEN_STATUSES))


def clasificar_estado(estado: str | None, forma_resolucion: str | None = None) -> dict[str, Any]:
    """Clasifica un estado Jira crudo igual que el portal de Elha.

    Devuelve: abierta (bool), funnel (A/B/C), funnel_label, fase (bucket).
    Si se conoce la 'forma de resolución' del ticket, ésta manda sobre el funnel
    (Interna->A, Externa->B, Online->C), como en lib/jira.ts.
    """
    estado = (estado or "").strip()
    mapa = RAW_STATUS_TO_FUNNEL.get(estado, {"bucket": "Abierto", "funnel": "A"})
    funnel = mapa["funnel"]
    fase = mapa["bucket"]

    if forma_resolucion:
        fr = forma_resolucion.lower()
        if "extern" in fr:
            funnel = "B"
        elif "online" in fr:
            funnel = "C"
        elif "intern" in fr:
            funnel = "A"

    return {
        "abierta": estado in OPEN_STATUSES,
        "funnel": funnel,
        "funnel_label": FUNNEL_LABELS.get(funnel, funnel),
        "fase": fase,
    }


# ===========================================================================
# CAMPOS PERSONALIZADOS DE JIRA (portado de elha-portal/lib/jira.ts)
# ===========================================================================

CF_CLIENTE = "customfield_10211"        # Cliente / centro
CF_CONSOLA = "customfield_10171"        # Nº de serie consola
CF_MANIPULO = "customfield_10150"       # Nº de serie manípulo (handpiece)
CF_MODELO = "customfield_10131"         # Modelo
CF_BLOQUEANTE = "customfield_10208"     # ¿Bloqueante? (equipo parado)
CF_SUSTITUCION = "customfield_10198"    # Máquina de sustitución entregada
CF_TIPO_AVERIA = "customfield_10140"    # Tipo de avería
CF_DESC_AVERIA = "customfield_10210"    # Descripción de la avería
CF_FORMA_RESOLUCION = "customfield_10128"  # Forma de resolución (Interna/Externa/Online)

_SEARCH_FIELDS = [
    "summary", "status", "assignee", "priority", "updated", "created", "reporter",
    CF_CLIENTE, CF_CONSOLA, CF_MANIPULO, CF_MODELO, CF_BLOQUEANTE,
    CF_SUSTITUCION, CF_FORMA_RESOLUCION,
]


def _cf_text(value: Any) -> str | None:
    """Extrae texto de un custom field que puede ser str, {'value':..} o {'name':..}."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return value.get("value") or value.get("name") or None
    if isinstance(value, list):
        parts = [_cf_text(v) for v in value]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None
    return str(value)


def _cf_bool(value: Any) -> bool:
    """Interpreta un custom field como booleano (checkbox / select Sí-No)."""
    if value is None:
        return False
    txt = _cf_text(value)
    if txt is None:
        return False
    return txt.strip().lower() in {"sí", "si", "yes", "true", "1", "bloqueante"}


def _enriquecer(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields", {}) or {}
    estado = (f.get("status") or {}).get("name")
    forma = _cf_text(f.get(CF_FORMA_RESOLUCION))
    clas = clasificar_estado(estado, forma)
    return {
        "key": issue.get("key"),
        "resumen": f.get("summary"),
        "estado": estado,
        "abierta": clas["abierta"],
        "funnel": clas["funnel"],
        "funnel_label": clas["funnel_label"],
        "fase": clas["fase"],
        "prioridad": (f.get("priority") or {}).get("name"),
        "asignado": (f.get("assignee") or {}).get("displayName"),
        "cliente": _cf_text(f.get(CF_CLIENTE)),
        "consola": _cf_text(f.get(CF_CONSOLA)),
        "manipulo": _cf_text(f.get(CF_MANIPULO)),
        "modelo": _cf_text(f.get(CF_MODELO)),
        "bloqueante": _cf_bool(f.get(CF_BLOQUEANTE)),
        "sustitucion": _cf_bool(f.get(CF_SUSTITUCION)),
        "forma_resolucion": forma,
        "actualizado": f.get("updated"),
        "creado": f.get("created"),
        "url": f"{_base()}/browse/{issue.get('key')}",
    }


# ===========================================================================
# BÚSQUEDA / DETALLE / CREACIÓN
# ===========================================================================

def buscar_incidencias(
    cliente: str | None = None,
    estado: str | None = None,
    jql_extra: str | None = None,
    solo_abiertas: bool = False,
    tipo: str | None = "Task",
    limit: int = 20,
) -> dict[str, Any]:
    """Busca incidencias de SAT en Jira y las devuelve YA clasificadas, con el
    MISMO racional que la pantalla de Incidencias del portal de Elha.

    - tipo="Task" (por defecto, como el portal): solo cuenta las incidencias de
      servicio técnico. Excluye los tickets de "Máquina de sustitución" y de
      "Revisión queja Calidad", que son de otros tipos. Pasa tipo=None para
      traer todos los tipos.
    - solo_abiertas=True filtra a los 30 estados "abiertos" de LEAS.
    - 'cliente' se busca en los CAMPOS Cliente/centro, serial de consola y serial
      de manípulo (NO en texto libre), para no arrastrar tickets de otros
      clientes que solo mencionen la palabra. Sirve tanto para nombre de cliente
      ("Elha") como para un serial ("40679", "C00519").
    - Devuelve un resumen (total, abiertas, cerradas, por funnel) + la lista de
      incidencias enriquecidas (abierta, funnel, fase, cliente, consola,
      manípulo, bloqueante, sustitución...).
    """
    clauses = [f"project = {config.JIRA_PROJECT_KEY}"]
    # Igual que el portal: solo incidencias de servicio técnico (type = Task).
    if tipo:
        clauses.append(f'issuetype = "{_jql_escape(tipo)}"')
    # Filtro por CAMPOS (Cliente/centro + seriales), no por texto libre.
    if cliente:
        c = _jql_escape(cliente)
        clauses.append(f'(cf[10211] ~ "{c}" OR cf[10171] ~ "{c}" OR cf[10150] ~ "{c}")')
    if estado:
        clauses.append(f'status = "{_jql_escape(estado)}"')
    elif solo_abiertas:
        clauses.append(f"status IN ({_open_statuses_jql()})")
    if jql_extra:
        clauses.append(f"({jql_extra})")
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"

    # Nuevo endpoint de búsqueda de Jira Cloud: POST /rest/api/3/search/jql
    # (el antiguo GET /rest/api/3/search fue retirado por Atlassian → 410 Gone).
    resp = requests.post(
        f"{_base()}/rest/api/3/search/jql",
        auth=_auth(),
        json={"jql": jql, "maxResults": limit, "fields": _SEARCH_FIELDS},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    issues = resp.json().get("issues", [])
    enriquecidas = [_enriquecer(i) for i in issues]

    abiertas = [i for i in enriquecidas if i["abierta"]]
    cerradas = [i for i in enriquecidas if not i["abierta"]]
    por_funnel: dict[str, int] = {}
    for i in abiertas:
        por_funnel[i["funnel_label"]] = por_funnel.get(i["funnel_label"], 0) + 1

    return {
        "resumen": {
            "total": len(enriquecidas),
            "abiertas": len(abiertas),
            "cerradas": len(cerradas),
            "abiertas_por_funnel": por_funnel,
            "solo_abiertas": solo_abiertas,
        },
        "incidencias": enriquecidas,
    }


def detalle_incidencia(issue_key: str) -> dict[str, Any]:
    fields = ",".join(
        _SEARCH_FIELDS
        + ["description", "comment", CF_TIPO_AVERIA, CF_DESC_AVERIA]
    )
    resp = requests.get(
        f"{_base()}/rest/api/3/issue/{issue_key}",
        auth=_auth(),
        params={"fields": fields},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    i = resp.json()
    base = _enriquecer(i)
    f = i.get("fields", {}) or {}
    base.update(
        {
            "tipo_averia": _cf_text(f.get(CF_TIPO_AVERIA)),
            "descripcion_averia": _cf_text(f.get(CF_DESC_AVERIA)),
            "descripcion": _adf_to_text(f.get("description")),
        }
    )
    return base


def crear_incidencia(
    resumen: str,
    descripcion: str,
    cliente: str | None = None,
    prioridad: str | None = None,
) -> dict[str, Any]:
    desc = descripcion
    if cliente:
        desc = f"Cliente: {cliente}\n\n{descripcion}"

    fields: dict[str, Any] = {
        "project": {"key": config.JIRA_PROJECT_KEY},
        "issuetype": {"name": config.JIRA_ISSUE_TYPE},
        "summary": resumen,
        "description": _text_to_adf(desc),
    }
    if prioridad:
        fields["priority"] = {"name": prioridad}

    resp = requests.post(
        f"{_base()}/rest/api/3/issue",
        auth=_auth(),
        json={"fields": fields},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"key": data.get("key"), "url": f"{_base()}/browse/{data.get('key')}"}


# --- Jira usa el formato ADF (Atlassian Document Format) en descripciones ---

def _text_to_adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def _adf_to_text(adf: Any) -> str:
    if not isinstance(adf, dict):
        return str(adf or "")
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)
            if node.get("type") == "paragraph":
                parts.append("\n")
        elif isinstance(node, list):
            for n in node:
                walk(n)

    walk(adf)
    return "".join(parts).strip()
