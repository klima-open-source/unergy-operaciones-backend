"""El simulador: plantas, generación promedio y asignaciones GESCON del mes.

Puerto de `get_simulador` (297 líneas). Alimenta la pantalla donde el usuario
mueve plantas entre contratos y ve el efecto en el cumplimiento. **No escribe
nada**: la simulación vive en el frontend.

Una asignación primaria ADICIONAL (despacho partido: 50% Terpel 1 + 50% Terpel 2)
se emite como fila propia, no sobrescribe. Con el dict de antes solo sobrevivía el
último contrato y el 50% del primero desaparecía de la vista.
"""

from __future__ import annotations

import calendar
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Optional

from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaCompromisoEnergia

from .consultas import (
    _clasificar_remanente_bolsa, _contratos_vigentes, _query_contratos_venta,
    _resolve_gescon,
)
from .periodos import _gen_vigencia_mwh, _responsable_payload, _vigencia_window
from .piscinas import _plantas_del_mes, _proyectos_por_contrato_ppa
from .xm_api import (
    _fetch_month,
    _fetch_range,
    _fetch_recent_avg,
    _unergy_token,
)
# Alias historico: en el arbol de FastAPI se importaba asi
# (app/api/v1/cumplimiento.py:28) y el port conservo los usos, no el import.
from apps.energia.services.comercializacion import (
    identificador_monitoreo as _mon_id,
)

logger = logging.getLogger("operaciones.cumplimiento")


