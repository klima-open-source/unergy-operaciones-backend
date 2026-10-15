"""La matriz anual y el detalle por contrato.

Puerto de `/anual-matriz/contratos`, `/anual-matriz/contrato/{id}`,
`/ppa/{id}/anual` y `/ppa/{id}/plantas-inscritas-por-mes`.

**La matriz se carga en dos pasos a propósito.** `/anual-matriz` completo hacía
los fetches de todos los contratos en una sola petición y se caía por timeout con
muchos contratos; el frontend pide primero la lista ligera (sin generación) y
después una fila a la vez. Los dos caminos comparten `_matriz_un_contrato`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from rest_framework.exceptions import NotFound

from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaCompromisoEnergia, PpaContrato

from .anual import _anual_meses_para_contrato, _build_fetch_sets
from .consultas import _query_contratos_venta, _resolve_gescon
from .resumen import _matriz_un_contrato
from .xm_api import _fetch_month, _fetch_range, _fetch_recent_avg, _unergy_token

logger = logging.getLogger("operaciones.cumplimiento")


def anual_matriz_contratos(year: int) -> dict:
    """Lista ligera de contratos de venta para la matriz anual, SIN generación.

    Carga instantánea: el frontend pinta las filas y después pide el detalle de
    cada una a `/anual-matriz/contrato/{id}`. Es lo que evita el timeout del
    endpoint agregado cuando hay muchos contratos.
    """
    contratos = _query_contratos_venta(year)
    return {
        "year": year,
        "contratos": [
            {
                "id": c.id,
                "nombre_interno": c.nombre_interno,
                "numero_codigo_contrato": c.numero_codigo_contrato,
                "comprador_nombre": c.comprador_nombre,
            }
            for c in contratos
        ],
    }


def anual_matriz_contrato(contrato_id: int, year: int) -> dict:
    """Matriz anual de UN contrato (meses + proyectos + rollup), para carga progresiva."""
    contrato = PpaContrato.objects.filter(pk=contrato_id).first()
    if not contrato:
        raise NotFound("Contrato PPA no encontrado")
    return _matriz_un_contrato(contrato, year, hoy_col())


def anual_de_contrato(contrato_id: int, year: int) -> dict:
    """Los 12 meses de un contrato: generación contra compromisos."""
    today = hoy_col()

    contrato = PpaContrato.objects.filter(pk=contrato_id).first()
    if not contrato:
        raise NotFound("Contrato PPA no encontrado")

    comp_map = {
        r.mes: r
        for r in PpaCompromisoEnergia.objects.filter(contrato_id=contrato_id, año=year)
    }

    gescon_per_month: dict = {}
    for m in range(1, 13):
        gescon_per_month[m] = (
            _resolve_gescon(contrato.numero_codigo_contrato, year, m)
            if contrato.numero_codigo_contrato else []
        )

    need_month, need_avg, need_range = _build_fetch_sets({contrato.id: gescon_per_month}, year, today)

    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}

    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_anual: %s", exc)
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

    meses, _proyectos = _anual_meses_para_contrato(
        contrato, year, gescon_per_month, comp_map, month_cache, avg_cache, today, range_cache
    )

    return {
        "contrato": {
            "id": contrato.id,
            "nombre_interno": contrato.nombre_interno,
            "numero_codigo_contrato": contrato.numero_codigo_contrato,
            "comprador_nombre": contrato.comprador_nombre,
        },
        "year": year,
        "meses": meses,
    }


def plantas_inscritas_por_mes(contrato_id: int) -> list[dict]:
    """Plantas INSCRITAS por año/mes = plantas registradas y despachando energía al
    contrato (asignaciones GESCON vigentes ese mes). Es el numerador del indicador de
    cumplimiento de plantas; la plataforma lo calcula (no se monta).

    Devuelve solo los periodos que tienen compromiso del contrato. Cuenta asignaciones
    GESCON desde BD (`_resolve_gescon`) sin traer generación de la API → barato.
    """
    contrato = PpaContrato.objects.filter(pk=contrato_id).first()
    if not contrato:
        raise NotFound("Contrato PPA no encontrado")

    periodos = (
        PpaCompromisoEnergia.objects
        .filter(contrato_id=contrato_id)
        .order_by("año", "mes")
        .values_list("año", "mes")
    )
    codigo = contrato.numero_codigo_contrato
    out = []
    for año, mes in periodos:
        n = len(_resolve_gescon(codigo, año, mes)) if codigo else 0
        out.append({"año": año, "mes": mes, "plantas_inscritas": n})
    return out
