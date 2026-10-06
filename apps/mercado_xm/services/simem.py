"""Precio de bolsa mensual desde la API pública de SIMEM, para el valor a indemnizar.

La bolsa vive en el dataset SIMEM **709b84** (variable `PB_Nal`, precio HORARIO en
COP/kWh). El valor del mes (PNBA) se calcula así:

  1. por cada (día, hora) se toma la **versión de liquidación más nueva disponible**:
     TXR sale primero y se usa mientras tanto; cuando XM publica **TXF** (~el día 10
     del mes siguiente) esta gana y el valor se actualiza solo,
  2. cada valor horario se **redondea a 2 decimales**,
  3. se promedian TODAS las horas del mes y se redondea a 2 → PNBA del mes.

Este es el mismo procedimiento del Excel de Compensación de la usuaria (la hoja
"Bolsa": 24 horas por día y `AX34 = ROUND(AVERAGE(horas), 2)`).

Techo (opcional): un tope mensual (p. ej. precio de escasez) que recorta cada hora
antes de promediar (`min(hora, techo)`). En el Excel es la celda AB1; si no se pasa,
no se techa (en meses normales ninguna hora lo supera, así que no cambia el número).

Si SIMEM no responde se devuelve `precio_bolsa=None` y la vista muestra "s/precio"
en vez de romperse.
"""

import calendar
import logging
from collections import defaultdict
from statistics import mean

import httpx

logger = logging.getLogger("operaciones.simem")

SIMEM_URL = "https://www.simem.co/backend-files/api/PublicData"
DATASET_BOLSA = "709b84"
VARIABLE_NACIONAL = "PB_Nal"
# Precio de las transacciones en bolsa. XM lo publica EN LUGAR del nacional en
# las horas en que la bolsa supera el precio de escasez de activación.
VARIABLE_TRANSACCIONES = "PTB"
_TIMEOUT = httpx.Timeout(10.0, read=60.0)

# Definitividad de las liquidaciones XM (menor = más preliminar; TXF es la
# definitiva y gana cuando existe). Se toma la más alta disponible: TXR mientras
# TXF no esté, TXF en cuanto XM la publica. Explícito para no caer en el orden
# lexicográfico ('TX10' < 'TX2').
_ORDEN_VERSIONES = ["TX1", "TX2", "TX3", "TX4", "TX5", "TXR", "TXF"]


def _version_rank(version) -> int:
    try:
        return _ORDEN_VERSIONES.index(str(version).upper())
    except ValueError:
        return -1


def fetch_records(start: str, end: str, *, client: httpx.Client | None = None) -> list[dict]:
    """GET a SIMEM PublicData para el dataset de bolsa. `client` inyectable en tests."""
    params = {"startdate": start, "enddate": end, "datasetId": DATASET_BOLSA}
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


def bolsa_horaria(
    records: list[dict], variable: str = VARIABLE_NACIONAL,
) -> dict[tuple[str, str], float]:
    """{records SIMEM} -> {('YYYY-MM-DD', 'HH'): precio_hora (COP/kWh, 2 dec)}.

    Por cada (día, hora) usa la versión más nueva disponible (TXF gana; si no está,
    TXR) y redondea a 2 decimales, igual que el Excel.

    **Las horas sin `PB_Nal` se toman del `PTB`.** En esas horas el precio de bolsa
    superó el de escasez de activación y XM publica el precio de las transacciones
    en bolsa en vez del nacional: son dos nombres del mismo dato según el régimen,
    no dos datos distintos. En 2026-09 fueron 18 horas —todas en el pico de la
    noche— y dejarlas fuera sesgaba el promedio del mes hacia abajo, justo por lo
    caro. Ver :func:`bolsa_mensual`.

    Si una hora trae las dos, manda `PB_Nal`.
    """
    mejor: dict[tuple[str, str], tuple[int, int]] = {}
    valor: dict[tuple[str, str], float] = {}
    # (prioridad de variable, …): PB_Nal pisa a PTB venga en el orden que venga.
    prioridad = {variable: 1, VARIABLE_TRANSACCIONES: 0}
    for r in records:
        pri = prioridad.get(r.get("CodigoVariable"))
        if pri is None:
            continue
        fh = str(r.get("FechaHora", ""))
        dia, hora = fh[:10], fh[11:13]
        if not dia or not hora:
            continue
        rk = _version_rank(r.get("Version"))
        if rk < 0:
            continue
        clave = (dia, hora)
        if clave not in mejor or (pri, rk) > mejor[clave]:
            try:
                valor[clave] = round(float(r["Valor"]), 2)
                mejor[clave] = (pri, rk)
            except (TypeError, ValueError, KeyError):
                continue
    return valor


def horas_de_transacciones(
    records: list[dict], variable: str = VARIABLE_NACIONAL,
) -> set[tuple[str, str]]:
    """Las (día, hora) que solo existen como `PTB`: precio sobre el de escasez."""
    con_nal = {
        (str(r.get("FechaHora", ""))[:10], str(r.get("FechaHora", ""))[11:13])
        for r in records if r.get("CodigoVariable") == variable
    }
    return {
        clave for r in records
        if r.get("CodigoVariable") == VARIABLE_TRANSACCIONES
        and (clave := (str(r.get("FechaHora", ""))[:10],
                       str(r.get("FechaHora", ""))[11:13])) not in con_nal
        and all(clave)
    }


def bolsa_mensual(anio: int, mes: int, *, techo: float | None = None,
                  client: httpx.Client | None = None) -> dict:
    """Precio de bolsa del mes (COP/kWh) tal como lo calcula el Excel de Compensación.

    Devuelve `precio_bolsa` (PNBA del mes) más el detalle por día/hora para poder
    reconstruir la hoja Bolsa del export. `precio_bolsa=None` si SIMEM no trae datos.
    """
    horaria: dict[tuple[str, str], float] = {}
    del_ptb: set[tuple[str, str]] = set()
    try:
        ultimo = calendar.monthrange(anio, mes)[1]
        registros = fetch_records(
            f"{anio}-{mes:02d}-01", f"{anio}-{mes:02d}-{ultimo:02d}", client=client
        )
        horaria = bolsa_horaria(registros)
        del_ptb = horas_de_transacciones(registros)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SIMEM bolsa no disponible para %s-%02d: %s", anio, mes, exc)

    if not horaria:
        return {"precio_bolsa": None, "horas": 0, "dias": 0, "techo": techo,
                "horas_techadas": 0, "horas_ptb": 0, "detalle": {}}

    detalle: dict[str, dict[str, float]] = defaultdict(dict)
    valores, horas_techadas = [], 0
    for (dia, hora), v in horaria.items():
        if techo is not None and v > techo:
            v = round(float(techo), 2)
            horas_techadas += 1
        detalle[dia][hora] = v
        valores.append(v)

    return {
        "precio_bolsa": round(mean(valores), 2),
        "horas": len(valores),
        "dias": len(detalle),
        "techo": techo,
        "horas_techadas": horas_techadas,
        # Horas tomadas del PTB por no haber PB_Nal: la vista las nombra, porque
        # son horas con el precio por encima del de escasez.
        "horas_ptb": len(del_ptb),
        "detalle": {d: dict(sorted(h.items())) for d, h in sorted(detalle.items())},
    }
