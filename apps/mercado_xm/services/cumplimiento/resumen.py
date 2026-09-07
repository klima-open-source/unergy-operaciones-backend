"""Los dos resúmenes de cumplimiento: el del mes y el de los doce meses.

Puerto de `get_resumen` y `get_anual_matriz` (`app/api/v1/cumplimiento.py`), que
eran endpoints de 277 y 88 líneas. El cuerpo vino casi verbatim: lo único que
tocaba la sesión eran los helpers, que ahora viven en `consultas.py`.

**Una planta se le pide UNA vez a la API de generación**, aunque esté en tres
contratos: `sp_set` deduplica por `sub_project` antes del fan-out. Con ~200
plantas y 12 meses, no hacerlo son miles de llamadas HTTP.
"""

from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaCompromisoEnergia

from .anual import _anual_meses_para_contrato, _build_fetch_sets
from .consultas import (
    _contratos_vigentes, _get_bolsa_avg, _query_contratos_venta, _resolve_gescon,
)
from .periodos import _responsable_payload, _rollup_cumplimiento
from .xm_api import _fetch_month, _fetch_range, _fetch_recent_avg, _unergy_token

logger = logging.getLogger("operaciones.cumplimiento")


def resumen(year: int, month: int, incluir_todos: bool = False) -> dict:
    """Cumplimiento de todos los contratos PPA en un período.

    Deduplica `sub_project` y hace UNA sola llamada a la API de generación por
    planta: una planta en tres contratos se consulta una vez, no tres.
    """
    today = hoy_col()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 1. Contratos y compromisos ────────────────────────────────────────────
    contratos = _contratos_vigentes(year, month, solo_relevantes=not incluir_todos)
    compromisos_map = {
        c.contrato_id: c
        for c in PpaCompromisoEnergia.objects.filter(año=year, mes=month)
    }

    # ── 2. GESCON por contrato ────────────────────────────────────────────────
    contrato_assignments: dict[int, list] = {}
    for c in contratos:
        if c.numero_codigo_contrato:
            contrato_assignments[c.id] = _resolve_gescon(c.numero_codigo_contrato, year, month)
        else:
            contrato_assignments[c.id] = []

    # ── 3. Sub-projects únicos ────────────────────────────────────────────────
    sp_set: set[str] = set()
    for assignments in contrato_assignments.values():
        for asic in assignments:
            if asic.proyecto and asic.proyecto.sub_project:
                sp_set.add(asic.proyecto.sub_project)

    # ── 4. Generación en paralelo (un solo fetch por planta) ──────────────────
    gen_cache: dict[str, dict] = {}
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in resumen: %s", exc)
            token = None

        if token:
            sp_list = list(sp_set)

            def _fetch_sp(sp: str) -> tuple:
                if es_mes_futuro:
                    recent = _fetch_recent_avg(token, sp)
                    avg = recent["avg_daily_mwh"]
                    mwh = round(avg * total_dias, 3) if avg is not None else None
                    return sp, {"mwh": mwh, "n_records": recent["n_days_used"], "ultimo_dia": None}
                return sp, _fetch_month(token, sp, year, month)

            max_workers = min(len(sp_list), 10)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for sp, result in pool.map(_fetch_sp, sp_list):
                    gen_cache[sp] = result

    # ── 5. Cálculo por contrato ────────────────────────────────────────────────
    contratos_result = []
    total_min = 0.0
    total_max = 0.0
    total_gen = 0.0
    total_proy = 0.0
    has_any_compromisos = False

    for c in contratos:
        assignments = contrato_assignments[c.id]
        compromiso = compromisos_map.get(c.id)

        gen_total_c = 0.0
        bolsa_dup_c = 0.0
        ur_c = 0.0
        plantas_sin_datos: list[str] = []
        dias_datos: list[int] = []
        n_duplicados = 0
        n_uso_recurso = 0

        for asic in assignments:
            proyecto = asic.proyecto
            nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
            pct = float(asic.porcentaje_despacho or 0)
            is_dup = bool(asic.es_duplicado)
            is_ur = bool(getattr(asic, "uso_del_recurso", False))
            if proyecto and proyecto.sub_project:
                gd = gen_cache.get(proyecto.sub_project, {"mwh": None, "ultimo_dia": None})
                gp = gd["mwh"]
                if gp is not None:
                    mwh_contrato = gp * pct
                    # El suministro al contrato cuenta para el cumplimiento sin importar
                    # el origen (real o compra en bolsa). El duplicado además se registra
                    # en bolsa_dup_c como sub-cifra informativa (cuánto proviene de bolsa).
                    gen_total_c += mwh_contrato
                    if is_dup:
                        bolsa_dup_c += mwh_contrato
                        n_duplicados += 1
                    if is_ur:
                        # Uso del recurso: cuenta como suministro normal del contrato;
                        # la sub-cifra estima lo que se le pagará al cliente a bolsa.
                        ur_c += mwh_contrato
                        n_uso_recurso += 1
                    if gd.get("ultimo_dia") is not None:
                        dias_datos.append(gd["ultimo_dia"])
                else:
                    plantas_sin_datos.append(nombre)
            else:
                plantas_sin_datos.append(nombre)

        gen_total_c = round(gen_total_c, 3)
        bolsa_dup_c = round(bolsa_dup_c, 3)
        ur_c = round(ur_c, 3)
        gen_proy_c = (
            round(gen_total_c * total_dias / dia_actual, 3)
            if es_mes_actual and dia_actual > 0 and gen_total_c > 0
            else gen_total_c
        )

        min_mwh: Optional[float] = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None
        max_mwh: Optional[float] = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else None

        val_b = gen_proy_c if (es_mes_actual or es_mes_futuro) else gen_total_c

        if min_mwh is not None or max_mwh is not None:
            has_any_compromisos = True
            effective_max = max_mwh if max_mwh is not None else float('inf')
            effective_min = min_mwh if min_mwh is not None else 0.0
            if val_b < effective_min:
                estado_c = "deficit"
            elif val_b > effective_max:
                estado_c = "excedente"
            else:
                estado_c = "ok"
            compras_c = round(max(0.0, effective_min - val_b), 3)
            excedentes_c = round(max(0.0, val_b - effective_max), 3) if max_mwh is not None else 0.0
            total_min += effective_min
            total_max += max_mwh if max_mwh is not None else 0.0
        else:
            estado_c = "sin_compromisos"
            compras_c = None
            excedentes_c = None

        total_gen += gen_total_c
        total_proy += gen_proy_c

        contratos_result.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "energia_minima_mwh": min_mwh,
            "energia_maxima_mwh": max_mwh,
            "gen_total_mwh": gen_total_c,
            "gen_proyectada_mwh": gen_proy_c,
            "estado": estado_c,
            "compras_bolsa_mwh": compras_c,
            "excedentes_bolsa_mwh": excedentes_c,
            "exposicion_bolsa_duplicados_mwh": bolsa_dup_c if bolsa_dup_c > 0 else None,
            "uso_recurso_mwh": ur_c if ur_c > 0 else None,
            "n_plantas_activas": len(assignments),
            "n_duplicados": n_duplicados,
            "n_uso_recurso": n_uso_recurso,
            "plantas_sin_datos": plantas_sin_datos,
            "dia_min_datos": min(dias_datos) if dias_datos else None,
            # Indicador de cumplimiento de plantas: registradas (numerador) vs esperadas (denominador).
            "plantas_registradas": len(assignments),
            "plantas_esperadas": int(compromiso.cantidad_proyectos) if compromiso and compromiso.cantidad_proyectos is not None else None,
        })

    # ── 6. Totales agregados ──────────────────────────────────────────────────
    total_gen = round(total_gen, 3)
    total_proy = round(total_proy, 3)
    val_total = total_proy if (es_mes_actual or es_mes_futuro) else total_gen

    if has_any_compromisos and (total_min > 0 or total_max > 0):
        if total_min > 0 and val_total < total_min:
            total_estado = "deficit"
        elif total_max > 0 and val_total > total_max:
            total_estado = "excedente"
        else:
            total_estado = "ok"
        total_compras = round(max(0.0, total_min - val_total), 3) if total_min > 0 else 0.0
        total_excedentes = round(max(0.0, val_total - total_max), 3) if total_max > 0 else 0.0
    else:
        total_estado = "sin_compromisos"
        total_compras = None
        total_excedentes = None

    dias_min_list = [c["dia_min_datos"] for c in contratos_result if c["dia_min_datos"] is not None]

    # ── Valoración COP con precios de bolsa ──────────────────
    bolsa = _get_bolsa_avg(year, month)
    precio_bolsa = bolsa["precio_promedio"]

    valoracion_total = None
    if precio_bolsa is not None and (total_compras or total_excedentes):
        valoracion_total = {
            "precio_bolsa_avg_cop_kwh": precio_bolsa,
            "dias_con_precios": bolsa["dias_con_datos"],
            "compras_bolsa_cop": round(total_compras * 1000 * precio_bolsa, 0) if total_compras else 0,
            "excedentes_bolsa_cop": round(total_excedentes * 1000 * precio_bolsa, 0) if total_excedentes else 0,
        }

    # Add COP to each contract row
    if precio_bolsa is not None:
        for c in contratos_result:
            if c["compras_bolsa_mwh"] is not None and c["compras_bolsa_mwh"] > 0:
                c["compras_bolsa_cop"] = round(c["compras_bolsa_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["compras_bolsa_cop"] = None
            if c["excedentes_bolsa_mwh"] is not None and c["excedentes_bolsa_mwh"] > 0:
                c["excedentes_bolsa_cop"] = round(c["excedentes_bolsa_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["excedentes_bolsa_cop"] = None
            if c.get("uso_recurso_mwh"):
                # Costo interno estimado: lo que Unergy le pagará al cliente a precio
                # bolsa (el pago real es manual en Liquidaciones).
                c["uso_recurso_cop"] = round(c["uso_recurso_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["uso_recurso_cop"] = None

    return {
        "periodo": {
            "year": year,
            "month": month,
            "dia_actual": dia_actual,
            "dias_mes": total_dias,
            "es_mes_actual": es_mes_actual,
            "es_mes_futuro": es_mes_futuro,
            "tipo_datos": "proyeccion_historica" if es_mes_futuro else ("proyeccion_lineal" if es_mes_actual else "real"),
            "dia_min_datos": min(dias_min_list) if dias_min_list else None,
            "dia_max_datos": max(dias_min_list) if dias_min_list else None,
        },
        "totales": {
            "energia_minima_mwh": round(total_min, 3) if has_any_compromisos else None,
            "energia_maxima_mwh": round(total_max, 3) if has_any_compromisos else None,
            "gen_total_mwh": total_gen,
            "gen_proyectada_mwh": total_proy,
            "estado": total_estado,
            "compras_bolsa_mwh": total_compras,
            "excedentes_bolsa_mwh": total_excedentes,
        },
        "valoracion_bolsa": valoracion_total,
        "contratos": contratos_result,
    }


def _matriz_un_contrato(contrato, year: int, today) -> dict:
    """Ensambla la fila de matriz anual de UN contrato (meses + proyectos + rollup).

    Hace los fetches a Unergy solo de las plantas de este contrato → ~2-3s, apto para
    carga progresiva fila por fila (evita el timeout del endpoint agregado con muchos contratos).
    """
    gpm = {
        m: (_resolve_gescon(contrato.numero_codigo_contrato, year, m) if contrato.numero_codigo_contrato else [])
        for m in range(1, 13)
    }
    comp_map = {
        r.mes: r for r in PpaCompromisoEnergia.objects.filter(
            contrato_id=contrato.id, año=year,
        )
    }
    need_month, need_avg, need_range = _build_fetch_sets({contrato.id: gpm}, year, today)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}
    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in _matriz_un_contrato: %s", exc)
            token = None
        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res
        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")
        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res
    meses, proyectos = _anual_meses_para_contrato(contrato, year, gpm, comp_map, month_cache, avg_cache, today, range_cache)
    rollup = _rollup_cumplimiento(meses)
    n_plantas = max((len(gpm[m]) for m in range(1, 13)), default=0)
    return {
        "id": contrato.id,
        "nombre_interno": contrato.nombre_interno,
        "numero_codigo_contrato": contrato.numero_codigo_contrato,
        "comprador_nombre": contrato.comprador_nombre,
        **_responsable_payload(contrato),
        "meses": meses,
        "proyectos": proyectos,
        "n_plantas": n_plantas,
        **rollup,
    }


def anual_matriz(year: int, incluir_todos: bool = False) -> dict:
    """Matriz anual contrato → proyectos × 12 meses (solo venta).

    Por defecto oculta los contratos de responsables no relevantes: además de
    limpiar la vista, ahorra sus llamadas a la API de Unergy.
    """
    today = hoy_col()

    # 1. Contratos de venta (mismo universo que el simulador, sin restricción de mes)
    contratos = _query_contratos_venta(year, solo_relevantes=not incluir_todos)

    # 2. GESCON por contrato/mes + compromisos por contrato
    gpm_por_contrato: dict = {}
    comp_por_contrato: dict = {}
    for c in contratos:
        gpm_por_contrato[c.id] = {
            m: (_resolve_gescon(c.numero_codigo_contrato, year, m) if c.numero_codigo_contrato else [])
            for m in range(1, 13)
        }
        comp_por_contrato[c.id] = {
            r.mes: r for r in PpaCompromisoEnergia.objects.filter(
                contrato_id=c.id, año=year,
            )
        }

    # 3. Set global de fetches deduplicado
    need_month, need_avg, need_range = _build_fetch_sets(gpm_por_contrato, year, today)

    # 4. Fetch único en paralelo (mismo patrón que get_anual)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}
    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_anual_matriz: %s", exc)
            token = None

        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res  # key = (m, sp) — misma orientación que get_anual

        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")

        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res

    # 5. Ensamblar por contrato
    out = []
    for c in contratos:
        meses, proyectos = _anual_meses_para_contrato(
            c, year, gpm_por_contrato[c.id], comp_por_contrato[c.id],
            month_cache, avg_cache, today, range_cache,
        )
        rollup = _rollup_cumplimiento(meses)
        n_plantas = max((len(gpm_por_contrato[c.id][m]) for m in range(1, 13)), default=0)
        out.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "meses": meses,
            "proyectos": proyectos,
            "n_plantas": n_plantas,
            **rollup,
        })
    return {"year": year, "contratos": out}
