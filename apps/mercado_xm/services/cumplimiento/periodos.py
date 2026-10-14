"""Aritmética de períodos, vigencias y consolidación anual de Cumplimiento.

Copiado SIN CAMBIOS de `app/api/v1/cumplimiento.py`. Son funciones puras — recortar
una vigencia contra el mes, restar los tramos ya ocupados para saber qué quedó en
bolsa, prorratear un compromiso por días activos, consolidar los doce meses de un
contrato. No tocan la base, así que no hay nada que traducir al ORM de Django.

Es la mitad del módulo que SÍ se puede mover verbatim: 3 805 líneas de router en
las que la lógica de negocio vivía mezclada con la sesión de SQLAlchemy. Separarla
antes de reescribir las consultas es lo que hace revisable el resto del puerto.

**`_restar_intervalos` es el corazón del remanente e/f.** Una planta que sale de
su contrato a mitad de mes aporta un tramo a la bolsa y sigue listada en (a) con
su ventana: por eso la misma planta puede tener varias filas en un mes.
"""

from __future__ import annotations

import calendar
import time as _time
from datetime import date, timedelta
from typing import Optional

from apps.mercado_xm.models import CumplimientoMensual
from apps.plataforma.services.fechas import hoy_col

# Todo el módulo original resolvía "hoy" con `date.today()`. El contenedor corre
# en UTC y Colombia es UTC−5: entre las 19:00 y medianoche de Bogotá el servidor
# ya está en el día siguiente, y el último día del mes eso cambia de MES — el
# corte de vigencias y la proyección del mes en curso salían del período
# equivocado. Se usa `hoy_col()` (CLAUDE.md).


UNGC_COMERCIALIZADOR = "UNGC"

def _vigencia_window(fecha_inicio, fecha_fin, first_day: date, last_day: date):
    """Ventana efectiva [eff_start, eff_end] (ambos inclusive) de un registro
    dentro del período [first_day, last_day], recortada a esos límites."""
    eff_start = max(first_day, fecha_inicio) if fecha_inicio else first_day
    eff_end = min(last_day, fecha_fin) if fecha_fin else last_day
    return eff_start, eff_end

def _gen_vigencia_mwh(
    eff_start: date,
    eff_end: date,
    dias_periodo: int,
    gen_periodo_completo: Optional[float],
    gen_rango: Optional[float],
) -> Optional[float]:
    """Generación REAL atribuible a un registro vigente [eff_start, eff_end] dentro
    de un período de `dias_periodo` días (típicamente el mes, o el mes hasta el
    corte para el mes en curso).

    - Vigencia que cubre TODO el período → `gen_periodo_completo` (ya es la
      generación real de esos días; no hace falta pedir el rango aparte).
    - Vigencia PARCIAL → `gen_rango` (suma real de esos días exactos, vía
      `_fetch_range`/`_sumar_deltas_en_rango`) — NUNCA el total del período
      prorrateado por fracción de días: la generación diaria no es pareja
      (clima, etc.), así que `total × dias/dias_mes` (regla de tres) ≠ suma real.

    Devuelve None si no hay días activos o falta el dato correspondiente.

    Fuente ÚNICA de verdad de energía para las tres vistas — Estrategia
    (/simulador), Matriz (/anual-matriz) y Energía transada (/energia-transada):
    todas deben dar el mismo número para la misma planta/mes.
    """
    dias_activos = max(0, (eff_end - eff_start).days + 1)
    if dias_activos <= 0:
        return None
    if dias_activos >= dias_periodo:
        return gen_periodo_completo
    return gen_rango

