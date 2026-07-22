"""
MCP de Leaseir — servidor.

Expone, como herramientas MCP, el acceso a:
  - Airtable (base Leaseir): parque de equipos (Inmovilizado) y pedidos.
  - Jira: incidencias del servicio técnico (SAT).
  - MongoDB: telemetría / estado en tiempo real de los equipos.

Se conecta desde Claude, ChatGPT (Developer Mode) o Gemini como conector
remoto (Streamable HTTP). Ver README.md.
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

import config
from sources import airtable_client, cloudmed, jira_client, mantenimiento

mcp = FastMCP(
    name="Leaseir",
    instructions=(
        "Acceso a los datos operativos de Leaseir: parque de equipos y pedidos "
        "(Airtable), incidencias de servicio técnico / SAT (Jira) y telemetría en "
        "tiempo real de los equipos (MongoDB). Úsalo para consultar el estado de "
        "equipos y pedidos de un cliente, seguir incidencias de SAT, y crear "
        "pedidos en borrador o incidencias de SAT."
    ),
)


def _safe(fn, *args, **kwargs):
    """Ejecuta un helper y devuelve un error legible en vez de reventar la tool."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - queremos el mensaje para el modelo
        return {"error": f"{type(exc).__name__}: {exc}"}


# ===========================================================================
# EQUIPOS DE UN CLIENTE (derivados de la tabla Pedidos)
#
# No hay una tabla global de todos los equipos fabricados y su dueño. Los
# equipos de un cliente se obtienen de sus pedidos (seriales entregados en
# ID_Console / ID_Handpiece). Para el estado en vivo, usa las herramientas de
# telemetría (Mongo) con el serial.
# ===========================================================================

@mcp.tool
def equipos_de_cliente(cliente: str, limite: int = 50) -> list[dict[str, Any]] | dict[str, Any]:
    """Lista los equipos que tiene un cliente, derivados de sus pedidos.

    PARCIAL: solo cubre los pedidos registrados en Airtable (histórico
    limitado); NO es el parque histórico completo del cliente. Adviértelo al
    usuario si la respuesta parece incompleta.

    Recorre los pedidos del cliente en Airtable y devuelve, por cada uno, los
    seriales entregados (ID_Console / ID_Handpiece), la configuración (consola,
    spot, longitud de onda), el estado del pedido, la fecha de entrega y la
    garantía. Los seriales que devuelve son los que luego puedes pasar a
    `estado_equipo_tiempo_real` o `telemetria_equipo` para ver el estado real.
    No existe un inventario global de equipos por dueño. Si el usuario te da un
    listado de seriales, úsalos directamente contra la telemetría sin pasar por
    aquí.
    """
    return _safe(airtable_client.equipos_de_cliente, customer=cliente, limit=limite)


@mcp.tool
def equipos_de_cliente_parque(cliente: str) -> list[dict[str, Any]] | dict[str, Any]:
    """Lista las consolas (equipos) de un cliente según el PARQUE real de Cloudmed.

    Usa el mapa maestro serial→cliente del parque (ownerMap), que cubre todo el
    parque conectado a Cloudmed de cualquier año — más completo que derivarlo de
    los pedidos de Airtable. Devuelve, por cada equipo: serial, cadena, centro,
    país y tipo de handpiece. Estos seriales son los que pasas a `uso_equipo`,
    `estado_equipo` o `actividad_dia` para ver la telemetría real. Es la vía
    preferente para "¿qué equipos tiene el cliente X?".
    """
    return _safe(cloudmed.equipos_de_cliente, cliente)


# ===========================================================================
# PEDIDOS (Airtable · tabla Pedidos)
# ===========================================================================

