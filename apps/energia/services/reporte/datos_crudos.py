"""Datos crudos de potencia activa por nodo (vars=ap), resolución ~15 min irregular.
Usados para el Caso 7 (suma de Riemann sobre electricidad cruda).

Puerto de process/src/internals/datos_crudos.py (repo Reporte-Energia).

El endpoint /api/node/{id}/measurements/?vars=ap retorna app1/app2/app3
(potencia activa por fase, en kW pese al rótulo [W] en los reportes Excel de Quoia).
Los valores negativos representan exportación (generación solar).
Los timestamps no son exactamente 15 min — el Δt se calcula entre mediciones reales.
"""
from __future__ import annotations

import pandas as pd

from apps.comun.integraciones.gaia_client import GaiaClient
from apps.energia.services.reporte.utils import limite_plausible_kwh

GAP_MAX_SEGUNDOS      = 1500  # 25 min: gap máximo tolerado entre mediciones consecutivas
UMBRAL_GENERACION_KW  = 5.0   # potencia total mínima (kW negativo) para considerar "generando"
CAMPOS_AP             = ("app1", "app2", "app3")  # potencia activa por fase, en kW (rótulo [W] incorrecto)
UMBRAL_PICO_MULTIPLO  = 15    # lectura > 15x la mediana del día = corrupta, se descarta


def get_datos_crudos(
    gaia: GaiaClient, node_id: int, fecha_str: str, capacidad_efectiva_mw: float | None = None,
) -> pd.DataFrame:
    """Potencia activa (vars=ap) de un nodo para una fecha (YYYY-MM-DD).

    Retorna DataFrame con columnas 'time', 'appd1', 'appd2', 'appd3'.
    DataFrame vacío si no hay datos o falla la petición.

    Se descartan las filas con 'recovered' == True -- son marcadores sintéticos
    que Quoia inserta justo en cada minuto múltiplo de 15, siempre en 0.0,
    segundos antes de la lectura real -- sin filtrarlas, la suma de Riemann
    integra el intervalo entre la lectura real y el marcador en 0 como si la
    potencia real se hubiera sostenido ~15 min completos en vez de los pocos
    segundos reales, multiplicando la energía por ~70x en ese intervalo.
    """
    filas = gaia.get_node_measurements(node_id, fecha_str, "ap")
    if not filas:
        return pd.DataFrame()

    df = pd.DataFrame(filas)
    if "recovered" in df.columns:
        df = df[df["recovered"] != True].reset_index(drop=True)  # noqa: E712

    return _descartar_picos_espurios(df, node_id, capacidad_efectiva_mw)


def _descartar_picos_espurios(
    df: pd.DataFrame, node_id: int | None = None, capacidad_efectiva_mw: float | None = None,
) -> pd.DataFrame:
    """Pone en 0 las lecturas de potencia con magnitud absurda -- picos de
    datos corruptos en la fuente, no generación real. Dos chequeos
    independientes (OR):

    1. Relativo al resto del propio día del nodo -- mediana de |potencia|
    entre lecturas ya significativas (> UMBRAL_GENERACION_KW) x
    UMBRAL_PICO_MULTIPLO (ej. mediana ~1 kW pero una lectura puntual de
    -343.964 kW, ~300x la mediana, que infla la suma de Riemann del Caso 7
    a millones de kWh en un solo día). No depende de calibrar la unidad
    real de app1/app2/app3.

    2. Absoluto, contra la capacidad de la frontera (limite_plausible_kwh()
    en utils.py, mismo criterio que medidor/CGM/SolarView/reconectador) --
    el chequeo (1) es ciego a un error CRÓNICO de escala de unidad (ej.
    Polaris 1/2, ~1.150x, un nodo que reporta en W en vez de kW: TODAS las
    lecturas del día quedan igual de infladas, así que ninguna se ve
    "atípica" frente a la mediana del propio día). Este chequeo sí lo
    detecta, comparando contra un techo físico externo al día.
    """
    if df.empty or not any(c in df.columns for c in CAMPOS_AP):
        return df

    df = df.copy()
    potencia_abs = sum(
        pd.to_numeric(df[c], errors="coerce").fillna(0) for c in CAMPOS_AP if c in df.columns
    ).abs()

    espurias = pd.Series(False, index=df.index)

    significativas = potencia_abs[potencia_abs > UMBRAL_GENERACION_KW]
    if not significativas.empty:
        mediana = significativas.median()
        if mediana > 0:
            espurias = espurias | (potencia_abs > mediana * UMBRAL_PICO_MULTIPLO)

    limite = limite_plausible_kwh(capacidad_efectiva_mw)
    if limite is not None:
        espurias = espurias | (potencia_abs > limite)

    if espurias.any():
        for c in CAMPOS_AP:
            if c in df.columns:
                df.loc[espurias, c] = 0.0

    return df


def proyecto_generando(df: pd.DataFrame) -> bool:
    """True si el proyecto generó energía ese día (hay potencia negativa significativa).

    Usa el mismo umbral que datos_completos pero sin verificar completitud.
    Permite distinguir Caso 6 (apagado) de Casos 7/8 (encendido sin datos).
    """
    if df.empty:
        return False
    potencia_total = sum(
        pd.to_numeric(df[c], errors="coerce").fillna(0)
        for c in CAMPOS_AP if c in df.columns
    )
    return bool((potencia_total < -UMBRAL_GENERACION_KW).any())


