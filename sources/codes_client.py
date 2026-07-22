"""
Cliente de la Activation Codes API de Leaseir.

La API genera el código de activación DIARIO de un equipo a partir del número de
serie del MANÍPULO (handpiece), no el de la consola. El código son 8 dígitos en
formato de TEXTO (puede empezar por cero) y solo es válido para su fecha.

Docs: BASE/health, GET /codes/{serial}, GET /codes?serials=...
Sin autenticación (acceso abierto). El aislamiento por cliente se hará en la
capa del MCP (Fase II multi-tenant), no aquí.
"""
from __future__ import annotations

from typing import Any

import requests

import config


def _base() -> str:
    return config.CODES_API_BASE_URL.rstrip("/")


def _limpiar_serial(serial: str) -> str:
    # El código se calcula sobre el texto del serial; conservamos ceros a la
    # izquierda y solo quitamos espacios. NO convertir a número.
    return str(serial).strip()


def codigo_activacion(serial: str, fecha: str | None = None) -> dict[str, Any]:
    """Código de activación del día para un equipo (por serial de MANÍPULO).

    - serial: nº de serie del manípulo (handpiece). Obligatorio.
    - fecha: 'YYYY-MM-DD'. Opcional; por defecto hoy.
    Devuelve {serial, fecha, code} o {error, ...}.
    """
    serial = _limpiar_serial(serial)
    if not serial:
        return {"error": "Falta el número de serie del manípulo."}

    params = {}
    if fecha:
        params["date"] = fecha

    resp = requests.get(
        f"{_base()}/codes/{serial}",
        params=params,
        headers={"Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code == 404:
        return {
            "serial": serial,
            "error": "No se pudo generar un código para ese serial (404 not_found). "
            "Revisa que sea el serial del MANÍPULO, no el de la consola.",
        }
    if resp.status_code == 400:
        return {"serial": serial, "error": _msg_error(resp, "Parámetro inválido (400 bad_request).")}
    if resp.status_code == 502:
        return {"serial": serial, "error": "Error temporal de la API de códigos (502). Reintenta."}
    resp.raise_for_status()

    data = resp.json()
    return {
        "serial": str(data.get("serial", serial)),
        "fecha": data.get("date"),
        "code": str(data.get("code")) if data.get("code") is not None else None,
    }


def codigos_activacion(seriales: list[str] | str, fecha: str | None = None) -> dict[str, Any]:
    """Códigos de activación del día para VARIOS equipos (bulk, hasta 200).

    - seriales: lista de seriales de manípulo, o una cadena separada por comas.
    - fecha: 'YYYY-MM-DD'. Opcional; por defecto hoy.
    Devuelve {fecha, total, ok, fallidos, resultados:[{serial, code} | {serial, error}]}.
    """
    if isinstance(seriales, str):
        lista = [s for s in (x.strip() for x in seriales.split(",")) if s]
    else:
        lista = [_limpiar_serial(s) for s in seriales if _limpiar_serial(s)]

    if not lista:
        return {"error": "No se han indicado seriales."}
    if len(lista) > 200:
        return {"error": f"Máximo 200 seriales por llamada (has pasado {len(lista)})."}

    params = {"serials": ",".join(lista)}
    if fecha:
        params["date"] = fecha

    resp = requests.get(
        f"{_base()}/codes",
        params=params,
        headers={"Accept": "application/json"},
        timeout=45,
    )
    if resp.status_code == 400:
        return {"error": _msg_error(resp, "Parámetro inválido (400 bad_request).")}
    if resp.status_code == 502:
        return {"error": "Error temporal de la API de códigos (502). Reintenta."}
    resp.raise_for_status()

    data = resp.json()
    resultados = []
    for r in data.get("results", []) or []:
        item = {"serial": str(r.get("serial"))}
        if r.get("code") is not None:
            item["code"] = str(r.get("code"))
        if r.get("error") is not None:
            item["error"] = r.get("error")
        resultados.append(item)

    return {
        "fecha": data.get("date"),
        "total": data.get("count"),
        "ok": data.get("ok"),
        "fallidos": data.get("failed"),
        "resultados": resultados,
    }


def _msg_error(resp: requests.Response, defecto: str) -> str:
    try:
        body = resp.json()
        return str(body.get("error") or body.get("message") or defecto)
    except Exception:  # noqa: BLE001
        return defecto
