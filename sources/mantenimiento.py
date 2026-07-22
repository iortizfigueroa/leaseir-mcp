"""
Mantenimiento preventivo Cloudmed — port de thermal.ts + maintenance.ts + estado online.

- Térmico (thermal.ts): temperaturas de tip/diodos/nevera y voltaje de condensador
  desde `messages` (submensajes HPTS/I2C2/STAT), con estados alerta/vigilancia/sano.
- Salud de diodos (maintenance.ts): eventos COMP de la colección `calibrations`
  (pulso medido vs banda 780/1300). Media 7d < 780 o ≥50% "under" = alerta.
  Baremo: familias SP*/093*/8 dígitos no comparan con 780. Happy Laser calibra
  en seco (pulse<100) → esas lecturas se descartan.
- Estado online (disparando / en línea / offline) derivado de la última conexión.

Todo lectura. Reutiliza la conexión read-only de sources/cloudmed.py.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

import config
from sources import cloudmed as C

MSG = config.CLOUDMED_MESSAGES_COLLECTION
TRAMOS = config.CLOUDMED_COLLECTION
CAL = "calibrations"

MIN_CPVOL = 38          # umbral voltaje condensador
DAYS_SERIE = 120        # ventana serie COMP
VIVO_DESDE = "2026-01-01"

# Umbrales de estado online (minutos / días). Aproximan la clasificación del portal.
FIRING_MIN = 15         # < 15 min desde el último mensaje y con disparos hoy → disparando
ONLINE_HOURS = 26       # < 26 h → en línea (standby)
OFFLINE_SHORT_DAYS = 7  # < 7 d offline = corto; ≥ 7 d = largo


def _num1(x: Any):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v * 10) / 10


def _pick_sm(sm: Any, cd: str):
    if not isinstance(sm, list):
        return None
    for s in sm:
        if s and s.get("cd") == cd:
            return s
    return None


def _future_cap() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=36)


# ===========================================================================
# TÉRMICO
# ===========================================================================

def thermal_last(consola: str) -> dict[str, Any]:
    db = C._db()
    m = db[MSG]
    cap = _future_cap()
    temp_doc = m.find_one(
        {"dv": consola, "tm": {"$lte": cap}, "sm.cd": {"$in": ["HPTS", "I2C2"]}},
        sort=[("tm", -1)], projection={"tm": 1, "sm": 1},
    )
    cond_doc = m.find_one(
        {"dv": consola, "tm": {"$lte": cap}, "sm.cd": "STAT"},
        sort=[("tm", -1)], projection={"tm": 1, "sm": 1},
    )
    r: dict[str, Any] = {
        "consola": consola, "tip": None, "dio1": None, "dio2": None, "cooler": None,
        "delta": None, "cpVol": None, "flags": None, "tempTs": None, "condTs": None,
        "hasHpts": False,
    }
    if temp_doc:
        hp = _pick_sm(temp_doc.get("sm"), "HPTS")
        i2 = _pick_sm(temp_doc.get("sm"), "I2C2")
        r["tip"] = _num1(hp["tempTip"] if hp else (i2 or {}).get("ta1"))
        r["dio1"] = _num1(hp["tempDio1"] if hp else (i2 or {}).get("to1"))
        r["dio2"] = _num1(hp["tempDio2"] if hp else (i2 or {}).get("to2"))
        r["cooler"] = _num1(hp["tempCooler"] if hp else None)
        r["hasHpts"] = bool(hp and hp.get("tempCooler") is not None)
        if r["dio1"] is not None and r["dio2"] is not None:
            r["delta"] = round(abs(r["dio1"] - r["dio2"]) * 10) / 10
        r["tempTs"] = C._as_utc(temp_doc["tm"]).isoformat() if temp_doc.get("tm") else None
    if cond_doc:
        st = _pick_sm(cond_doc.get("sm"), "STAT")
        if st:
            r["cpVol"] = _num1(st.get("cpVol"))
            r["flags"] = {
                "err": bool(st.get("lsErr")), "ovc": bool(st.get("lsOvc")),
                "vls": bool(st.get("lsVls")), "pok": bool(st.get("lsPok")),
                "wrn": bool(st.get("dvWrn")),
            }
            r["condTs"] = C._as_utc(cond_doc["tm"]).isoformat() if cond_doc.get("tm") else None
    r["condEstado"] = cond_estado(r)
    r["tempEstado"] = temp_estado(r)
    return r


def cond_estado(t: dict) -> str:
    if not t or t.get("cpVol") is None:
        return "sin_datos"
    f = t.get("flags")
    if (f and (f["err"] or f["ovc"])) or t["cpVol"] < MIN_CPVOL:
        return "alerta"
    if t["cpVol"] < MIN_CPVOL + 2 or (f and f["wrn"]):
        return "vigilancia"
    return "sano"


def temp_estado(t: dict) -> str:
    if not t or (t.get("dio1") is None and t.get("dio2") is None and t.get("tip") is None):
        return "sin_datos"
    max_dio = max(t.get("dio1") if t.get("dio1") is not None else -99,
                  t.get("dio2") if t.get("dio2") is not None else -99)
    delta = t.get("delta")
    if max_dio >= 55 or (delta is not None and delta >= 8):
        return "alerta"
    if max_dio >= 48 or (delta is not None and delta >= 5):
        return "vigilancia"
    return "sano"


def thermal_series(consola: str, dias: int = 30) -> list[dict[str, Any]]:
    db = C._db()
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=dias)
    cap = _future_cap()
    rows = list(db[MSG].aggregate([
        {"$match": {"dv": consola, "tm": {"$gte": since, "$lte": cap}}},
        {"$project": {
            "tm": 1,
            "hp": {"$arrayElemAt": [{"$filter": {"input": "$sm", "as": "s", "cond": {"$eq": ["$$s.cd", "HPTS"]}}}, 0]},
            "i2": {"$arrayElemAt": [{"$filter": {"input": "$sm", "as": "s", "cond": {"$eq": ["$$s.cd", "I2C2"]}}}, 0]},
            "st": {"$arrayElemAt": [{"$filter": {"input": "$sm", "as": "s", "cond": {"$eq": ["$$s.cd", "STAT"]}}}, 0]},
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$tm"}},
            "tip": {"$avg": {"$ifNull": ["$hp.tempTip", "$i2.ta1"]}},
            "dio1": {"$avg": {"$ifNull": ["$hp.tempDio1", "$i2.to1"]}},
            "dio2": {"$avg": {"$ifNull": ["$hp.tempDio2", "$i2.to2"]}},
            "cooler": {"$avg": "$hp.tempCooler"},
            "cpVol": {"$avg": "$st.cpVol"},
            "err": {"$max": {"$cond": ["$st.lsErr", 1, 0]}},
            "ovc": {"$max": {"$cond": ["$st.lsOvc", 1, 0]}},
            "wrn": {"$max": {"$cond": ["$st.dvWrn", 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ], allowDiskUse=True))
    return [{
        "d": str(r["_id"]), "tip": _num1(r.get("tip")), "dio1": _num1(r.get("dio1")),
        "dio2": _num1(r.get("dio2")), "cooler": _num1(r.get("cooler")), "cpVol": _num1(r.get("cpVol")),
        "err": r.get("err") or 0, "ovc": r.get("ovc") or 0, "wrn": r.get("wrn") or 0,
    } for r in rows]


# ===========================================================================
# SALUD DE DIODOS (calibrations · COMP)
# ===========================================================================

def _has_baremo(diodo: str) -> bool:
    s = str(diodo).strip()
    if not s:
        return False
    if re.search(r"sp", s, re.I):
        return False
    if s.startswith("093"):
        return False
    if re.fullmatch(r"\d{8,}", s):
        return False
    return True


def _diode_from_series(diodo: str, consola: str | None, manipulo: str | None,
                       last_seen: Any, life: float | None) -> dict[str, Any]:
    db = C._db()
    since_serie = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=DAYS_SERIE)
    own = C.owner_of(consola) if consola else None
    is_hl = bool(consola and str(consola).startswith("80")) or bool(
        own and re.search(r"happy", str(own.get("chain", "")), re.I)
    )
    docs = list(db[CAL].find(
        {"handpieceSid": diodo, "dateFrom": {"$gte": since_serie}, "isEmpty": {"$ne": True}},
        {"calibrations": 1, "dateFrom": 1},
    ))
    by_day: dict[str, dict] = {}
    dry = 0
    for doc in docs:
        for e in (doc.get("calibrations") or []):
            if not e or e.get("cd") != "COMP":
                continue
            pulse = C._num(e.get("pulse"))
            if pulse is None:
                continue
            if is_hl and pulse < 100:
                dry += 1
                continue
            tm = e.get("tm") or doc.get("dateFrom")
            day = C._as_utc(tm).strftime("%Y-%m-%d")
            b = by_day.setdefault(day, {"sum": 0.0, "n": 0, "under": 0})
            b["sum"] += pulse
            b["n"] += 1
            if e.get("check") == "under":
                b["under"] += 1
    serie = [
        {"d": d, "avg": round(by_day[d]["sum"] / by_day[d]["n"]), "n": by_day[d]["n"], "under": by_day[d]["under"]}
        for d in sorted(by_day)
    ]
    baremo = _has_baremo(diodo)
    last7 = serie[-7:]
    ma7 = pct_under7 = None
    if last7:
        s = sum(r["avg"] * r["n"] for r in last7)
        n = sum(r["n"] for r in last7)
        u = sum(r["under"] for r in last7)
        ma7 = round(s / n) if n else None
        pct_under7 = (u / n) if n else None
    first_symptom = None
    for r in serie:
        if r["n"] >= 3 and (r["avg"] < 780 or r["under"] / r["n"] >= 0.5):
            first_symptom = r["d"]
            break
    if not baremo:
        estado = "sin_baremo"
    elif ma7 is None:
        estado = "sin_datos"
    elif ma7 < 780 or (pct_under7 or 0) >= 0.5:
        estado = "alerta"
    elif ma7 < 1000 or (pct_under7 or 0) >= 0.2:
        estado = "vigilancia"
    else:
        estado = "sano"
    return {
        "diodo": diodo, "manipulo": manipulo, "consola": consola,
        "centro": (own.get("center") or own.get("chain")) if own else None,
        "cadena": own.get("chain") if own else None,
        "baremo": baremo, "estado": estado, "ma7": ma7,
        "pctUnder7": round(pct_under7 * 1000) / 1000 if pct_under7 is not None else None,
        "lastCal": serie[-1]["d"] if serie else None,
        "firstSymptom": first_symptom,
        "lifeShots": life,
        "nReal": sum(r["n"] for r in serie),
        "dryReadings": dry,
        "lastShot": C._as_utc(last_seen).isoformat() if last_seen else None,
        "serie": serie,
    }


def salud_diodos_de_consola(consola: str) -> list[dict[str, Any]]:
    """Salud de los diodos vivos de UNA consola (los vistos en telemetría este año)."""
    db = C._db()
    tramos = db[TRAMOS]
    vivos = list(tramos.aggregate([
        {"$match": {"consola": consola, "fecha": {"$gte": VIVO_DESDE}, "diodo": {"$nin": [None, ""]}}},
        {"$sort": {"tsEnd": 1}},
        {"$group": {"_id": "$diodo", "manipulo": {"$last": "$manipulo"},
                    "consola": {"$last": "$consola"}, "lastSeen": {"$last": "$tsEnd"}}},
    ], allowDiskUse=True))
    life_by_manip = _life_by_manip([str(v.get("manipulo") or "") for v in vivos if v.get("manipulo")])
    out = []
    for v in vivos:
        manip = str(v["manipulo"]) if v.get("manipulo") is not None else None
        out.append(_diode_from_series(
            str(v["_id"]), consola, manip, v.get("lastSeen"),
            life_by_manip.get(manip) if manip else None,
        ))
    rank = {"alerta": 0, "vigilancia": 1, "sano": 2, "sin_datos": 3, "sin_baremo": 4}
    out.sort(key=lambda a: (rank.get(a["estado"], 9), a["ma7"] if a["ma7"] is not None else 99999))
    return out


def salud_diodo(diodo: str) -> dict[str, Any]:
    """Salud de un diodo concreto (por su id). Localiza su última consola/manípulo."""
    db = C._db()
    rows = list(db[TRAMOS].find(
        {"diodo": diodo}, {"consola": 1, "manipulo": 1, "tsEnd": 1}
    ).sort("tsEnd", -1).limit(1))
    consola = str(rows[0]["consola"]) if rows and rows[0].get("consola") is not None else None
    manip = str(rows[0]["manipulo"]) if rows and rows[0].get("manipulo") is not None else None
    last_seen = rows[0].get("tsEnd") if rows else None
    life = _life_by_manip([manip]).get(manip) if manip else None
    return _diode_from_series(diodo, consola, manip, last_seen, life)


def _life_by_manip(manips: list[str]) -> dict[str, float]:
    manips = [m for m in set(manips) if m]
    if not manips:
        return {}
    db = C._db()
    rows = list(db[TRAMOS].aggregate([
        {"$match": {"manipulo": {"$in": manips}}},
        {"$group": {"_id": "$manipulo", "fpc": {"$max": "$fpcEnd"}}},
    ], allowDiskUse=True))
    out = {}
    for r in rows:
        f = C._num(r.get("fpc"))
        if f is not None:
            out[str(r["_id"])] = f
    return out


# ===========================================================================
# ESTADO ONLINE (disparando / en línea / offline)
# ===========================================================================

def estado_online(serial: str) -> dict[str, Any]:
    db = C._db()
    now = _dt.datetime.now(_dt.timezone.utc)
    live = C.build_today_tramos(db, serial, C.today_str())
    last_seen = live.get("lastSeen")
    shots_today = sum((C._num(t.get("shots")) or 0) for t in live.get("tramos", []))

    # última actividad histórica (de la colección) para clasificar offline
    last_row = list(db[TRAMOS].find({"consola": serial}, {"tsEnd": 1}).sort("tsEnd", -1).limit(1))
    last_activity = last_row[0].get("tsEnd") if last_row else None

    estado = "offline"
    detalle = None
    if last_seen:
        mins = (now - C._as_utc(last_seen)).total_seconds() / 60
        if mins <= FIRING_MIN and shots_today > 0:
            estado = "disparando"
        elif mins <= ONLINE_HOURS * 60:
            estado = "en línea (standby)"
        else:
            estado = "offline"
    if estado == "offline":
        ref = C._as_utc(last_activity) if last_activity else (C._as_utc(last_seen) if last_seen else None)
        if ref is not None:
            days = (now - ref).total_seconds() / 86400
            detalle = f"offline {'<7d (corto)' if days < OFFLINE_SHORT_DAYS else '>7d (largo)'}"
        else:
            detalle = "sin actividad registrada"

    return {
        "serial": serial,
        "estado": estado,
        "detalle": detalle,
        "disparosHoy": shots_today,
        "ultimoMensaje": C._as_utc(last_seen).isoformat() if last_seen else None,
        "ultimaActividad": C._as_utc(last_activity).isoformat() if last_activity else None,
        "owner": C.owner_of(serial),
    }


def mantenimiento_equipo(serial: str) -> dict[str, Any]:
    """Foto de mantenimiento de una consola: térmico + condensador + salud de sus diodos."""
    return {
        "serial": serial,
        "owner": C.owner_of(serial),
        "termico": thermal_last(serial),
        "diodos": salud_diodos_de_consola(serial),
    }
