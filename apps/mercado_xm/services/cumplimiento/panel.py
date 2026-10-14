"""El panel anual: todo lo que dibuja la pestaña Cumplimiento, en una llamada.

Puerto de `get_panel_anual` (134 líneas).

**El valor que se compara contra el compromiso ya viene resuelto en `valor_mwh`.**
Es la razón de existir del endpoint: un panel externo no reimplementa las reglas
(mes cerrado = real, mes en curso = proyección) y por tanto sus números no pueden
divergir de los de la plataforma.

Cacheado 15 minutos EN MEMORIA DEL PROCESO. Con `WORKERS=1` (ver CLAUDE.md) eso es
una sola caché; si algún día se sube el número de workers, cada uno tendrá la suya
y habrá que sacarla a un caché compartido.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaCompromisoEnergia

from .anual import _anual_meses_para_contrato, _build_fetch_sets
from .consultas import _query_contratos_venta, _resolve_gescon
from .periodos import (
    _consolidar_meses, _panel_cache_get, _panel_cache_set,
    _rollup_cumplimiento, _totales_tabla,
)
from .xm_api import _fetch_month, _fetch_range, _fetch_recent_avg, _unergy_token

logger = logging.getLogger("operaciones.cumplimiento")


def panel_anual(year: int, incluir_plantas: bool = True, refrescar: bool = False) -> dict:
    """Todo lo que dibuja la pestaña Cumplimiento de /mem/cumplimiento, en una llamada.

    Devuelve, para el año pedido:
      - `consolidado`: los 12 meses con todos los contratos de venta sumados.
      - `contratos[]`: cada contrato con sus 12 meses y los totales de la tabla resumen.

    Pensado para paneles externos: el valor que se compara contra el compromiso ya
    viene resuelto en `valor_mwh`, así que el consumidor no reimplementa reglas de
    negocio y sus números no pueden divergir de los de la plataforma.

    Cacheado 15 minutos en memoria (`?refrescar=true` para saltarla).
    """
    cache_key = f"panel-anual:{year}:{int(incluir_plantas)}"
    if not refrescar:
        cached = _panel_cache_get(cache_key)
        if cached is not None:
            return {**cached, "desde_cache": True}

    today = hoy_col()
    contratos = _query_contratos_venta(year)

    # GESCON por contrato/mes + compromisos, igual que get_anual_matriz.
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

    # Un solo set deduplicado de fetches para TODOS los contratos: una planta que
    # despacha a tres contratos se consulta una vez, no tres.
    need_month, need_avg, need_range = _build_fetch_sets(gpm_por_contrato, year, today)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}

    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_panel_anual: %s", exc)
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

    out_contratos = []
    meses_por_contrato = []
    for c in contratos:
        meses, _proyectos = _anual_meses_para_contrato(
            c, year, gpm_por_contrato[c.id], comp_por_contrato[c.id],
            month_cache, avg_cache, today, range_cache,
        )
        etiqueta = c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}"
        # `_contrato_label` lo consume _consolidar_meses para etiquetar cada planta
        # con el contrato al que aporta; no se expone en la respuesta.
        for m in meses:
            m["_contrato_label"] = etiqueta
        meses_por_contrato.append(meses)

        limpios = []
        for m in meses:
            fila = {k: v for k, v in m.items() if k != "_contrato_label"}
            if not incluir_plantas:
                fila.pop("plantas", None)
            limpios.append(fila)

        out_contratos.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            **_totales_tabla(meses),
            **_rollup_cumplimiento(meses),
            "meses": limpios,
        })

    consolidado_meses = _consolidar_meses(meses_por_contrato)
    if not incluir_plantas:
        for m in consolidado_meses:
            m.pop("plantas", None)

    payload = {
        "year": year,
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "consolidado": {
            "nombre": "Consolidado (todos)",
            "n_contratos": len(out_contratos),
            **_totales_tabla(consolidado_meses),
            "meses": consolidado_meses,
        },
        "contratos": out_contratos,
    }
    _panel_cache_set(cache_key, payload)
    return {**payload, "desde_cache": False}