def datos_completos(df: pd.DataFrame) -> bool:
    """True si los datos de generación cubren la ventana activa sin brechas.

    Detecta dinámicamente el inicio y fin de generación (potencia negativa
    real) y verifica que no haya brechas > GAP_MAX_SEGUNDOS dentro de ese
    rango. No asume una ventana solar fija.
    """
    if df.empty:
        return False

    df = df.copy()
    df["_t"] = pd.to_datetime(df["time"])
    df = df.sort_values("_t").reset_index(drop=True)

    potencia_total = sum(
        pd.to_numeric(df[c], errors="coerce").fillna(0)
        for c in CAMPOS_AP if c in df.columns
    )

    generando = df.loc[potencia_total < -UMBRAL_GENERACION_KW, "_t"]
    if generando.empty:
        return False  # proyecto apagado o sin generación ese día

    inicio = generando.iloc[0]
    fin    = generando.iloc[-1]

    # Guardia: si el último registro del día es antes de las 20:00, el
    # medidor dejó de reportar antes del fin del día -> incompleto
    if df["_t"].iloc[-1].hour < 20:
        return False

    ventana = df.loc[(df["_t"] >= inicio) & (df["_t"] <= fin), "_t"]
    gaps = ventana.diff().dt.total_seconds().dropna()
    return bool((gaps <= GAP_MAX_SEGUNDOS).all())


def _potencia_exportada_kw(row: pd.Series) -> float:
    """Potencia exportada (generación solar) en kW para un intervalo.

    La API retorna valores negativos durante exportación (generación).
    Retorna 0 en intervalos de importación para no contaminar la suma.
    """
    total = sum(float(row.get(f, 0) or 0) for f in CAMPOS_AP)
    return abs(total) if total < 0 else 0.0


def _preparar_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_t"] = pd.to_datetime(df["time"])
    return df.sort_values("_t").reset_index(drop=True)


def riemann_eae(df: pd.DataFrame) -> float:
    """kWh totales de generación del día vía suma de Riemann.

    Usa los Δt reales entre timestamps consecutivos. El último intervalo
    reutiliza el mismo Δt que el penúltimo.
    """
    if df.empty:
        return 0.0

    df = _preparar_df(df)
    total = 0.0

    for i in range(len(df) - 1):
        p_kw = _potencia_exportada_kw(df.iloc[i])
        dt_h = (df.at[i + 1, "_t"] - df.at[i, "_t"]).total_seconds() / 3600
        total += p_kw * dt_h

    if len(df) >= 2:
        p_kw = _potencia_exportada_kw(df.iloc[-1])
        dt_h = (df.at[len(df) - 1, "_t"] - df.at[len(df) - 2, "_t"]).total_seconds() / 3600
        total += p_kw * dt_h

    return round(total, 4)


def curva_horaria_ap(df: pd.DataFrame) -> pd.Series:
    """kWh de generación por hora (pd.Series[0..23]) vía suma de Riemann."""
    if df.empty:
        return pd.Series({h: 0.0 for h in range(24)}, dtype=float)

    df = _preparar_df(df)
    acum = {h: 0.0 for h in range(24)}

    for i in range(len(df) - 1):
        hora = df.at[i, "_t"].hour
        p_kw = _potencia_exportada_kw(df.iloc[i])
        dt_h = (df.at[i + 1, "_t"] - df.at[i, "_t"]).total_seconds() / 3600
        acum[hora] += p_kw * dt_h

    if len(df) >= 2:
        hora = df.at[len(df) - 1, "_t"].hour
        p_kw = _potencia_exportada_kw(df.iloc[-1])
        dt_h = (df.at[len(df) - 1, "_t"] - df.at[len(df) - 2, "_t"]).total_seconds() / 3600
        acum[hora] += p_kw * dt_h

    return pd.Series(acum, dtype=float)


def curva_horaria_ap_con_huecos(df: pd.DataFrame) -> pd.Series:
    """Igual que curva_horaria_ap(), pero deja en NaN las horas donde no llegó
    NINGÚN punto crudo (en vez de 0.0) -- para el Caso 8: cuando los datos
    crudos están incompletos, se aprovechan las horas que sí tienen cobertura
    real y solo se marcan como "sin dato" las que de verdad no tienen ningún
    punto, para poder rellenarlas después (reconectador/Solenium/histórico)."""
    if df.empty:
        return pd.Series({h: None for h in range(24)}, dtype=float)

    df = _preparar_df(df)
    acum = {h: 0.0 for h in range(24)}
    horas_con_dato = set(df["_t"].dt.hour)

    for i in range(len(df) - 1):
        hora = df.at[i, "_t"].hour
        p_kw = _potencia_exportada_kw(df.iloc[i])
        dt_h = (df.at[i + 1, "_t"] - df.at[i, "_t"]).total_seconds() / 3600
        acum[hora] += p_kw * dt_h

    if len(df) >= 2:
        hora = df.at[len(df) - 1, "_t"].hour
        p_kw = _potencia_exportada_kw(df.iloc[-1])
        dt_h = (df.at[len(df) - 1, "_t"] - df.at[len(df) - 2, "_t"]).total_seconds() / 3600
        acum[hora] += p_kw * dt_h

    curva = pd.Series(acum, dtype=float)
    for h in range(24):
        if h not in horas_con_dato:
            curva[h] = None
    return curva
