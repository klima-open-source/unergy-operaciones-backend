"""El cierre mensual y su histórico: lo único de Cumplimiento que ESCRIBE.

Puerto de `/cerrar-periodo`, `/historico`, `/historico/{id}` y
`/historico/{id}/facturar`.

**El cierre cuenta todos los contratos PPA vigentes.** Persiste el snapshot
mensual: dejar contratos fuera cambiaría el histórico guardado, no solo lo que se
ve en pantalla.

**Un registro facturado es inmutable desde acá.** Reejecutar el cierre de un mes
ya facturado devuelve el registro tal cual, sin tocarlo.
"""

from __future__ import annotations

import calendar
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from api.exceptions import ServicioNoDisponible

from apps.mercado_xm.models import CumplimientoMensual
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaCompromisoEnergia, PpaTarifa

from .consultas import _contratos_vigentes, _get_bolsa_avg, _resolve_gescon
from .periodos import _build_cumplimiento_out
from .xm_api import _fetch_month, _fetch_recent_avg, _unergy_token

# Los campos que el cierre recalcula, en un solo lugar. El `bulk_update`
# los tomaba de la ultima vuelta del bucle (`list(campos)`): funcionaba de
# casualidad porque todas las vueltas arman las mismas claves, y se caia si
# alguna dejaba de armarlas.
_CAMPOS_CIERRE = (
    "gen_total_mwh", "compromiso_mwh", "compras_bolsa_mwh",
    "excedentes_bolsa_mwh", "precio_bolsa_promedio", "compras_bolsa_cop",
    "excedentes_bolsa_cop", "tarifa_ppa_cop_mwh", "valoracion_contrato_cop",
    "estado",
)

logger = logging.getLogger("operaciones.cumplimiento")


def cerrar_periodo(anio: int, mes: int) -> dict:
    """Cierra un período para todos los contratos PPA activos.

    Calcula el cumplimiento con la misma lógica que `/ppa/resumen` (generación
    real desde la API de Unergy + compromisos de la base) y persiste un snapshot
    en `cumplimiento_mensual`. Es un upsert: reejecutar el cierre de un mes
    actualiza sus registros.

    **Un registro ya FACTURADO no se pisa**: se devuelve tal cual. Cerrar de nuevo
    un mes facturado no puede cambiar lo que ya se cobró.
    """
    year, month = anio, mes
    today = hoy_col()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 1. Contratos y compromisos ────────────────────────────────────────────
    # Todos los vigentes: esto PERSISTE el cierre mensual, y dejar contratos fuera
    # cambiaría el histórico guardado.
    contratos = _contratos_vigentes(year, month)
    if not contratos:
        raise NotFound("No hay contratos PPA registrados")

    compromisos_map = {
        c.contrato_id: c
        for c in PpaCompromisoEnergia.objects.filter(año=year, mes=month)
    }

    tarifas_map = {
        t.contrato_id: float(t.tarifa)
        for t in PpaTarifa.objects.filter(año=year, mes=month)
        if t.tarifa is not None
    }

    # ── 2. GESCON assignments ─────────────────────────────────────────────────
    contrato_assignments: dict[int, list] = {}
    for c in contratos:
        if c.numero_codigo_contrato:
            contrato_assignments[c.id] = _resolve_gescon(c.numero_codigo_contrato, year, month)
        else:
            contrato_assignments[c.id] = []

    # ── 3. Sub-projects unicos ────────────────────────────────────────────────
    sp_set: set[str] = set()
    for assignments in contrato_assignments.values():
        for asic in assignments:
            if asic.proyecto and asic.proyecto.sub_project:
                sp_set.add(asic.proyecto.sub_project)

    # ── 4. Generacion en paralelo ─────────────────────────────────────────────
    gen_cache: dict[str, dict] = {}
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in cerrar_periodo: %s", exc)
            raise ServicioNoDisponible("No se pudo autenticar con la API de Unergy")

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

    # ── 5. Precios de bolsa ───────────────────────────────────────────────────
    bolsa = _get_bolsa_avg(year, month)
    precio_bolsa = bolsa["precio_promedio"]

    # ── 6. Calculo y persistencia por contrato ────────────────────────────────
    registros: list[dict] = []
    # Se acumulan durante el bucle y se escriben en un solo bulk al final.
    # Faltaba inicializarlos: cerrar cualquier mes con al menos un contrato
    # moria con NameError antes de guardar nada.
    a_actualizar: list[CumplimientoMensual] = []
    a_crear: list[CumplimientoMensual] = []
    n_deficit = 0
    n_cumplidos = 0

    for c in contratos:
        assignments = contrato_assignments[c.id]
        compromiso = compromisos_map.get(c.id)

        gen_total_c = 0.0
        for asic in assignments:
            proyecto = asic.proyecto
            pct = float(asic.porcentaje_despacho or 0)
            if proyecto and proyecto.sub_project:
                gd = gen_cache.get(proyecto.sub_project, {"mwh": None})
                gp = gd.get("mwh")
                if gp is not None:
                    gen_total_c += gp * pct

        gen_total_c = round(gen_total_c, 3)

        # Linear projection for current month
        gen_val = gen_total_c
        if es_mes_actual and dia_actual > 0 and gen_total_c > 0:
            gen_val = round(gen_total_c * total_dias / dia_actual, 3)

        min_mwh = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None

        compras_mwh = None
        excedentes_mwh = None
        compras_cop = None
        excedentes_cop = None

        if min_mwh is not None:
            compras_mwh = round(max(0.0, min_mwh - gen_val), 3)
            max_mwh_val = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else min_mwh
            excedentes_mwh = round(max(0.0, gen_val - max_mwh_val), 3)

            if precio_bolsa is not None:
                compras_cop = round(compras_mwh * 1000 * precio_bolsa, 2)
                excedentes_cop = round(excedentes_mwh * 1000 * precio_bolsa, 2)

            if compras_mwh > 0:
                n_deficit += 1
            else:
                n_cumplidos += 1

        tarifa_ppa = tarifas_map.get(c.id)

        # Valoracion del contrato: generacion * tarifa PPA (COP/kWh -> COP)
        valoracion_cop = None
        if tarifa_ppa is not None and gen_val > 0:
            valoracion_cop = round(gen_val * 1000 * tarifa_ppa, 2)

        # Upsert
        existing = CumplimientoMensual.objects.filter(
            contrato_ppa_id=c.id, anio=year, mes=month,
        ).first()

        campos = {
            "gen_total_mwh": gen_total_c,
            "compromiso_mwh": min_mwh,
            "compras_bolsa_mwh": compras_mwh,
            "excedentes_bolsa_mwh": excedentes_mwh,
            "precio_bolsa_promedio": precio_bolsa,
            "compras_bolsa_cop": compras_cop,
            "excedentes_bolsa_cop": excedentes_cop,
            "tarifa_ppa_cop_mwh": tarifa_ppa,
            "valoracion_contrato_cop": valoracion_cop,
            "estado": "cerrado",
        }
        if existing:
            # Un registro facturado NO se pisa.
            if existing.estado == "facturado":
                registros.append(_build_cumplimiento_out(existing))
                continue
            for campo, valor in campos.items():
                setattr(existing, campo, valor)
            a_actualizar.append(existing)
            registros.append(_build_cumplimiento_out(existing))
        else:
            nueva = CumplimientoMensual(
                contrato_ppa_id=c.id, proyecto_id=None, anio=year, mes=month, **campos,
            )
            a_crear.append(nueva)
            registros.append(_build_cumplimiento_out(nueva))

    with transaction.atomic():
        if a_actualizar:
            CumplimientoMensual.objects.bulk_update(a_actualizar, _CAMPOS_CIERRE)
        if a_crear:
            CumplimientoMensual.objects.bulk_create(a_crear)

    return {
        "anio": year,
        "mes": month,
        "contratos_procesados": len(contratos),
        "contratos_con_deficit": n_deficit,
        "contratos_cumplidos": n_cumplidos,
        "registros": registros,
    }