def simulador(year: int, month: int, incluir_todos: bool = False) -> dict:
    """Plantas con su generación promedio + las asignaciones GESCON del mes.

    Alimenta el simulador: el usuario mueve plantas entre contratos y ve el efecto
    en el cumplimiento sin escribir nada. Es solo lectura.
    """
    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)

    # Mismo universo de plantas que `/plantas-contratos`: ver `_plantas_del_mes`.
    plantas_db = _plantas_del_mes(first_day)

    contratos_db = _contratos_vigentes(year, month, solo_relevantes=not incluir_todos)

    contratos_venta = _query_contratos_venta(year, month, solo_relevantes=not incluir_todos)
    contratos_compra = [c for c in contratos_db if (c.tipo_contrato or "venta") == "compra"]

    last_day = date(year, month, total_dias)
    compra_proyecto_ids: set[int] = set()
    compra_nombre_map: dict[int, str] = {}
    en_ventana = [
        cc for cc in contratos_compra
        if not (cc.fecha_fin and cc.fecha_fin < first_day)
        and not (cc.fecha_inicio and cc.fecha_inicio > last_day)
    ]
    # Una consulta para todos, en vez de una por contrato dentro del bucle.
    proyectos_por_compra = _proyectos_por_contrato_ppa([cc.id for cc in en_ventana])
    for cc in en_ventana:
        for proy in proyectos_por_compra.get(cc.id, []):
            compra_proyecto_ids.add(proy.id)
            compra_nombre_map[proy.id] = cc.nombre_interno or cc.numero_codigo_contrato or f"Compra {cc.id}"

    proyecto_primary: dict[int, dict] = {}
    proyecto_dups: list[dict] = []
    assigned_ids: set[int] = set()
    for c in contratos_venta:
        if not c.numero_codigo_contrato:
            continue
        for asic in _resolve_gescon(c.numero_codigo_contrato, year, month):
            if not asic.proyecto_id:
                continue
            entry = {
                "contrato_id": c.id,
                "pct_despacho": float(asic.porcentaje_despacho or 0),
                "es_duplicado": bool(asic.es_duplicado),
                "uso_del_recurso": bool(getattr(asic, "uso_del_recurso", False)),
                "proyecto_id": asic.proyecto_id,
                "fecha_inicio": asic.fecha_inicio,
                "fecha_fin": asic.fecha_fin,
            }
            if asic.es_duplicado or asic.proyecto_id in proyecto_primary:
                # Duplicado, O una asignación primaria ADICIONAL de una planta que ya
                # tiene primaria en otro contrato (despacho partido, p.ej. 50% Terpel 1
                # + 50% Terpel 2). Antes el dict proyecto_primary sobrescribía y solo
                # sobrevivía el último contrato → el 50% del primero desaparecía de la
                # vista. Ahora cada asignación extra se emite como fila propia.
                proyecto_dups.append(entry)
            else:
                proyecto_primary[asic.proyecto_id] = entry
            assigned_ids.add(c.id)

    comp_map = {
        r.contrato_id: r
        for r in PpaCompromisoEnergia.objects.filter(año=year, mes=month)
    }

    today = hoy_col()
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    es_mes_actual = (year == today.year and month == today.month)
    dia_actual = today.day if es_mes_actual else total_dias
    dias_restantes = (total_dias - dia_actual) if es_mes_actual else 0

    sp_list = [_mon_id(p) for p in plantas_db if _mon_id(p)]
    gen_cache: dict[str, float | None] = {}
    avg_cache_sim: dict[str, float | None] = {}
    gen_warning: str | None = None
    if sp_list:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in simulador: %s", exc)
            gen_warning = "No se pudo autenticar con la API de generación. Datos de generación no disponibles."
            token = None

        if token:
            if es_mes_futuro:
                def _fa(sp: str):
                    res = _fetch_recent_avg(token, sp, n_days=30)
                    avg = res.get("avg_daily_mwh")
                    return sp, round(avg * total_dias, 3) if avg is not None else None

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, mwh in pool.map(_fa, sp_list):
                        gen_cache[sp] = mwh
            elif es_mes_actual:
                def _fm(sp: str):
                    return sp, _fetch_month(token, sp, year, month), _fetch_recent_avg(token, sp, n_days=30)

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, month_res, avg_res in pool.map(_fm, sp_list):
                        gen_cache[sp] = month_res.get("mwh")
                        avg_cache_sim[sp] = avg_res.get("avg_daily_mwh")
            else:
                def _fp(sp: str):
                    res = _fetch_month(token, sp, year, month)
                    return sp, res.get("mwh")

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, mwh in pool.map(_fp, sp_list):
                        gen_cache[sp] = mwh

    plantas_by_id = {p.id: p for p in plantas_db}

    # Rangos reales para asignaciones con vigencia PARCIAL del período (relevo/
    # arranque/terminación intra-mes). Misma fuente que Matriz y Generación solar:
    # se suma la generación real de esos días, no se prorratea el total del mes.
    # (Solo meses pasado/actual; el futuro se proyecta con avg × días.)
    dias_periodo_real = dia_actual  # = total_dias para mes pasado; = hoy para mes actual
    period_end_real = date(year, month, dia_actual)

    range_cache_sim: dict = {}
    if not es_mes_futuro:
        _sp_by_pid = {pid: (_mon_id(plantas_by_id[pid]) if pid in plantas_by_id else None)
                      for pid in set(proyecto_primary) | {d["proyecto_id"] for d in proyecto_dups}}
        need_range_sim: set = set()
        for entry in list(proyecto_primary.values()) + proyecto_dups:
            sp_e = _sp_by_pid.get(entry["proyecto_id"])
            if not sp_e:
                continue
            eff_start, eff_end = _vigencia_window(entry["fecha_inicio"], entry["fecha_fin"], first_day, period_end_real)
            dias_act = max(0, (eff_end - eff_start).days + 1)
            if 0 < dias_act < dias_periodo_real:
                need_range_sim.add((sp_e, eff_start, eff_end))
        if need_range_sim:
            try:
                tok = token  # reutiliza el token ya obtenido arriba
            except NameError:
                tok = None
            if tok:
                def _fr(task: tuple) -> tuple:
                    sp, s, e = task
                    return task, _fetch_range(tok, sp, s, e)
                with ThreadPoolExecutor(max_workers=min(len(need_range_sim), 12)) as pool:
                    for task, res in pool.map(_fr, list(need_range_sim)):
                        range_cache_sim[task] = res

    def _scoped_gen(sp, fecha_inicio, fecha_fin):
        """(month_mwh, month_mwh_proyectado) de una asignación escalada a SU
        vigencia dentro del mes — energía real de la vigencia, no el total del
        mes. Misma lógica que la Matriz para que Estrategia coincida."""
        if not sp:
            return None, None
        full = gen_cache.get(sp)
        if es_mes_futuro:
            # Proyección plana: avg_daily × días vigentes (= total_proyectado × proración).
            eff_start, eff_end = _vigencia_window(fecha_inicio, fecha_fin, first_day, last_day)
            dias_act = max(0, (eff_end - eff_start).days + 1)
            if full is None or dias_act <= 0:
                return (None, None) if full is None else (0.0, 0.0)
            val = round(full * dias_act / total_dias, 3)
            return val, val
        # Mes pasado/actual: parte REAL de la vigencia hasta el corte.
        eff_start, eff_end = _vigencia_window(fecha_inicio, fecha_fin, first_day, period_end_real)
        range_gen = range_cache_sim.get((sp, eff_start, eff_end), {}).get("mwh")
        real = _gen_vigencia_mwh(eff_start, eff_end, dias_periodo_real, full, range_gen)
        if not es_mes_actual:
            return real, real
        # Mes actual: proyecta los días de vigencia que aún faltan (después de hoy).
        avg = avg_cache_sim.get(sp)
        eff_end_full = _vigencia_window(fecha_inicio, fecha_fin, first_day, last_day)[1]
        dias_proj = max(0, (eff_end_full - period_end_real).days)
        if real is None:
            return None, None
        proy = round(real + (avg or 0) * dias_proj, 3) if avg is not None else real
        return real, proy

    plantas_out = []
    for p in plantas_db:
        asn = proyecto_primary.get(p.id)
        if asn:
            # Energía escalada a la vigencia de ESTA asignación (clave del fix:
            # una planta que solo estuvo parte del mes en el contrato refleja la
            # generación real de esos días, no el total del mes).
            row_mwh, row_proy = _scoped_gen(_mon_id(p), asn["fecha_inicio"], asn["fecha_fin"])
        else:
            # Planta sin contrato PPA de venta (remanente/bolsa): generación del
            # mes completo, sin escalar.
            _mid = _mon_id(p)
            row_mwh = gen_cache.get(_mid)
            row_proy = (
                round((gen_cache.get(_mid) or 0) + (avg_cache_sim.get(_mid) or 0) * dias_restantes, 3)
                if es_mes_actual and avg_cache_sim.get(_mid) is not None
                else gen_cache.get(_mid)
            )
        plantas_out.append({
            "id": p.id,
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "tipo_proyecto": p.tipo_proyecto,
            "potencia_kwp": float(p.potencia_ac_kw) if p.potencia_ac_kw else None,
            "month_mwh": row_mwh,
            "month_mwh_proyectado": row_proy,
            "contrato_id": asn["contrato_id"] if asn else None,
            "pct_despacho": asn["pct_despacho"] if asn else 1.0,
            "es_duplicado": False,
            "uso_del_recurso": asn["uso_del_recurso"] if asn else False,
            "comprado_por_unergy": p.id in compra_proyecto_ids,
            "contrato_compra_nombre": compra_nombre_map.get(p.id),
            # Subdivisión del remanente (mismo criterio que /plantas-contratos): solo para
            # plantas sin contrato PPA de venta. "comercializador" (UNGC) | "libre" | None.
            "piscina_bolsa": (
                None if asn else _clasificar_remanente_bolsa(p.id, first_day, last_day)[0]
            ),
        })

    for dup in proyecto_dups:
        p = plantas_by_id.get(dup["proyecto_id"])
        if not p:
            continue
        # Escalada a la vigencia de esta asignación (igual que la fila primaria).
        dup_mwh, dup_proy = _scoped_gen(_mon_id(p), dup["fecha_inicio"], dup["fecha_fin"])
        plantas_out.append({
            "id": f"{p.id}_dup_{dup['contrato_id']}",
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "tipo_proyecto": p.tipo_proyecto,
            "potencia_kwp": float(p.potencia_ac_kw) if p.potencia_ac_kw else None,
            "month_mwh": dup_mwh,
            "month_mwh_proyectado": dup_proy,
            "contrato_id": dup["contrato_id"],
            "pct_despacho": dup["pct_despacho"],
            # es_duplicado real del registro: True para duplicado (compra en bolsa),
            # False para una segunda asignación primaria (despacho partido entre contratos).
            "es_duplicado": dup["es_duplicado"],
            "uso_del_recurso": dup["uso_del_recurso"],
            # Misma lógica que la fila primaria: si Unergy compra la planta vía un
            # contrato de compra, la etiqueta es "comprado por Unergy" aunque la fila
            # sea duplicado de un contrato de venta (ej. GD Astrolumen La Garita en
            # Terpel 4). Antes estaba hardcodeado a False/None y salía como compra en bolsa.
            "comprado_por_unergy": p.id in compra_proyecto_ids,
            "contrato_compra_nombre": compra_nombre_map.get(p.id),
            "piscina_bolsa": None,  # los duplicados pertenecen a un contrato, no al remanente
        })

    contratos_out = []
    for c in contratos_venta:
        comp = comp_map.get(c.id)
        if comp is None and c.id not in assigned_ids:
            continue
        contratos_out.append({
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "min_mwh": float(comp.energia_minima) if comp and comp.energia_minima is not None else None,
            "max_mwh": float(comp.energia_maxima) if comp and comp.energia_maxima is not None else None,
            # Plantas esperadas para el mes (denominador del indicador de cumplimiento de plantas).
            # El numerador (plantas registradas) lo calcula el frontend con las plantas asignadas.
            "plantas_esperadas": int(comp.cantidad_proyectos) if comp and comp.cantidad_proyectos is not None else None,
        })

    result = {
        "year": year, "month": month, "dias_mes": total_dias,
        "es_mes_futuro": es_mes_futuro, "es_mes_actual": es_mes_actual,
        "dia_actual": dia_actual, "dias_restantes": dias_restantes,
        "plantas": plantas_out, "contratos": contratos_out,
    }
    if gen_warning:
        result["warning"] = gen_warning
    return result
