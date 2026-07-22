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
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.settings import ClientRegistrationOptions

import config
from sources import airtable_client, cloudmed, codes_client, jira_client, mantenimiento

# Autenticación OAuth 2.1 para que el conector de Claude/ChatGPT/Gemini pueda
# conectarse (esos clientes exigen OAuth con registro dinámico de cliente, no un
# token estático). Este proveedor implementa el flujo completo (metadata,
# /register, /authorize, /token) sobre la propia app.
#   NOTA v1: es un proveedor de "grado test" (auto-aprueba el consentimiento);
#   el acceso queda protegido de facto por lo privado de la URL. Antes de
#   exponerlo a clientes conviene un login real / IdP o restringir por red.
_auth = InMemoryOAuthProvider(
    base_url=config.PUBLIC_BASE_URL,
    client_registration_options=ClientRegistrationOptions(enabled=True),
)

mcp = FastMCP(
    name="Leaseir",
    auth=_auth,
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
    solo_reales: bool = True,
    solo_entregados: bool = False,
    incluir_excluidos: bool = False,
    limite: int = 50,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Busca pedidos de Leaseir (tabla Pedidos de Airtable) con el MISMO racional que el portal de Elha.

    Por defecto solo devuelve PEDIDOS REALES de equipo: filtra por Type of request
    (Sale of new device / Competitors device) y descarta los marcados 'Exclude'
    (manípulos sueltos), los demos y los sprays — igual que el portal. Cada pedido
    llega con su 'Fase' (Recién recibido → Pendiente fabricación → En proceso →
    Fabricado pendiente de entrega → En tránsito → Entregado) y se incluye un
    'resumen' (total, entregados, pendientes, desglose por fase).

    - solo_entregados=True: cuenta solo los ENTREGADOS (Entregado a Cliente /
      Enviado a Cliente en vuelo) → el nº real de equipos entregados/parque. P.ej.
      "¿cuántos equipos ha recibido Elha en 2026?".
    - solo_reales=False: trae también manípulos sueltos, demos y sprays (datos en crudo).
    - incluir_excluidos=True: no descarta los marcados 'Exclude'.
    - 'estado' filtra por un Status concreto; 'pais' por país.
    Útil para "¿qué pedidos tiene el cliente X?", "¿qué hay pendiente de entregar?"
    o "¿qué se ha entregado a Elha este año?".
    """
    return _safe(
        airtable_client.buscar_pedidos,
        customer=cliente,
        estado=estado,
        country=pais,
        solo_reales=solo_reales,
        solo_entregados=solo_entregados,
        incluir_excluidos=incluir_excluidos,
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
    solo_abiertas: bool = False,
    tipo: str | None = "Task",
    limite: int = 20,
) -> list[dict[str, Any]] | dict[str, Any]:
    """Busca incidencias de servicio técnico (SAT) en Jira, con el MISMO racional que el portal de Elha.

    No devuelve datos en crudo: cada incidencia viene con 'abierta' (true/false
    según los 30 estados abiertos de LEAS), 'funnel' (A=Gestión en taller,
    B=Gestión externa, C=Gestión online), 'fase' (paso del funnel), y los campos
    de negocio (cliente/centro, consola, manípulo, modelo, bloqueante,
    sustitución, forma de resolución). Incluye además un 'resumen' con el conteo
    de abiertas/cerradas y abiertas por funnel.

    - Igual que el portal, por defecto SOLO cuenta las incidencias de servicio
      técnico (tipo Task): NO incluye "Máquina de sustitución" ni "Revisión queja
      Calidad". Para verlas todas, pasa tipo=None.
    - Para "qué incidencias están ABIERTAS" usa solo_abiertas=True: filtra los
      estados exactamente como la pantalla de Incidencias Abiertas del portal.
    - 'cliente' se busca en los CAMPOS Cliente/centro y en los seriales de consola
      y manípulo (no en texto libre), así que sirve tanto para "Elha" como para un
      serial ("40679", "C00519") y no arrastra tickets de otros clientes.
    - 'estado' fuerza un Status concreto; 'jql_extra' permite JQL avanzado.
    """
    return _safe(
        jira_client.buscar_incidencias,
        cliente=cliente,
        estado=estado,
        jql_extra=jql_extra,
        solo_abiertas=solo_abiertas,
        tipo=tipo,
        limit=limite,
    )


@mcp.tool
def detalle_incidencia_sat(clave: str) -> dict[str, Any]:
    """Devuelve el detalle completo de una incidencia SAT de Jira por su clave.

    'clave' es el identificador tipo 'LEAS-123'. Incluye la clasificación del
    portal (abierta, funnel, fase), los campos de negocio (cliente, consola,
    manípulo, modelo, bloqueante, sustitución, tipo y descripción de avería) y
    la descripción/enlace del ticket.
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
# CÓDIGOS DE ACTIVACIÓN (API propia de Leaseir)
# ===========================================================================

@mcp.tool
def codigo_activacion(serial: str, fecha: str | None = None) -> dict[str, Any]:
    """Devuelve el código de activación DIARIO de un equipo.

    El código son 8 dígitos (texto, puede empezar por 0) y se calcula a partir
    del número de serie del MANÍPULO (handpiece), NO el de la consola. Cambia
    cada día y solo vale para su fecha.

    - 'serial': nº de serie del manípulo.
    - 'fecha': 'YYYY-MM-DD' opcional; por defecto hoy. Pide el código el mismo
      día que se va a usar.
    """
    return _safe(codes_client.codigo_activacion, serial, fecha=fecha)


@mcp.tool
def codigos_activacion(seriales: list[str], fecha: str | None = None) -> dict[str, Any]:
    """Códigos de activación del día para VARIOS equipos a la vez (hasta 200).

    Útil para un cliente con varios manípulos. 'seriales' es la lista de nº de
    serie de manípulo. 'fecha' 'YYYY-MM-DD' opcional; por defecto hoy. Devuelve
    un resumen (total/ok/fallidos) y el código de cada serial.
    """
    return _safe(codes_client.codigos_activacion, seriales, fecha=fecha)


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
    """Construye la app ASGI. La autenticación la gestiona el proveedor OAuth
    del FastMCP: http_app() expone la metadata .well-known y los endpoints
    /register, /authorize y /token que usa el conector de Claude."""
    return mcp.http_app()


# Objeto ASGI para servidores tipo `uvicorn server:app`
app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