def historico(contrato_id=None, proyecto_id=None, anio=None, mes=None,
              estado=None) -> list[dict]:
    """Registros históricos de cumplimiento, con filtros opcionales.

    El `join` con el contrato no es decorativo: `_build_cumplimiento_out` lee su
    nombre y su comprador, y sin precargarlo son dos consultas por fila.
    """
    filtros = {
        "contrato_ppa_id": contrato_id,
        "proyecto_id": proyecto_id,
        "anio": anio,
        "mes": mes,
        "estado": estado,
    }
    qs = CumplimientoMensual.objects.select_related("contrato_ppa").filter(
        **{campo: valor for campo, valor in filtros.items() if valor is not None}
    )
    return [
        _build_cumplimiento_out(r)
        for r in qs.order_by("-anio", "-mes", "contrato_ppa_id")
    ]


def historico_detalle(record_id: int) -> dict:
    """Detalle de un registro de cumplimiento."""
    row = (
        CumplimientoMensual.objects
        .select_related("contrato_ppa")
        .filter(pk=record_id)
        .first()
    )
    if not row:
        raise NotFound("Registro de cumplimiento no encontrado")
    return _build_cumplimiento_out(row)


def facturar(record_id: int, liquidacion_id: int | None = None) -> dict:
    """Marca un registro como facturado y lo vincula a una liquidación."""
    row = (
        CumplimientoMensual.objects
        .select_related("contrato_ppa")
        .filter(pk=record_id)
        .first()
    )
    if not row:
        raise NotFound("Registro de cumplimiento no encontrado")
    if row.estado == "facturado":
        raise ValidationError("El registro ya esta facturado")

    row.estado = "facturado"
    campos = ["estado"]
    if liquidacion_id is not None:
        row.liquidacion_id = liquidacion_id
        campos.append("liquidacion_id")
    row.save(update_fields=campos)
    return _build_cumplimiento_out(row)
