"""Conector SIMEM para el precio de bolsa nacional (PB_Nal), AISLADO de garantías.

NO toca el pipeline EVO/`precios_bolsa_diario` existente. Fuente: API pública de SIMEM,
dataset EC6945 (precio de bolsa horario, COP/kWh). Por cada día se toma la `Version` más
alta disponible (TX1, TX2, …): los días recientes solo tienen TX1, los más viejos ya
tienen TX2; tomar el máximo por día da recencia + refinamiento a la vez.

Parseo/agregado en funciones puras (sin red); `fetch_records` hace la llamada httpx;
`precio_bolsa_prom_7d` orquesta. Salida en COP/kWh.
"""
from __future__ import annotations

from collections import defaultdict

import httpx

SIMEM_URL = "https://www.simem.co/backend-files/api/PublicData"
DATASET_PRECIO_BOLSA = "EC6945"
VARIABLE_NACIONAL = "PB_Nal"

# Orden de definitividad de las liquidaciones XM (menor = más preliminar). Explícito
# para evitar el bug de orden lexicográfico ('TX10' < 'TX2'). Ajustable si aparecen más.
_ORDEN_VERSIONES = ["TX1", "TX2", "TX3", "TX4", "TX5", "TXR", "TXF"]


def _version_rank(version: str) -> int:
    """Rank de definitividad; versión desconocida cae al fondo (-1)."""
    try:
        return _ORDEN_VERSIONES.index(str(version).upper())
    except ValueError:
        return -1


def promedio_diario_max_version(records: list[dict], variable: str = VARIABLE_NACIONAL) -> dict[str, float]:
    """{records SIMEM} -> {'YYYY-MM-DD': precio_promedio_dia}.

    Filtra por CodigoVariable == variable. Por cada día usa SOLO las filas de la Version
    más alta presente ese día, y promedia sus horas.
    """
    # 1) mejor versión por día
    mejor: dict[str, int] = {}
    for r in records:
        if r.get("CodigoVariable") != variable:
            continue
        dia = str(r.get("FechaHora", ""))[:10]
        if not dia:
            continue
        rank = _version_rank(r.get("Version"))
        if dia not in mejor or rank > mejor[dia]:
            mejor[dia] = rank
    # 2) acumular horas de la mejor versión
    acc: dict[str, list[float]] = defaultdict(list)
    for r in records:
        if r.get("CodigoVariable") != variable:
            continue
        dia = str(r.get("FechaHora", ""))[:10]
        if not dia or _version_rank(r.get("Version")) != mejor.get(dia):
            continue
        try:
            acc[dia].append(float(r["Valor"]))
        except (TypeError, ValueError, KeyError):
            continue
    return {dia: sum(v) / len(v) for dia, v in acc.items() if v}


def promedio_ultimos_n_dias(daily: dict[str, float], n: int = 7) -> float | None:
    """Promedio de los últimos n días CONOCIDOS (por fecha, no calendario). None si vacío."""
    if not daily:
        return None
    ultimos = sorted(daily)[-n:]
    vals = [daily[d] for d in ultimos]
    return sum(vals) / len(vals)


_TIMEOUT = httpx.Timeout(10.0, read=40.0)


def fetch_records(start: str, end: str, *, dataset: str = DATASET_PRECIO_BOLSA,
                  client: httpx.Client | None = None) -> list[dict]:
    """GET a SIMEM PublicData. Devuelve result.records (o [] si no hay). `client`
    inyectable para tests (MockTransport)."""
    params = {"startdate": start, "enddate": end, "datasetId": dataset}
    propio = client is None
    cli = client or httpx.Client(timeout=_TIMEOUT, headers={"User-Agent": "unergy-ops/1.0"})
    try:
        resp = cli.get(SIMEM_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if propio:
            cli.close()
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict) and isinstance(result.get("records"), list):
        return result["records"]
    if isinstance(data, dict) and isinstance(data.get("Records"), list):
        return data["Records"]
    return []


def precio_bolsa_prom_7d(start: str, end: str, *, n_dias: int = 7,
                         client: httpx.Client | None = None) -> float | None:
    """Orquesta: fetch SIMEM -> promedio diario (versión más alta por día) -> promedio de
    los últimos n días conocidos. Devuelve COP/kWh (o None si SIMEM no trae datos)."""
    records = fetch_records(start, end, client=client)
    daily = promedio_diario_max_version(records)
    return promedio_ultimos_n_dias(daily, n_dias)
