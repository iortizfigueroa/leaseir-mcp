"""
Cliente ligero de Jira Cloud (REST API v3) para las incidencias de SAT.

Autenticación básica con email + API token (https://id.atlassian.com/manage/api-tokens).
Conexión perezosa: no falla al importar, solo al usarse sin credenciales.
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


def buscar_incidencias(
    cliente: str | None = None,
    estado: str | None = None,
    jql_extra: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses = [f"project = {config.JIRA_PROJECT_KEY}"]
    if cliente:
        clauses.append(f'text ~ "{_jql_escape(cliente)}"')
    if estado:
        clauses.append(f'status = "{_jql_escape(estado)}"')
    if jql_extra:
        clauses.append(f"({jql_extra})")
    jql = " AND ".join(clauses) + " ORDER BY updated DESC"

    # Nuevo endpoint de búsqueda de Jira Cloud: POST /rest/api/3/search/jql
    # (el antiguo GET /rest/api/3/search fue retirado por Atlassian → 410 Gone).
    # El JQL va en el body, los campos como array, y la paginación es por
    # nextPageToken (aquí solo pedimos la primera página con maxResults).
    resp = requests.post(
        f"{_base()}/rest/api/3/search/jql",
        auth=_auth(),
        json={
            "jql": jql,
            "maxResults": limit,
            "fields": ["summary", "status", "assignee", "priority", "updated", "created", "reporter"],
        },
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    issues = resp.json().get("issues", [])
    out: list[dict[str, Any]] = []
    for i in issues:
        f = i.get("fields", {})
        out.append(
            {
                "key": i.get("key"),
                "resumen": f.get("summary"),
                "estado": (f.get("status") or {}).get("name"),
                "prioridad": (f.get("priority") or {}).get("name"),
                "asignado": (f.get("assignee") or {}).get("displayName"),
                "actualizado": f.get("updated"),
                "creado": f.get("created"),
                "url": f"{_base()}/browse/{i.get('key')}",
            }
        )
    return out


def detalle_incidencia(issue_key: str) -> dict[str, Any]:
    resp = requests.get(
        f"{_base()}/rest/api/3/issue/{issue_key}",
        auth=_auth(),
        params={"fields": "summary,status,assignee,priority,updated,created,description,comment"},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    i = resp.json()
    f = i.get("fields", {})
    return {
        "key": i.get("key"),
        "resumen": f.get("summary"),
        "estado": (f.get("status") or {}).get("name"),
        "prioridad": (f.get("priority") or {}).get("name"),
        "asignado": (f.get("assignee") or {}).get("displayName"),
        "actualizado": f.get("updated"),
        "creado": f.get("created"),
        "descripcion": _adf_to_text(f.get("description")),
        "url": f"{_base()}/browse/{i.get('key')}",
    }


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
