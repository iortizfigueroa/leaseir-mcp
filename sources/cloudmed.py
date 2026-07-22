"""
Agregación de telemetría Cloudmed — port fiel de lib/cloudmed/aggregateCollection.ts
del portal elha-portal, para que el MCP dé EXACTAMENTE los mismos números que Cloudmed.

Fuente de verdad:
  - Histórico: colección derivada `pulses_handpiece_2026` (tramos ya parseados,
    con la regla Quad ya aplicada a `fluenciaReal`; SP se aplica al leer).
  - Día en curso (y "ayer" si el cron nocturno aún no lo ha volcado): se
    reconstruye EN VIVO desde `messages`, igual que el portal.

Lógica clave replicada:
  - HIGH-WATER por manípulo: el contador real solo sube; solo se cuenta lo que
    supera el máximo ya alcanzado. Los "replays" (por debajo del máximo) cuentan 0.
  - Anti-salto de contador: tope físico de un MHR ~12 Hz; un avance > 10.000 que
    además supera ese tope físico es un salto de contador → se descarta.
  - Disparos ESTIMADOS: tras una alucinación del contador, la recuperación se
    reparte sobre las horas de replay (donde el equipo estuvo online), ponderando
    por su actividad.
  - Reglas: modo SP → frecuencia fija 10 Hz y fluencia /2; Quad → fluencia /2 (ya
    aplicada en la colección; en el vivo se aplica al construir el tramo).

Un tramo (documento de pulses_handpiece_2026) trae:
  consola, manipulo, diodo, tipo, modo, fluenciaReal, frecuencia, shots,
  fpcStart, fpcEnd, tsStart, tsEnd, fecha (YYYY-MM-DD).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any
from zoneinfo import ZoneInfo

import config

_client = None
_TZ = ZoneInfo(config.CLOUDMED_TZ)

MODE_MAP = {0: "AUTO", 1: "SP", 2: "30ms", 3: "100ms", 4: "400ms"}
PARAM_CODES = ["MODE", "FREQ", "FLUE"]
COUNTER_CODES = ["RDPC", "RSPC", "WRPC_RDPC"]
NEEDED_CODES = PARAM_CODES + COUNTER_CODES + ["RDFR"]
MAX_DELTA = 10_000
MAX_HZ = 12


# ---------------------------------------------------------------------------
# Conexión (perezosa, cacheada, read-only)
# ---------------------------------------------------------------------------

def _db():
    global _client
    if not config.CLOUDMED_MONGO_URI:
        raise RuntimeError(
            "CLOUDMED_MONGO_URI no definida. Añádela al .env / Render (Mongo Atlas read-only)."
        )
    if _client is None:
        from pymongo import MongoClient

        _client = MongoClient(
            config.CLOUDMED_MONGO_URI,
            serverSelectionTimeoutMS=12000,
            tz_aware=True,  # datetimes salen como UTC aware
            compressors="zlib",
        )
    return _client[config.CLOUDMED_DB]


def configured() -> bool:
    return bool(config.CLOUDMED_MONGO_URI)


def today_str() -> str:
    """Fecha de hoy (YYYY-MM-DD) en la zona horaria de Cloudmed."""
    return _tz_parts(_dt.datetime.now(_dt.timezone.utc))[0]


# ---------------------------------------------------------------------------
# Helpers de tiempo / formato de claves (imitando String(number) de JS)
# ---------------------------------------------------------------------------

def _as_utc(dt: Any) -> _dt.datetime:
    if isinstance(dt, str):
        dt = _dt.datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def _tz_parts(dt: Any) -> tuple[str, int]:
    """Devuelve (YYYY-MM-DD, hora 0-23) de un instante en la zona local."""
    loc = _as_utc(dt).astimezone(_TZ)
    return loc.strftime("%Y-%m-%d"), loc.hour


def _ms(dt: Any) -> float:
    return _as_utc(dt).timestamp() * 1000.0


def _num_key(v: float) -> str:
    """Imita String(n) de JS: enteros sin decimales, si no float normal."""
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _inc(obj: dict, key: str, n: float) -> None:
    obj[key] = obj.get(key, 0) + n


def phys_max(a_ms: float, b_ms: float) -> float:
    dur_s = max(0.0, (b_ms - a_ms) / 1000.0)
    return (dur_s + 60) * MAX_HZ


def is_counter_jump(new_ground: float, a_ms: float, b_ms: float) -> bool:
    return new_ground > 10000 and new_ground > phys_max(a_ms, b_ms)


def _num(x: Any):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Estructura de agregado por equipo/manípulo
# ---------------------------------------------------------------------------

def _empty(sid: str) -> dict[str, Any]:
    return {
        "sid": sid,
        "totalShots": 0,
        "lifetimeShots": None,
        "estShots": 0,
        "unreliable": False,
        "activeDays": 0,
        "activeHours": 0,
        "firstActivity": None,
        "lastActivity": None,
        "manip": None,
        "diode": None,
        "model": None,
        "byDay": {},
        "dayHour": {},
        "dayHourEst": {},
        "byMode": {},
        "byFreq": {},
        "byFluence": {},
        "byFluenceMode": {},
    }


def _add_measured(d: dict, t: dict, shots: float, hourset: set) -> None:
    if shots <= 0:
        return
    dk, hour = _tz_parts(t["tsStart"])
    d["totalShots"] += shots
    _inc(d["byDay"], dk, shots)
    d["dayHour"].setdefault(dk, [0] * 24)
    d["dayHour"][dk][hour] += shots
    hourset.add(f"{dk}T{hour}")
    modo = t.get("modo") or "(sin modo)"
    _inc(d["byMode"], modo, shots)
    is_sp = modo == "SP"
    freq_v = _num(t.get("frecuencia"))
    flue_v = _num(t.get("fluenciaReal"))
    if is_sp:
        if flue_v is not None:
            flue_v = flue_v / 2
        freq_v = 10
    if freq_v is not None:
        _inc(d["byFreq"], _num_key(freq_v), shots)
    if flue_v is not None:
        fk = _num_key(round(flue_v * 10) / 10)
        _inc(d["byFluence"], fk, shots)
        _inc(d["byFluenceMode"], f"{fk}|{modo}", shots)


def _est_pieces(est_shots: float, replays: list) -> list[dict]:
    if not replays:
        return []
    ws = [max(1.0, _num(r.get("shots")) or 0) for r in replays]
    tot = sum(ws) or 1
    out = []
    assigned = 0.0
    for i, r in enumerate(replays):
        part = est_shots - assigned if i == len(replays) - 1 else round(est_shots * ws[i] / tot)
        assigned += part
        dk, hour = _tz_parts(r["tsStart"])
        if part > 0:
            out.append({"dk": dk, "hour": hour, "shots": part})
    return out


def _add_estimated(d: dict, est_shots: float, replays: list, hourset: set) -> None:
    if est_shots <= 0 or not replays:
        return
    d["estShots"] += est_shots
    d["unreliable"] = True
    for p in _est_pieces(est_shots, replays):
        _inc(d["byDay"], p["dk"], p["shots"])
        d["dayHour"].setdefault(p["dk"], [0] * 24)
        d["dayHour"][p["dk"]][p["hour"]] += p["shots"]
        d["dayHourEst"].setdefault(p["dk"], [0] * 24)
        d["dayHourEst"][p["dk"]][p["hour"]] += p["shots"]
        hourset.add(f"{p['dk']}T{p['hour']}")


def aggregate_device_tramos(d: dict, tramos: list, hourset: set) -> None:
    """HIGH-WATER por manípulo sobre TODOS los tramos de un equipo. Port fiel."""
    by_manip: dict[str, list] = {}
    for t in tramos:
        by_manip.setdefault(str(t.get("manipulo") or ""), []).append(t)

    latest_end = -1.0
    for mk in by_manip:
        arr = sorted(by_manip[mk], key=lambda t: _ms(t["tsStart"]))
        hw = None
        dip_replays: list = []
        for t in arr:
            fs = _num(t.get("fpcStart"))
            fe = _num(t.get("fpcEnd"))
            if fs is None or fe is None:
                continue
            if hw is None:
                hw = fs
            new_ground = fe - max(hw, fs)
            ts_start_ms = _ms(t["tsStart"])
            ts_end_ms = _ms(t.get("tsEnd") or t["tsStart"])
            if new_ground <= 0:
                dip_replays.append(t)
            else:
                stored = _num(t.get("shots"))
                cap = min(new_ground, stored) if (stored is not None and stored > 0) else new_ground
                if dip_replays:
                    replay_shots = sum((_num(p.get("shots")) or 0) for p in dip_replays)
                    est = min(new_ground, replay_shots + cap)
                    win_start = _ms(dip_replays[0]["tsStart"])
                    if est <= 0 or is_counter_jump(est, win_start, ts_end_ms):
                        dip_replays = []
                    else:
                        _add_estimated(d, est, dip_replays, hourset)
                        dip_replays = []
                elif is_counter_jump(cap, ts_start_ms, ts_end_ms):
                    pass  # salto de contador → descartar
                else:
                    _add_measured(d, t, cap, hourset)
                hw = max(hw, fe)
            # primera/última actividad + manip/diodo/modelo del tramo más reciente
            if d["firstActivity"] is None or ts_start_ms < _ms(d["firstActivity"]):
                d["firstActivity"] = _as_utc(t["tsStart"]).isoformat()
            if d["lastActivity"] is None or ts_end_ms > _ms(d["lastActivity"]):
                d["lastActivity"] = _as_utc(t.get("tsEnd") or t["tsStart"]).isoformat()
            if ts_end_ms >= latest_end:
                latest_end = ts_end_ms
                if t.get("manipulo") is not None:
                    d["manip"] = str(t["manipulo"])
                if t.get("diodo") is not None:
                    d["diode"] = str(t["diodo"])
                if t.get("tipo"):
                    d["model"] = str(t["tipo"])


def _finalize(d: dict, hourset: set, tramos: list) -> dict:
    d["activeDays"] = len(d["byDay"])
    d["activeHours"] = len(hourset)
    # lifetimeShots = contador de pantalla = fpcEnd del tramo más reciente
    if tramos:
        latest = max(tramos, key=lambda t: _ms(t.get("tsEnd") or t["tsStart"]))
        fe = _num(latest.get("fpcEnd"))
        d["lifetimeShots"] = fe if fe is not None else d["lifetimeShots"]
    return d


# ---------------------------------------------------------------------------
# Live desde `messages` (día en curso / hueco antes del cron nocturno)
# ---------------------------------------------------------------------------

def _live_from_day(db) -> str:
    """Primer día que el vivo debe cubrir = siguiente al último día en la colección,
    acotado a [hoy-4, hoy]."""
    today = _tz_parts(_dt.datetime.now(_dt.timezone.utc))[0]
    last_coll = None
    try:
        docs = list(
            db[config.CLOUDMED_COLLECTION]
            .find({}, {"fecha": 1})
            .sort("fecha", -1)
            .limit(1)
        )
        if docs and docs[0].get("fecha"):
            last_coll = str(docs[0]["fecha"])
    except Exception:
        last_coll = None

    def add_days(day: str, n: int) -> str:
        base = _dt.datetime.fromisoformat(day + "T00:00:00+00:00")
        return (base + _dt.timedelta(days=n)).strftime("%Y-%m-%d")

    frm = add_days(last_coll, 1) if last_coll else today
    min_from = add_days(today, -4)
    if frm < min_from:
        frm = min_from
    if frm > today:
        frm = today
    return frm


def build_today_tramos(db, sid: str, from_day: str) -> dict[str, Any]:
    """Reconstruye los tramos del día en curso desde `messages` para una consola.
    Port de buildTodayTramos: corrección de reloj por offset de llegada, last-known
    de parámetros y troceo por (manípulo/modo/fluencia/frecuencia)."""
    since = _dt.datetime.fromisoformat(from_day + "T00:00:00+00:00") - _dt.timedelta(hours=4)
    cond = {
        "$or": [
            {"$in": ["$$s.cd", NEEDED_CODES]},
            {"$regexMatch": {"input": {"$ifNull": ["$$s.rw", ""]}, "regex": "RdHp_"}},
        ]
    }
    docs = list(
        db[config.CLOUDMED_MESSAGES_COLLECTION].aggregate(
            [
                {"$match": {"dv": sid, "tm": {"$gte": since}, "sm.cd": {"$in": NEEDED_CODES}}},
                {"$project": {"_id": 1, "sm": {"$filter": {"input": "$sm", "as": "s", "cond": cond}}}},
            ],
            maxTimeMS=30000,
            allowDiskUse=True,
        )
    )
    evs = []
    for m in docs:
        arr = m["_id"].generation_time  # tiempo de llegada al servidor (UTC aware)
        for s in (m.get("sm") or []):
            s["_arr"] = arr
            evs.append(s)
    evs.sort(key=lambda e: _ms(e["tm"]))

    offs = sorted(
        (_ms(e["_arr"]) - _ms(e["tm"])) / 60000.0
        for e in evs
        if e.get("tm") and e.get("_arr")
    )
    off = offs[len(offs) // 2] if offs else 0.0
    apply = abs(off) > 5

    def corr(t):
        return _as_utc(t) + _dt.timedelta(minutes=off) if apply else _as_utc(t)

    last_seen_ms = 0.0
    for e in evs:
        if e.get("_arr") and _ms(e["_arr"]) > last_seen_ms:
            last_seen_ms = _ms(e["_arr"])
    last_seen = _dt.datetime.fromtimestamp(last_seen_ms / 1000, _dt.timezone.utc).isoformat() if last_seen_ms else None

    manip = diodo = model = modo = None
    freq = flue_b = prev = last_fpc = None
    segs: list = []
    for e in evs:
        cd = e.get("cd")
        if cd == "RDFR":
            if e.get("lsSn") is not None:
                manip = str(e["lsSn"]).strip()
            if e.get("hpSn") is not None:
                diodo = str(e["hpSn"]).strip()
            if e.get("model"):
                model = str(e["model"]).lower()
            continue
        if not cd and isinstance(e.get("rw"), str) and "RdHp_" in e["rw"]:
            import re

            p = [x.strip() for x in re.sub(r"<.*$", "", e["rw"].split("RdHp_")[1]).split("_")]
            if len(p) > 0 and p[0]:
                diodo = p[0]
            if len(p) > 1 and p[1]:
                model = p[1].lower()
            if len(p) > 2 and p[2]:
                manip = p[2]
            continue
        if cd in PARAM_CODES:
            if e.get("modeNum") is not None:
                modo = MODE_MAP.get(int(e["modeNum"]), "m" + str(e["modeNum"]))
            if e.get("freq") is not None:
                freq = _num(e["freq"])
            if e.get("fluence") is not None:
                flue_b = _num(e["fluence"])
            continue
        if e.get("fpc") is not None:
            fpc = _num(e["fpc"])
            last_fpc = fpc
            if prev is not None:
                dd = fpc - prev
                if 0 < dd <= MAX_DELTA:
                    is_q = "quad" in (model or "")
                    flue_r = (flue_b / 2 if is_q else flue_b) if flue_b is not None else None
                    ts = corr(e["tm"])
                    last = segs[-1] if segs else None
                    if (
                        last
                        and last["manipulo"] == manip
                        and last["modo"] == modo
                        and last["fluenciaReal"] == flue_r
                        and last["frecuencia"] == freq
                    ):
                        last["shots"] += dd
                        last["fpcEnd"] = fpc
                        last["tsEnd"] = ts
                    else:
                        segs.append({
                            "consola": sid, "manipulo": manip, "diodo": diodo, "tipo": model,
                            "modo": modo, "fluenciaReal": flue_r, "frecuencia": freq,
                            "shots": dd, "fpcStart": prev, "fpcEnd": fpc, "tsStart": ts, "tsEnd": ts,
                        })
            prev = fpc

    tramos = [s for s in segs if _tz_parts(s["tsStart"])[0] >= from_day]
    return {"tramos": tramos, "lastFpc": last_fpc, "lastSeen": last_seen}


# ---------------------------------------------------------------------------
# API de alto nivel (lo que consumen las herramientas del MCP)
# ---------------------------------------------------------------------------

_PROJ = {
    "consola": 1, "manipulo": 1, "diodo": 1, "tipo": 1, "modo": 1,
    "fluenciaReal": 1, "frecuencia": 1, "shots": 1,
    "fpcStart": 1, "fpcEnd": 1, "tsStart": 1, "tsEnd": 1, "fecha": 1,
}


def _range(desde: str | None, hasta: str | None) -> tuple[_dt.datetime, _dt.datetime, str, str]:
    today = _tz_parts(_dt.datetime.now(_dt.timezone.utc))[0]
    if not hasta:
        hasta = today
    if not desde:
        # por defecto, primero del mes de `hasta`
        desde = hasta[:8] + "01"
    dfrom = _dt.datetime.fromisoformat(desde + "T00:00:00+00:00")
    dto = _dt.datetime.fromisoformat(hasta + "T00:00:00+00:00") + _dt.timedelta(days=1)
    return dfrom, dto, desde, hasta


def _collect_tramos(db, key_field: str, value: str, dfrom, dto, hasta_day: str) -> list:
    """Tramos de la colección + (si el rango llega a hoy) hueco en vivo desde messages."""
    tramos = list(db[config.CLOUDMED_COLLECTION].find(
        {key_field: value, "tsStart": {"$gte": dfrom, "$lt": dto}}, _PROJ,
    ))
    today = _tz_parts(_dt.datetime.now(_dt.timezone.utc))[0]
    if hasta_day >= today:
        live_from = _live_from_day(db)
        # excluir de la colección los días que cubre el vivo (evitar duplicar)
        tramos = [t for t in tramos if not t.get("fecha") or str(t["fecha"]) < live_from]
        # para manípulo hay que reconstruir el vivo por consola; se filtra luego
        consolas = _consolas_for(db, key_field, value, live_from)
        for cs in consolas:
            try:
                r = build_today_tramos(db, cs, live_from)
                for t in r["tramos"]:
                    if key_field == "consola" or str(t.get("manipulo") or "").strip() == value:
                        tramos.append(t)
            except Exception:
                pass
    return tramos


def _consolas_for(db, key_field: str, value: str, since_day: str) -> list[str]:
    if key_field == "consola":
        return [value]
    # manípulo: consolas donde ha estado recientemente
    desde = (_dt.date.fromisoformat(since_day) - _dt.timedelta(days=8)).isoformat()
    try:
        return [str(c) for c in db[config.CLOUDMED_COLLECTION].distinct(
            "consola", {"manipulo": value, "fecha": {"$gte": desde}}
        ) if c is not None][:6]
    except Exception:
        return []


def aggregate_consola(serial: str, desde: str | None = None, hasta: str | None = None) -> dict:
    db = _db()
    dfrom, dto, desde, hasta = _range(desde, hasta)
    tramos = _collect_tramos(db, "consola", serial, dfrom, dto, hasta)
    d = _empty(serial)
    hs: set = set()
    aggregate_device_tramos(d, tramos, hs)
    _finalize(d, hs, tramos)
    d["owner"] = owner_of(serial)
    d["rango"] = {"desde": desde, "hasta": hasta}
    return d


def aggregate_manipulo(manipulo: str, desde: str | None = None, hasta: str | None = None) -> dict:
    db = _db()
    dfrom, dto, desde, hasta = _range(desde, hasta)
    tramos = _collect_tramos(db, "manipulo", manipulo, dfrom, dto, hasta)
    d = _empty("H" + manipulo)
    hs: set = set()
    aggregate_device_tramos(d, tramos, hs)
    _finalize(d, hs, tramos)
    d["rango"] = {"desde": desde, "hasta": hasta}
    return d


def actividad_dia(identificador: str, fecha: str) -> dict:
    """Actividad (disparos por hora) de una consola o manípulo en un día concreto.
    Si el identificador empieza por 'H' se trata como manípulo."""
    es_manip = identificador.startswith("H") and not identificador[1:].isalpha()
    if es_manip:
        d = aggregate_manipulo(identificador[1:], desde=fecha, hasta=fecha)
    else:
        d = aggregate_consola(identificador, desde=fecha, hasta=fecha)
    horas = d["dayHour"].get(fecha, [0] * 24)
    return {
        "id": identificador,
        "fecha": fecha,
        "disparos": d["byDay"].get(fecha, 0),
        "porHora": horas,
        "horasActivas": sum(1 for h in horas if h > 0),
        "modo": d["byMode"],
        "frecuencia": d["byFreq"],
        "fluencia": d["byFluence"],
        "estimados": d.get("estShots", 0),
        "ultimaActividad": d.get("lastActivity"),
    }


# ---------------------------------------------------------------------------
# ownerMap: serial (consola) -> cliente/cadena/centro
# ---------------------------------------------------------------------------

_OWNERS: dict[str, Any] | None = None


def _owners() -> dict[str, Any]:
    global _OWNERS
    if _OWNERS is None:
        import json
        import os

        path = os.path.join(os.path.dirname(__file__), "ownerMap.json")
        try:
            with open(path, encoding="utf-8") as f:
                _OWNERS = json.load(f)
        except Exception:
            _OWNERS = {}
    return _OWNERS


def owner_of(serial: str) -> dict | None:
    return _owners().get(serial)


def equipos_de_cliente(cliente: str) -> list[dict]:
    """Consolas (equipos) de una cadena/cliente según el ownerMap del parque."""
    q = cliente.lower().strip()
    out = []
    for sid, o in _owners().items():
        chain = str(o.get("chain", ""))
        if q in chain.lower():
            out.append({
                "serial": sid,
                "cadena": chain,
                "centro": o.get("center"),
                "pais": o.get("country"),
                "tipo": o.get("tipo"),
            })
    return sorted(out, key=lambda x: x["serial"])