def _contrato_vigente_en_mes(contrato, year: int, month: int) -> bool:
    """True si el contrato está vigente en (year, month) según fecha_inicio/fecha_fin.

    Un compromiso del mes M solo cuenta si:
      (fecha_inicio IS NULL OR fecha_inicio <= último día de M) AND
      (fecha_fin    IS NULL OR fecha_fin    >= primer día de M).
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return (
        (contrato.fecha_inicio is None or contrato.fecha_inicio <= last_day)
        and (contrato.fecha_fin is None or contrato.fecha_fin >= first_day)
    )

def _fecha_corte(year: int, month: int, hoy: date | None = None) -> date:
    """Fecha contra la que se decide si una ventana sigue vigente.

    Mes en curso → HOY: una planta cuyo contrato termina el 30 sigue vigente el
    26. Mes pasado → último día del mes (foto de cierre). Mes futuro → último
    día del mes (proyección).
    """
    hoy = hoy or hoy_col()
    if (year, month) == (hoy.year, hoy.month):
        return hoy
    return date(year, month, calendar.monthrange(year, month)[1])

def _recortar(ini: date | None, fin: date | None, lo: date, hi: date):
    """Intersección de [ini, fin] con [lo, hi]. Bordes nulos = abiertos.
    Devuelve None si no se tocan."""
    a = max(ini, lo) if ini else lo
    b = min(fin, hi) if fin else hi
    return (a, b) if a <= b else None

def _restar_intervalos(base, ocupados: list) -> list:
    """Días de `base` (par (ini, fin)) que NO cubre ninguno de `ocupados`.

    Es lo que convierte la bolsa de "plantas sin contrato" a "días sin
    contrato": el residuo de una planta liberada el 23-jul es [24-jul, 31-jul].
    """
    if base is None:
        return []
    libres = [base]
    for oi, of in sorted(ocupados):
        nuevos = []
        for li, lf in libres:
            if of < li or oi > lf:  # sin solape: el tramo sobrevive entero
                nuevos.append((li, lf))
                continue
            if li < oi:
                nuevos.append((li, oi - timedelta(days=1)))
            if lf > of:
                nuevos.append((of + timedelta(days=1), lf))
        libres = nuevos
    return libres

def _estado_segmento(ini: date, fin: date, corte: date) -> str:
    """'terminado' (se acabó antes del corte) | 'futuro' (aún no empieza) |
    'vigente'. Los contadores de la vista solo cuentan 'vigente'."""
    if fin < corte:
        return "terminado"
    if ini > corte:
        return "futuro"
    return "vigente"

def _con_segmento(entry: dict, ini: date | None, fin: date | None,
                  first_day: date, last_day: date, corte: date) -> dict:
    """Añade a una fila de planta su ventana recortada al mes y su estado."""
    seg = _recortar(ini, fin, first_day, last_day) or (first_day, last_day)
    entry["segmento_inicio"] = seg[0].isoformat()
    entry["segmento_fin"] = seg[1].isoformat()
    entry["estado"] = _estado_segmento(seg[0], seg[1], corte)
    return entry

def _rollup_cumplimiento(meses: list[dict]) -> dict:
    """Deriva el rollup anual de cumplimiento a partir de los 12 meses de un contrato.

    Consume la lista producida por `_anual_meses_para_contrato` y devuelve un resumen
    con las métricas clave de cumplimiento anual.
    """
    deficit = sum(1 for m in meses if m.get("estado") == "deficit")
    bolsa = sum(
        (m.get("compras_bolsa_mwh") or 0) + (m.get("exposicion_bolsa_duplicados_mwh") or 0)
        for m in meses
    )
    return {
        "estado_cumplimiento": "no_cumple" if deficit > 0 else "cumple",
        "meses_en_deficit": deficit,
        "requiere_bolsa": bolsa > 0,
        "total_anual_mwh": round(sum(m.get("valor_mwh") or 0 for m in meses), 3),
        "total_min_anual_mwh": round(sum(m.get("min_mwh") or 0 for m in meses), 3),
        "bolsa_anual_mwh": round(bolsa, 3),
    }

def _build_cumplimiento_out(row: CumplimientoMensual) -> dict:
    """Serialize a CumplimientoMensual row to dict matching CumplimientoMensualOut."""
    contrato = row.contrato_ppa
    return {
        "id": row.id,
        "contrato_ppa_id": row.contrato_ppa_id,
        "proyecto_id": row.proyecto_id,
        "anio": row.anio,
        "mes": row.mes,
        "gen_total_mwh": float(row.gen_total_mwh) if row.gen_total_mwh is not None else None,
        "compromiso_mwh": float(row.compromiso_mwh) if row.compromiso_mwh is not None else None,
        "compras_bolsa_mwh": float(row.compras_bolsa_mwh) if row.compras_bolsa_mwh is not None else None,
        "excedentes_bolsa_mwh": float(row.excedentes_bolsa_mwh) if row.excedentes_bolsa_mwh is not None else None,
        "precio_bolsa_promedio": float(row.precio_bolsa_promedio) if row.precio_bolsa_promedio is not None else None,
        "compras_bolsa_cop": float(row.compras_bolsa_cop) if row.compras_bolsa_cop is not None else None,
        "excedentes_bolsa_cop": float(row.excedentes_bolsa_cop) if row.excedentes_bolsa_cop is not None else None,
        "estado": row.estado,
        "tarifa_ppa_cop_mwh": float(row.tarifa_ppa_cop_mwh) if row.tarifa_ppa_cop_mwh is not None else None,
        "valoracion_contrato_cop": float(row.valoracion_contrato_cop) if row.valoracion_contrato_cop is not None else None,
        "liquidacion_id": row.liquidacion_id,
        "contrato_nombre": contrato.nombre_interno if contrato else None,
        "comprador_nombre": contrato.comprador_nombre if contrato else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }

def _sumar_opcional(valores: list) -> float | None:
    """Suma ignorando None. Devuelve None si TODOS son None.

    Distingue "nadie tiene compromiso" (None) de "el compromiso es cero" (0.0).
    Un contrato sin compromiso cargado no debe arrastrar el consolidado a cero.
    """
    presentes = [v for v in valores if v is not None]
    return round(sum(presentes), 3) if presentes else None

def _consolidar_meses(meses_por_contrato: list[list[dict]]) -> list[dict]:
    """Suma los 12 meses de N contratos en una sola serie consolidada.

    Función pura: recibe las listas de meses ya construidas por
    `_anual_meses_para_contrato` y no hace I/O.

    Reglas (equivalentes a `loadConsolidado()` del frontend, más los casos que
    aquél no contempla):
      - min/max se suman solo entre los contratos que los tienen. Si ninguno
        tiene, el mes queda en None, no en 0.
      - El valor que se compara contra el compromiso es `valor_mwh`, que el
        backend ya resolvió por mes (real / cierre proyectado / proyección).
        Los meses en que un contrato no está vigente traen valor_mwh=None y por
        lo tanto no aportan — que es justo lo que corresponde.
      - `compras_bolsa_mwh` es el déficit DEL CONSOLIDADO (lo que dibuja la
        gráfica). `suma_compras_bolsa_mwh` es la suma de los déficits de cada
        contrato, que es el número operativo real: los contratos no se netean
        entre sí, un excedente en uno no cubre el faltante de otro.
    """
    if not meses_por_contrato:
        return []

    consolidado = []
    for i in range(12):
        fila = [c[i] for c in meses_por_contrato if i < len(c)]
        if not fila:
            continue

        min_mwh = _sumar_opcional([m.get("min_mwh") for m in fila])
        max_mwh = _sumar_opcional([m.get("max_mwh") for m in fila])
        valor   = _sumar_opcional([m.get("valor_mwh") for m in fila])
        gen     = round(sum(m.get("gen_mwh") or 0 for m in fila), 3)

        estado, compras, excedentes = "sin_compromisos", None, None
        if min_mwh is not None or max_mwh is not None:
            if valor is None:
                estado = "sin_datos"
            else:
                efectivo_min = min_mwh if min_mwh is not None else 0.0
                if valor < efectivo_min:
                    estado, compras, excedentes = "deficit", round(efectivo_min - valor, 3), 0.0
                elif max_mwh is not None and valor > max_mwh:
                    estado, compras, excedentes = "excedente", 0.0, round(valor - max_mwh, 3)
                else:
                    estado, compras, excedentes = "ok", 0.0, 0.0

        bolsa_dup = sum(m.get("exposicion_bolsa_duplicados_mwh") or 0 for m in fila)
        ref = fila[0]

        plantas = []
        for m in fila:
            for p in (m.get("plantas") or []):
                plantas.append({**p, "contrato": m.get("_contrato_label")})

        consolidado.append({
            "month": i + 1,
            "min_mwh": min_mwh,
            "max_mwh": max_mwh,
            "gen_mwh": gen,
            "gen_proyectada_mwh": _sumar_opcional([m.get("gen_proyectada_mwh") for m in fila]),
            "gen_proyectada_cierre": _sumar_opcional([m.get("gen_proyectada_cierre") for m in fila]),
            "valor_mwh": valor,
            "estado": estado,
            "tipo_datos": ref.get("tipo_datos"),
            "dia_actual": ref.get("dia_actual"),
            "dias_restantes": ref.get("dias_restantes"),
            "compras_bolsa_mwh": compras,
            "excedentes_bolsa_mwh": excedentes,
            "suma_compras_bolsa_mwh": _sumar_opcional([m.get("compras_bolsa_mwh") for m in fila]),
            "suma_excedentes_bolsa_mwh": _sumar_opcional([m.get("excedentes_bolsa_mwh") for m in fila]),
            "exposicion_bolsa_duplicados_mwh": round(bolsa_dup, 3) if bolsa_dup > 0 else None,
            "n_contratos_con_compromiso": sum(
                1 for m in fila if m.get("min_mwh") is not None or m.get("max_mwh") is not None
            ),
            "plantas": plantas,
            "n_plantas": len(plantas),
        })
    return consolidado

def _totales_tabla(meses: list[dict]) -> dict:
    """Fila de la tabla 'Resumen anual por contrato': mín/máx anual y meses con compromiso."""
    return {
        "total_min_mwh": _sumar_opcional([m.get("min_mwh") for m in meses]),
        "total_max_mwh": _sumar_opcional([m.get("max_mwh") for m in meses]),
        "meses_con_compromisos": sum(
            1 for m in meses if m.get("min_mwh") is not None or m.get("max_mwh") is not None
        ),
    }

_PANEL_CACHE: dict[str, tuple[float, dict]] = {}   # key → (monotonic_ts, payload)

PANEL_CACHE_TTL = 900   # 15 min. La generación de meses cerrados no cambia; la del

def _panel_cache_get(key: str) -> dict | None:
    entry = _PANEL_CACHE.get(key)
    if entry and (_time.monotonic() - entry[0]) < PANEL_CACHE_TTL:
        return entry[1]
    return None

def _panel_cache_set(key: str, data: dict) -> None:
    _PANEL_CACHE[key] = (_time.monotonic(), data)
