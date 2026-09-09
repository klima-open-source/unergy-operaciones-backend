"""Precio Techo de Bolsa (PTB) desde la API pública de SIMEM.

El PTB vive en el dataset SIMEM **709b84** (variable `PB_Nal`, precio horario en
COP/kWh). Se usa como TECHO del precio de bolsa: la energía que un PPA incumplió y
el comprador tuvo que comprar en bolsa no se le cobra por encima del techo.

`fetch_records`/`ptb_diario` son la capa de red (httpx) + agregado por día; el
capping vive en `precio_bolsa_techado`, que combina el techo con nuestro precio de
bolsa diario (`precios_bolsa_diario`, de EVO). Todo en COP/kWh.

Sigue el patrón de `evo.py`: si SIMEM no responde, se loguea y se sigue SIN techo
(mejor un valor sin techar que romper la vista); el resultado dice si el techo se
pudo aplicar (`ptb_disponible`).
"""

import calendar
import logging
from collections import defaultdict
from statistics import mean

import httpx
from django.db import connection

logger = logging.getLogger("operaciones.simem")

SIMEM_URL = "https://www.simem.co/backend-files/api/PublicData"
DATASET_PTB = "709b84"
VARIABLE_NACIONAL = "PB_Nal"
_TIMEOUT = httpx.Timeout(10.0, read=40.0)

# Definitividad de las liquidaciones XM (menor = más preliminar). Explícito para
# no caer en el orden lexicográfico ('TX10' < 'TX2'). Igual que en simem_bolsa.
_ORDEN_VERSIONES = ["TX1", "TX2", "TX3", "TX4", "TX5", "TXR", "TXF"]


def _version_rank(version) -> int:
    try:
        return _ORDEN_VERSIONES.index(str(version).upper())
    except ValueError:
        return -1


def fetch_records(start: str, end: str, *, client: httpx.Client | None = None) -> list[dict]:
    """GET a SIMEM PublicData para el dataset del PTB. `client` inyectable en tests."""
    params = {"startdate": start, "enddate": end, "datasetId": DATASET_PTB}
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
    return []


def ptb_diario(records: list[dict], variable: str = VARIABLE_NACIONAL) -> dict[str, float]:
    """{records SIMEM} -> {'YYYY-MM-DD': PTB_promedio_dia (COP/kWh)}.

    Por cada día usa SOLO las filas de la Version más alta presente y promedia sus
    horas. Mismo criterio de recencia+refinamiento que el conector de bolsa.
    """
    mejor: dict[str, int] = {}
    for r in records:
        if r.get("CodigoVariable") != variable:
            continue
        dia = str(r.get("FechaHora", ""))[:10]
        if not dia:
            continue
        rk = _version_rank(r.get("Version"))
        if dia not in mejor or rk > mejor[dia]:
            mejor[dia] = rk
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
    return {dia: mean(v) for dia, v in acc.items() if v}


def _nuestro_bolsa_diario(anio: int, mes: int) -> dict[str, float]:
    """{'YYYY-MM-DD': precio_promedio_dia} de nuestra tabla `precios_bolsa_diario`."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT to_char(fecha, 'YYYY-MM-DD'), precio_promedio
            FROM precios_bolsa_diario
            WHERE EXTRACT(YEAR FROM fecha) = %s
              AND EXTRACT(MONTH FROM fecha) = %s
              AND precio_promedio IS NOT NULL
        """, [anio, mes])
        return {d: float(v) for d, v in cur.fetchall()}


def precio_bolsa_techado(anio: int, mes: int, *, client: httpx.Client | None = None) -> dict:
    """Precio de bolsa del mes (COP/kWh) TECHADO por el PTB de SIMEM, día a día.

    Cada día se recorta nuestro precio de bolsa al PTB (`min(bolsa, PTB)`) y se
    promedian los días. Si SIMEM no responde, se devuelve el promedio SIN techo y
    `ptb_disponible=False` (no se rompe la vista por una caída de SIMEM).
    """
    nuestro = _nuestro_bolsa_diario(anio, mes)
    if not nuestro:
        return {"precio_bolsa": None, "dias": 0, "dias_techados": 0,
                "ptb_disponible": False, "ptb_promedio": None}

    techo: dict[str, float] = {}
    try:
        ultimo = calendar.monthrange(anio, mes)[1]
        registros = fetch_records(f"{anio}-{mes:02d}-01", f"{anio}-{mes:02d}-{ultimo:02d}", client=client)
        techo = ptb_diario(registros)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SIMEM PTB no disponible para %s-%02d: %s", anio, mes, exc)

    capados, dias_techados = [], 0
    for dia, bolsa in nuestro.items():
        ptb = techo.get(dia)
        if ptb is not None and ptb < bolsa:
            capados.append(ptb)
            dias_techados += 1
        else:
            capados.append(bolsa)

    ptb_prom = round(mean(techo.values()), 2) if techo else None
    return {
        "precio_bolsa": round(mean(capados), 2),
        "dias": len(capados),
        "dias_techados": dias_techados,
        "ptb_disponible": bool(techo),
        "ptb_promedio": ptb_prom,
    }
