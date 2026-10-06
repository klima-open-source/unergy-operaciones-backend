"""Generación horaria por proyecto reportada en SolarView (antes Solenium).
Endpoint principal: GET /solarview/measurements/generation/?project_id=...

Puerto de process/src/internals/energia_solenium.py (repo Reporte-Energia).

Sobre /power/: algunos proyectos tienen /generation/ (y /energy/)
completamente vacíos aunque SÍ generan de verdad (encontrado con "MGS 0033 -
Sabana de Torres": /inverter/ y /power/ sí tenían datos reales). Se probó
como respaldo de e_inv (Caso 3, con FP) y dio un total más lejano al valor
real que el reconectador -- por eso solo se usa como último recurso,
reportado DIRECTO sin FP (rescate de Caso 6, ver clasificador.py).
"""
from __future__ import annotations

import pandas as pd

from apps.comun.integraciones.solarview_client import SolarViewClient
from apps.energia.services.reporte.curvas import dia_completo
from apps.energia.services.reporte.utils import limite_plausible_kwh

HORAS = list(range(24))


def _curva_de_resp(resp: dict) -> pd.Series:
    """Convierte la respuesta de /generation/ en pd.Series[0..23]."""
    gen_kwh = resp.get("generation_kwh", {}) if resp else {}
    curva = pd.Series([None] * 24, index=HORAS, dtype=float)
    for ts, kwh in gen_kwh.items():
        hora = int(ts[11:13])
        if 0 <= hora < 24:
            curva[hora] = kwh
    return curva


def curva_de_power(
    resp: dict, capacidad_efectiva_mw: float | None = None,
) -> tuple[pd.Series, set[int]]:
    """Reconstruye la curva horaria a partir de /power/ (5 min) e integrando
    por Riemann -- respaldo cuando /generation/ viene vacío.

    Se pide con total_power=1 (ver SolarViewClient.get_power), así que
    `results.power` ya viene sumado entre todos los inversores -- a
    diferencia de la API vieja de Solenium, que devolvía potencia por
    inversor (`{inversor: {ts: kw}}`) y había que sumar acá.

    Si se pasa capacidad_efectiva_mw, las horas físicamente implausibles
    (ver limite_plausible_kwh() en utils.py) se descartan igual que en
    curva_generacion() -- acá el riesgo es mayor: este resultado se reporta
    DIRECTO como curva_final (rescate de Caso 6/7 en clasificador.py), sin
    ningún FP ni comparación de por medio que amortigüe un valor absurdo."""
    power = resp.get("results", {}).get("power", {}) if isinstance(resp, dict) else {}
    if not power:
        return pd.Series([None] * 24, index=HORAS, dtype=float), set()

    combinado = {ts: float(val) for ts, val in power.items() if val is not None}

    if not combinado:
        return pd.Series([None] * 24, index=HORAS, dtype=float), set()

    df = pd.DataFrame([{"time": ts, "kw": kw} for ts, kw in combinado.items()])
    df["_t"] = pd.to_datetime(df["time"])
    df = df.sort_values("_t").reset_index(drop=True)

    acum = {h: 0.0 for h in HORAS}
    horas_con_dato = set(df["_t"].dt.hour)
    for i in range(len(df) - 1):
        hora = df.at[i, "_t"].hour
        dt_h = (df.at[i + 1, "_t"] - df.at[i, "_t"]).total_seconds() / 3600
        acum[hora] += df.at[i, "kw"] * dt_h
    if len(df) >= 2:
        hora = df.at[len(df) - 1, "_t"].hour
        dt_h = (df.at[len(df) - 1, "_t"] - df.at[len(df) - 2, "_t"]).total_seconds() / 3600
        acum[hora] += df.at[len(df) - 1, "kw"] * dt_h

    curva = pd.Series(acum, dtype=float)
    for h in HORAS:
        if h not in horas_con_dato:
            curva[h] = None

    limite = limite_plausible_kwh(capacidad_efectiva_mw)
    if limite is not None:
        implausibles = curva.abs() > limite
        if implausibles.any():
            curva[implausibles] = None
            horas_con_dato -= set(curva.index[implausibles])

    return curva, horas_con_dato


def _horas_reportadas(gen_kwh: dict) -> set[int]:
    """Horas (0-23) para las que Solenium devolvió un valor real, sin importar
    cuál -- permite distinguir 'no generó' (valor 0) de 'no reportó'.

    Solenium siempre devuelve las 24 claves del día, incluso cuando no tiene
    dato -- en ese caso el valor es 'None' explícito, la clave NO desaparece.
    Contar solo la presencia de la clave nunca detecta ningún hueco.
    """
    horas = set()
    for ts, kwh in gen_kwh.items():
        if kwh is None:
            continue
        try:
            hora = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        if 0 <= hora < 24:
            horas.add(hora)
    return horas


def curva_generacion(
    sv: SolarViewClient, project_id_solarview: int | None, fecha_str: str,
    capacidad_efectiva_mw: float | None = None,
) -> tuple[pd.Series, bool]:
    """(curva horaria kWh, completo) de generación SolarView para un proyecto y fecha.

    Un hueco en inversores entiende el total por debajo de lo real (fillna(0)
    en la suma), y ese total se usa como referencia para validar CGM y
    medidores -- por eso importa saber si es confiable ('completo').

    Si se pasa capacidad_efectiva_mw, las horas físicamente implausibles
    (ver limite_plausible_kwh() en utils.py -- mismo criterio que ya usa
    reconectador.get_curva_reconectador()) se tratan como huecos (None), no
    como generación real: ver MGS 0010 Villanueva 2026-08-26, un valor de
    ~48.090 kWh en una sola hora para una frontera de 0,99 MW (glitch de
    SolarView) se colaba en e_inv/curva_solenium_referencia, contaminando
    la comparación medidor-vs-inversores de Caso 3 y aplastando la escala
    del gráfico.
    """
    vacia = pd.Series([None] * 24, index=HORAS, dtype=float)
    if project_id_solarview is None:
        return vacia, False
    resp = sv.get_generation(int(project_id_solarview), fecha_str, fecha_str)
    if not resp:
        return vacia, False
    curva = _curva_de_resp(resp)
    horas_reportadas = _horas_reportadas(resp.get("generation_kwh", {}))

    limite = limite_plausible_kwh(capacidad_efectiva_mw)
    if limite is not None:
        implausibles = curva.abs() > limite
        if implausibles.any():
            curva[implausibles] = None
            horas_reportadas -= set(curva.index[implausibles])

    completo = dia_completo(curva, horas_reportadas)
    return curva, completo