@mcp.tool
def buscar_pedidos(
    cliente: str | None = None,
    estado: str | None = None,
    pais: str | None = None,
    limite: int = 20,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Busca pedidos de Leaseir (tabla Pedidos de Airtable).

    Filtra por cliente (parcial), estado (Status) y/o país. Devuelve cliente,
    centro, nº de equipos, consola, tipo de petición, estado, prioridad, fecha
    esperada, precio total, comercial y URL de seguimiento. Útil para
    responder '¿qué pedidos tiene el cliente X?' o '¿qué hay pendiente de
    enviar en España?'.
    """
    return _safe(
        airtable_client.buscar_pedidos,
        customer=cliente,
        estado=estado,
        country=pais,
        limit=limite,
    )


@mcp.tool
def crear_pedido_borrador(
    cliente: str,
    tipo_de_peticion: str,
    centro: str | None = None,
    numero_de_equipos: int | None = None,
    consola: str | None = None,
    numero_de_botes_spray: int | None = None,
    extras: list[str] | None = None,
    tipo_de_venta: str | None = None,
    pais: str | None = None,
    contacto: str | None = None,
    email: str | None = None,
    comentarios: str | None = None,
) -> dict[str, Any]:
    """Crea un PEDIDO EN BORRADOR en la tabla Pedidos de Airtable.

    Sirve para pedidos de EQUIPO, de SPRAY/consumibles y de otros extras. Es una
    acción de ESCRITURA: crea un registro marcado como borrador (creado vía MCP,
    estado 'Pendiente de Aprobación') para que un humano lo revise y confirme.
    No cierra ninguna venta en firme. Confirma SIEMPRE los datos con el usuario
    antes de llamar a esta herramienta.

    Parámetros clave y valores válidos (usa exactamente estos textos):
    - tipo_de_peticion (obligatorio): uno de 'Sale of new device', 'Spray',
      'Backups for SAT or customer', 'Transportation to event / demo to
      customer', 'Competitors device (e.g. Cocoon or Opphalo)',
      'Recogida de equipo'.
    - consola: para equipos, uno de 'AHR', 'DS', 'MHR Aesth',
      'MHR Laser System', 'MHR Xcell', 'MHR Xcell Cosmetic', 'SRF'. Para un
      pedido de spray, usa 'Spray'.
    - numero_de_equipos: nº de equipos (para pedidos de equipo).
    - numero_de_botes_spray: nº de botes (para pedidos de spray; 1 caja = 6 botes).
    - extras: lista con cualquiera de 'Rollup (poster)',
      '1 Box with 6 bottles of spray', 'TP Link', 'Access to Cloudmed',
      'Others (explain in comments to Donet)'.
    - tipo_de_venta: 'Sale', 'Traditional Renting', 'Others' o 'N/A'.
    """
    return _safe(
        airtable_client.crear_pedido_borrador,
        customer=cliente,
        type_of_request=tipo_de_peticion,
        centro=centro,
        number_of_devices=numero_de_equipos,
        console=consola,
        number_of_bottles=numero_de_botes_spray,
        extras=extras,
        type_of_sale=tipo_de_venta,
        country=pais,
        contact=contacto,
        email=email,
        comments=comentarios,
    )


# ===========================================================================
# SAT / INCIDENCIAS (Jira)
# ===========================================================================

@mcp.tool
def buscar_incidencias_sat(
    cliente: str | None = None,
    estado: str | None = None,
    jql_extra: str | None = None,
    limite: int = 20,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Busca incidencias de servicio técnico (SAT) en Jira.

    Filtra por cliente (busca en el texto de la incidencia), estado (Status de
    Jira, p.ej. 'To Do', 'In Progress', 'Done') y, opcionalmente, un fragmento
    de JQL adicional para filtros avanzados. Devuelve clave, resumen, estado,
    prioridad, asignado, fechas y enlace directo a Jira.
    """
    return _safe(
        jira_client.buscar_incidencias,
        cliente=cliente,
        estado=estado,
        jql_extra=jql_extra,
        limit=limite,
    )


@mcp.tool
def detalle_incidencia_sat(clave: str) -> dict[str, Any]:
    """Devuelve el detalle completo de una incidencia SAT de Jira por su clave.

    'clave' es el identificador tipo 'SAT-123'. Incluye resumen, estado,
    prioridad, asignado, descripción y enlace.
    """
    return _safe(jira_client.detalle_incidencia, clave)


@mcp.tool
def crear_incidencia_sat(
    resumen: str,
    descripcion: str,
    cliente: str | None = None,
    prioridad: str | None = None,
) -> dict[str, Any]:
    """Crea una nueva incidencia de SAT en Jira. Acción de ESCRITURA.

    Crea un ticket en el proyecto de SAT con el resumen y la descripción
    indicados. Añade el cliente al principio de la descripción si se pasa.
    Confirma los datos con el usuario antes de crear la incidencia.
    """
    return _safe(
        jira_client.crear_incidencia,
        resumen=resumen,
        descripcion=descripcion,
        cliente=cliente,
        prioridad=prioridad,
    )


# ===========================================================================
# TELEMETRÍA CLOUDMED (MongoDB `cloudmed` · misma lógica que el portal)
#
# Los disparos se calculan igual que Cloudmed: high-water por manípulo sobre la
# colección de tramos `pulses_handpiece_2026`, con anti-salto de contador y
# disparos estimados; el día en curso se reconstruye en vivo desde `messages`.
# ===========================================================================

@mcp.tool
def uso_equipo(serial: str, desde: str | None = None, hasta: str | None = None) -> dict[str, Any]:
    """Uso de una CONSOLA (equipo) en un rango, con los mismos números que Cloudmed.

    'serial' es el número de serie de la consola. 'desde'/'hasta' son fechas
    'YYYY-MM-DD' (por defecto, el mes en curso). Devuelve disparos totales,
    desglose por día/modo/frecuencia/fluencia, días y horas activas, primer y
    último disparo, el contador de vida (lo que marca la pantalla) y el dueño.
    """
    return _safe(cloudmed.aggregate_consola, serial, desde=desde, hasta=hasta)


@mcp.tool
def uso_manipulo(manipulo: str, desde: str | None = None, hasta: str | None = None) -> dict[str, Any]:
    """Uso de un MANÍPULO (handpiece) en un rango, cruzando consolas. Igual que Cloudmed.

    'manipulo' es el id del manípulo (sin la 'H'; p.ej. '03346'). 'desde'/'hasta'
    son 'YYYY-MM-DD' (por defecto el mes en curso). Mismo desglose que
    `uso_equipo` pero agregando la actividad del manípulo aunque haya pasado por
    varias consolas.
    """
    return _safe(cloudmed.aggregate_manipulo, manipulo, desde=desde, hasta=hasta)


@mcp.tool
def actividad_dia(identificador: str, fecha: str) -> dict[str, Any]:
    """Actividad de un equipo o manípulo en un DÍA concreto (disparos por hora).

    'identificador' es el serial de la consola, o el manípulo con 'H' delante
    (p.ej. 'H03346'). 'fecha' es 'YYYY-MM-DD'. Devuelve los disparos de ese día,
    el desglose por hora (0-23, hora local), horas activas, modo/frecuencia/
    fluencia y la última actividad. Úsalo para '¿qué actividad tuvo H03346 ayer?'.
    """
    return _safe(cloudmed.actividad_dia, identificador, fecha)


@mcp.tool
def estado_equipo(serial: str) -> dict[str, Any]:
    """Estado de hoy de una consola: disparos del día y última actividad.

    Reconstruye el día en curso en vivo desde `messages` (igual que Cloudmed) y
    devuelve los disparos de hoy, el desglose por hora y el último disparo
    registrado. Úsalo para '¿cómo va hoy el equipo con serial X?'.
    """
    return _safe(cloudmed.actividad_dia, serial, cloudmed.today_str())


# ===========================================================================
# MANTENIMIENTO PREVENTIVO (Cloudmed) · térmico + diodos + estado online
# ===========================================================================

@mcp.tool
def estado_online(serial: str) -> dict[str, Any]:
    """Estado de conexión de una consola: disparando / en línea (standby) / offline.

    'serial' es el número de serie de la consola. Deriva el estado de la última
    conexión (último mensaje recibido) y los disparos de hoy: disparando (activo
    ahora), en línea/standby (conectado sin disparar), u offline (corto <7d /
    largo >7d). Devuelve también último mensaje y última actividad.
    """
    return _safe(mantenimiento.estado_online, serial)


@mcp.tool
def mantenimiento_equipo(serial: str) -> dict[str, Any]:
    """Foto de mantenimiento preventivo de una consola: térmico + condensador + diodos.

    Devuelve las últimas temperaturas (tip, diodos, nevera) con su estado
    (alerta/vigilancia/sano), el voltaje del condensador y sus flags, y la salud
    de cada diodo vivo de la consola (basada en las calibraciones COMP). Úsalo
    para '¿cómo está de salud el equipo con serial X?'.
    """
    return _safe(mantenimiento.mantenimiento_equipo, serial)


@mcp.tool
def serie_termica(serial: str, dias: int = 30) -> list[dict[str, Any]] | dict[str, Any]:
    """Serie diaria de temperaturas y voltaje de condensador de una consola.

    'serial' es la consola; 'dias' cuántos días atrás (por defecto 30). Devuelve,
    por día, la media de temperatura de tip/diodos/nevera, el voltaje del
    condensador y si hubo error/sobrecorriente/aviso. Útil para ver tendencias
    de calentamiento o degradación del condensador.
    """
    return _safe(mantenimiento.thermal_series, serial, dias=dias)


@mcp.tool
def salud_diodo(diodo: str) -> dict[str, Any]:
    """Salud de un DIODO concreto por su id (predicción de rotura vía calibraciones COMP).

    Analiza los eventos de calibración COMP (pulso medido vs banda de referencia
    780/1300): media de 7 días < 780 o ≥50% de lecturas 'under' = alerta. Estos
    síntomas anticipan la rotura del diodo con semanas/meses de antelación.
    Nota: familias SP*/093*/8 dígitos usan otra escala y salen como 'sin_baremo'.
    """
    return _safe(mantenimiento.salud_diodo, diodo)


# ===========================================================================
# App HTTP + autenticación por token (v1 interna)
# ===========================================================================

@mcp.custom_route("/health", methods=["GET"])
async def health(_request):  # noqa: ANN001
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "service": "leaseir-mcp"})


def build_app():
    """Construye la app ASGI con el gate de token (si MCP_AUTH_TOKEN está puesto)."""
    app = mcp.http_app()

    if config.MCP_AUTH_TOKEN:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class BearerTokenMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):  # noqa: ANN001
                if request.url.path.rstrip("/") == "/health":
                    return await call_next(request)
                # Se acepta el token de dos formas:
                #  - Cabecera:  Authorization: Bearer <token>
                #  - Query param: ...?token=<token>   (para clientes como el
                #    conector de Claude/ChatGPT que no dejan poner cabeceras)
                header_ok = request.headers.get("authorization", "") == f"Bearer {config.MCP_AUTH_TOKEN}"
                query_ok = request.query_params.get("token") == config.MCP_AUTH_TOKEN
                if not (header_ok or query_ok):
                    return JSONResponse({"error": "no autorizado"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerTokenMiddleware)

    return app


# Objeto ASGI para servidores tipo `uvicorn server:app`
app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
