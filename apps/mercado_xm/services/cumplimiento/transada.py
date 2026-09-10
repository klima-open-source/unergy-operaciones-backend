"""Energía transada por planta: cuánta fue a PPA y cuánta quedó en bolsa.

Puerto de `get_energia_transada` (233 líneas). **Solo datos reales**, nunca
proyección: un mes futuro devuelve listas vacías en vez de una estimación que se
leería como medición.

El reparto es por PORCENTAJE de despacho prorrateado por días activos. Un
registro con vigencia PARCIAL del período (relevo, arranque o terminación a mitad
de mes) se pide a la API como rango y se suma día a día — no se prorratea el
total del período, que daría un número plausible y falso.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.db.models import Q

from apps.energia.services.comercializacion import identificador_monitoreo as _mon_id
from apps.plataforma.services.fechas import hoy_col

from .consultas import _contratos_vigentes, _resolve_gescon
from .periodos import _gen_vigencia_mwh, _vigencia_window
from .piscinas import _plantas_del_mes
from .xm_api import _fetch_month, _fetch_range, _unergy_token

logger = logging.getLogger("operaciones.cumplimiento")


def energia_transada(year: int, month: int, incluir_todos: bool = False) -> dict:
    """Energía transada por planta en el mes — SOLO datos reales, sin proyección.

    Para cada planta representada: la generación del período (mes cerrado
    completo, mes en curso hasta hoy), cuánta se transó vía PPA (asignación GESCON
    × % de despacho, prorrateado por días activos dentro del período) y cuánta
    quedó en bolsa (el remanente sin asignación). Las asignaciones duplicadas
    (exposición a bolsa) NO cuentan como PPA.

    Dos consultas a la base y un solo fetch por planta, en paralelo.
    """
    from apps.proyectos.models import Proyecto

    today = hoy_col()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_corte = today.day if es_mes_actual else total_dias
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)
    corte = date(year, month, dia_corte)

    periodo = {
        "year": year,
        "month": month,
        "dias_mes": total_dias,
        "dia_corte": dia_corte,
        "fecha_corte": corte.isoformat(),
        "es_mes_actual": es_mes_actual,
        "es_mes_futuro": es_mes_futuro,
    }

    if es_mes_futuro:
        return {
            "periodo": periodo,
            "plantas": [],
            "totales": {"gen_mwh": 0.0, "ppa_mwh": 0.0, "bolsa_mwh": 0.0, "n_plantas": 0},
        }

    # ── 1. Plantas representadas activas en el período (1 query) ──────────────
    # Mismo universo que `/plantas-contratos` más el corte por fecha de entrada
    # en operación, que solo pide esta vista.
    plantas_db = [
        p for p in _plantas_del_mes(first_day)
        if p.fecha_entrada_operacion is None or p.fecha_entrada_operacion <= last_day
    ]
    plantas_by_id = {p.id: p for p in plantas_db}

    # ── 2. Asignaciones GESCON de contratos de venta vigentes ─────────────────
    contratos_venta = [
        c for c in _contratos_vigentes(year, month, solo_relevantes=not incluir_todos)
        if (getattr(c, "tipo_contrato", None) or "venta") != "compra"
    ]
    asignaciones: dict[int, list[dict]] = defaultdict(list)
    for c in contratos_venta:
        if not c.numero_codigo_contrato:
            continue
        nombre_c = c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}"
        for asic in _resolve_gescon(c.numero_codigo_contrato, year, month):
            if not asic.proyecto_id:
                continue
            # Ventana efectiva dentro del período [primer día .. corte]
            eff_start, eff_end = _vigencia_window(asic.fecha_inicio, asic.fecha_fin, first_day, corte)
            dias_activos = max(0, (eff_end - eff_start).days + 1)
            if dias_activos == 0:
                continue
            asignaciones[asic.proyecto_id].append({
                "contrato_id": c.id,
                "contrato": nombre_c,
                "pct": float(asic.porcentaje_despacho or 0),
                "dias_activos": dias_activos,
                "eff_start": eff_start,
                "eff_end": eff_end,
                "es_duplicado": bool(asic.es_duplicado),
            })

    # Plantas con GESCON que no entraron en el filtro inicial (1 query extra solo si hace falta)
    missing_ids = set(asignaciones) - set(plantas_by_id)
    if missing_ids:
        extra = Proyecto.objects.filter(id__in=missing_ids).filter(
            Q(fecha_inicio_comercializacion__isnull=False) | Q(sub_project__isnull=False)
        )
        for p in extra:
            plantas_by_id[p.id] = p
        plantas_db = sorted(plantas_by_id.values(), key=lambda p: p.nombre_comercial or "")

    # ── 3. Generación en paralelo (un fetch por sub_project único) ────────────
    # need_range: registros con vigencia PARCIAL del período (relevo/arranque/
    # terminación intra-mes) — su energía se suma día a día (misma fuente que la
    # Matriz y Generación solar), no se prorratea el total del período.
    need_range: set = set()
    for pid, asigs in asignaciones.items():
        sp_p = _mon_id(plantas_by_id[pid]) if pid in plantas_by_id else None
        if not sp_p:
            continue
        for a in asigs:
            if 0 < a["dias_activos"] < dia_corte:
                need_range.add((sp_p, a["eff_start"], a["eff_end"]))

    sp_set = {_mon_id(p) for p in plantas_db if _mon_id(p)}
    gen_cache: dict[str, dict] = {}
    range_cache: dict = {}
    warning = None
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in energia-transada: %s", exc)
            token = None
            warning = "No se pudo autenticar con la API de generación."
        if token:
            sp_list = list(sp_set)

            def _fetch_sp(sp: str) -> tuple:
                return sp, _fetch_month(token, sp, year, month)

            with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                for sp, res in pool.map(_fetch_sp, sp_list):
                    gen_cache[sp] = res

            if need_range:
                def _fetch_rg(task: tuple) -> tuple:
                    sp, start, end = task
                    return task, _fetch_range(token, sp, start, end)

                with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                    for task, res in pool.map(_fetch_rg, list(need_range)):
                        range_cache[task] = res

    # ── 4. Cálculo por planta ─────────────────────────────────────────────────
    plantas_out = []
    total_gen = total_ppa = total_bolsa = 0.0
    for p in plantas_db:
        gd = gen_cache.get(_mon_id(p), {"mwh": None, "ultimo_dia": None})
        gen = gd.get("mwh")
        asigs = asignaciones.get(p.id, [])
        contratos_planta = [
            {
                "id": a["contrato_id"],
                "nombre": a["contrato"],
                "pct": a["pct"],
                "dias_activos": a["dias_activos"],
                "es_duplicado": a["es_duplicado"],
            }
            for a in asigs
        ]
        if gen is None:
            plantas_out.append({
                "id": p.id,
                "nombre": p.nombre_comercial,
                "sub_project": p.sub_project,
                "potencia_kwp": float(p.potencia_ac_kw) if p.potencia_ac_kw else None,
                "gen_mwh": None, "ppa_mwh": None, "bolsa_mwh": None,
                "modo": "sin_datos", "contratos": contratos_planta, "ultimo_dia": None,
            })
            continue

        # Energía PPA = Σ (generación REAL de la vigencia de cada asignación ×
        # % despacho). Vigencia mes completo → gen del período; parcial → suma
        # real de esos días (helper compartido con Estrategia/Matriz), nunca
        # regla de tres sobre el total del período.
        ppa = 0.0
        for a in asigs:
            if a["es_duplicado"]:
                continue
            range_gen = range_cache.get((_mon_id(p), a["eff_start"], a["eff_end"]), {}).get("mwh")
            gv = _gen_vigencia_mwh(a["eff_start"], a["eff_end"], dia_corte, gen, range_gen)
            if gv is not None:
                ppa += gv * a["pct"]
        ppa = round(min(ppa, gen), 3)
        bolsa = round(max(0.0, gen - ppa), 3)
        if not any(not a["es_duplicado"] for a in asigs) or ppa <= 0:
            modo = "bolsa"
        elif bolsa <= max(0.001, gen * 0.005):
            modo, bolsa = "ppa", 0.0
        else:
            modo = "mixto"

        total_gen += gen
        total_ppa += ppa
        total_bolsa += bolsa
        plantas_out.append({
            "id": p.id,
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "potencia_kwp": float(p.potencia_ac_kw) if p.potencia_ac_kw else None,
            "gen_mwh": round(gen, 3),
            "ppa_mwh": ppa,
            "bolsa_mwh": bolsa,
            "modo": modo,
            "contratos": contratos_planta,
            "ultimo_dia": gd.get("ultimo_dia"),
        })

    plantas_out.sort(key=lambda x: -(x["gen_mwh"] or 0))

    result = {
        "periodo": periodo,
        "plantas": plantas_out,
        "totales": {
            "gen_mwh": round(total_gen, 3),
            "ppa_mwh": round(total_ppa, 3),
            "bolsa_mwh": round(total_bolsa, 3),
            "n_plantas": len([x for x in plantas_out if x["gen_mwh"] is not None]),
            "n_sin_datos": len([x for x in plantas_out if x["gen_mwh"] is None]),
        },
    }
    if warning:
        result["warning"] = warning
    return result
