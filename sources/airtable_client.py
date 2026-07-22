"""
Cliente ligero de Airtable (REST API) para la base Leaseir.

Usa `requests` directamente para que sea transparente y sin dependencias raras.
La conexión es perezosa: no falla al importar, solo cuando se usa sin credenciales.
"""
from __future__ import annotations

from typing import Any

import requests

import config

_API = "https://api.airtable.com/v0"


def _headers() -> dict[str, str]:
    if not config.AIRTABLE_API_KEY:
        raise RuntimeError(
            "Falta AIRTABLE_API_KEY. Rellénalo en el .env o en las variables de Render."
        )
    return {
        "Authorization": f"Bearer {config.AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def _url(table: str) -> str:
    return f"{_API}/{config.AIRTABLE_BASE_ID}/{requests.utils.quote(table)}"


def _escape(value: str) -> str:
    """Escapa comillas para usar el texto dentro de una fórmula de Airtable."""
    return value.replace('"', '\\"')


def list_records(
    table: str,
    formula: str | None = None,
    max_records: int = 20,
    fields: list[str] | None = None,
    sort_field: str | None = None,
    sort_desc: bool = True,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"maxRecords": max_records, "pageSize": min(max_records, 100)}
    if formula:
        params["filterByFormula"] = formula
    if fields:
        params["fields[]"] = fields
    if sort_field:
        params["sort[0][field]"] = sort_field
        params["sort[0][direction]"] = "desc" if sort_desc else "asc"

    resp = requests.get(_url(table), headers=_headers(), params=params, timeout=30)
    resp.raise_for_status()
    records = resp.json().get("records", [])
    # Devolvemos algo plano y legible para el modelo: id + campos.
    return [{"id": r["id"], **r.get("fields", {})} for r in records]


def create_record(table: str, fields: dict[str, Any]) -> dict[str, Any]:
    payload = {"fields": fields, "typecast": True}
    resp = requests.post(_url(table), headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], **data.get("fields", {})}


# ---------------------------------------------------------------------------
# Helpers de negocio
#
# NOTA: NO existe una tabla global con "todos los equipos fabricados y su
# dueño". Los equipos que tiene un cliente se derivan de sus PEDIDOS (los
# seriales entregados viven en ID_Console / ID_Handpiece de la tabla Pedidos),
# o de un listado de seriales que el usuario aporte. El estado real de cada
# equipo se consulta luego por serial contra la telemetría de MongoDB.
# ---------------------------------------------------------------------------

def equipos_de_cliente(customer: str, limit: int = 50) -> list[dict[str, Any]]:
    """Equipos de un cliente derivados de sus pedidos (tabla Pedidos).

    PARCIAL: solo cubre los pedidos registrados en Airtable (histórico
    limitado), no todo el parque histórico del cliente. Para equipos antiguos,
    usa un listado de seriales aportado por el usuario y consúltalos en Mongo.

    Devuelve, por cada pedido del cliente, los seriales entregados
    (ID_Console / ID_Handpiece) y la configuración del equipo. Estos seriales
    son los que luego se consultan en Mongo para ver el estado en tiempo real.
    """
    formula = f'FIND(LOWER("{_escape(customer)}"), LOWER({{Customer}}&"")) > 0'
    return list_records(
        config.AIRTABLE_TABLE_PEDIDOS,
        formula=formula,
        max_records=limit,
        fields=[
            "Customer",
            "Centro",
            "Country",
            "Console",
            "Spot Size",
            "Wavelenght",
            "ID_Console",
            "ID_Handpiece",
            "Status",
            "Delivery Date Donet",
            "Guarantee",
        ],
        sort_field="Creada",
    )


def buscar_pedidos(
    customer: str | None = None,
    estado: str | None = None,
    country: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conds: list[str] = []
    if customer:
        conds.append(f'FIND(LOWER("{_escape(customer)}"), LOWER({{Customer}}&"")) > 0')
    if estado:
        conds.append(f'FIND(LOWER("{_escape(estado)}"), LOWER({{Status}}&"")) > 0')
    if country:
        conds.append(f'FIND(LOWER("{_escape(country)}"), LOWER({{Country}}&"")) > 0')

    formula = None
    if conds:
        formula = "AND(" + ", ".join(conds) + ")" if len(conds) > 1 else conds[0]

    return list_records(
        config.AIRTABLE_TABLE_PEDIDOS,
        formula=formula,
        max_records=limit,
        fields=[
            "Customer",
            "Centro",
            "Country",
            "Number of devices",
            "Console",
            "Spot Size",
            "Wavelenght",
            "Type of request",
            "Status",
            "Priority",
            "Expected Date",
            "Total Price",
            "Commercial Lead",
            "ID_Console",
            "ID_Handpiece",
            "Tracking URL",
        ],
        sort_field="Creada",
    )


def crear_pedido_borrador(
    customer: str,
    type_of_request: str,
    centro: str | None = None,
    number_of_devices: int | None = None,
    console: str | None = None,
    number_of_bottles: int | None = None,
    extras: list[str] | None = None,
    type_of_sale: str | None = None,
    country: str | None = None,
    contact: str | None = None,
    email: str | None = None,
    comments: str | None = None,
) -> dict[str, Any]:
    note = "[BORRADOR creado vía MCP Leaseir]"
    full_comments = f"{note}\n{comments}" if comments else note

    fields: dict[str, Any] = {
        "Customer": customer,
        "Type of request": type_of_request,
        "Comments": full_comments,
    }
    if centro:
        fields["Centro"] = centro
    if number_of_devices is not None:
        fields["Number of devices"] = number_of_devices
    if console:
        fields["Console"] = console
    if number_of_bottles is not None:
        fields["Number of bottles (1 box = 6 bottles)"] = number_of_bottles
    if extras:
        fields["Additional Extras"] = extras
    if type_of_sale:
        fields["Type of sale"] = type_of_sale
    if country:
        fields["Country"] = country
    if contact:
        fields["Contact"] = contact
    if email:
        fields["Email"] = email
    if config.AIRTABLE_DRAFT_STATUS:
        fields["Status"] = config.AIRTABLE_DRAFT_STATUS

    return create_record(config.AIRTABLE_TABLE_PEDIDOS, fields)
