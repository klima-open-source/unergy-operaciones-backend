"""Descubrimientos: la exposición en COP de lo que no se cubrió con contrato.

Puerto de `get_descubrimientos` (147 líneas). Cruza los deltas de energía del
cumplimiento contra el precio promedio de bolsa del mes.

Cuenta todos los contratos PPA vigentes: su trabajo es destapar exposición, no
esconderla.
"""

from __future__ import annotations

import logging

from django.db.models import Sum

from apps.ppa.models import PpaCompromisoEnergia, PpaTarifa
from apps.proyectos.models import GeneracionDiaria

from .consultas import _contratos_vigentes, _get_bolsa_avg, _resolve_gescon

logger = logging.getLogger("operaciones.cumplimiento")


def descubrimientos(year: int, month_from: int = 1, month_to: int = 12) -> dict:
    """Exposición financiera por descubrimientos de energía en bolsa.

    Cruza los deltas de MWh contra el precio promedio de bolsa del mes. **Solo
    lee la base**: no llama a la API de Unergy, así que se puede pedir el año
    entero sin esperar.
    """
    contratos = _contratos_vigentes(year)

    meses_data = []
    gran_total_compras_cop = 0.0
    gran_total_excedentes_cop = 0.0
    gran_total_compras_mwh = 0.0
    gran_total_excedentes_mwh = 0.0

    for m in range(month_from, month_to + 1):
        bolsa = _get_bolsa_avg(year, m)
        precio = bolsa["precio_promedio"]

        compromisos = {
            c.contrato_id: c
            for c in PpaCompromisoEnergia.objects.filter(año=year, mes=m)
        }

        tarifas = {
            t.contrato_id: float(t.tarifa)
            for t in PpaTarifa.objects.filter(
                contrato_id__in=[c.id for c in contratos], año=year, mes=m,
            )
            if t.tarifa is not None
        }

        # Generación real del mes, sumada desde `generacion_diaria`.
        gen_by_proyecto = {
            fila["proyecto_id"]: float(fila["kwh"]) / 1000.0
            for fila in GeneracionDiaria.objects
            .filter(fecha__year=year, fecha__month=m, kwh_real__isnull=False)
            .values("proyecto_id")
            .annotate(kwh=Sum("kwh_real"))
        }

        mes_compras_cop = 0.0
        mes_excedentes_cop = 0.0
        mes_compras_mwh = 0.0
        mes_excedentes_mwh = 0.0
        contratos_mes = []

        for c in contratos:
            comp = compromisos.get(c.id)
            if not comp:
                continue
            min_mwh = float(comp.energia_minima) if comp.energia_minima is not None else None
            max_mwh = float(comp.energia_maxima) if comp.energia_maxima is not None else None
            if min_mwh is None or max_mwh is None:
                continue

            # Sum generation from GESCON-assigned plants
            if c.numero_codigo_contrato:
                assignments = _resolve_gescon(c.numero_codigo_contrato, year, m)
            else:
                assignments = []

            gen_assigned = 0.0
            for asic in assignments:
                if asic.proyecto_id and asic.proyecto_id in gen_by_proyecto:
                    pct = float(asic.porcentaje_despacho or 0)
                    gen_assigned += gen_by_proyecto[asic.proyecto_id] * pct

            gen_assigned = round(gen_assigned, 3)
            compras_mwh = round(max(0.0, min_mwh - gen_assigned), 3)
            excedentes_mwh = round(max(0.0, gen_assigned - max_mwh), 3)

            tarifa_ppa = tarifas.get(c.id)
            compras_cop = 0.0
            excedentes_cop = 0.0
            sobrecosto = None

            if precio is not None:
                compras_cop = round(compras_mwh * 1000 * precio, 0)
                excedentes_cop = round(excedentes_mwh * 1000 * precio, 0)
                if tarifa_ppa and compras_mwh > 0:
                    sobrecosto = round(compras_mwh * 1000 * (precio - tarifa_ppa), 0)

            if compras_mwh > 0 or excedentes_mwh > 0:
                mes_compras_cop += compras_cop
                mes_excedentes_cop += excedentes_cop
                mes_compras_mwh += compras_mwh
                mes_excedentes_mwh += excedentes_mwh
                contratos_mes.append({
                    "contrato_id": c.id,
                    "nombre": c.nombre_interno or c.numero_codigo_contrato,
                    "comprador": c.comprador_nombre,
                    "min_mwh": min_mwh,
                    "max_mwh": max_mwh,
                    "gen_asignada_mwh": gen_assigned,
                    "compras_mwh": compras_mwh,
                    "excedentes_mwh": excedentes_mwh,
                    "compras_cop": compras_cop,
                    "excedentes_cop": excedentes_cop,
                    "sobrecosto_vs_ppa_cop": sobrecosto,
                    "tarifa_ppa_kwh": tarifa_ppa,
                })

        gran_total_compras_cop += mes_compras_cop
        gran_total_excedentes_cop += mes_excedentes_cop
        gran_total_compras_mwh += mes_compras_mwh
        gran_total_excedentes_mwh += mes_excedentes_mwh

        meses_data.append({
            "month": m,
            "precio_bolsa_avg": precio,
            "dias_con_precios": bolsa["dias_con_datos"],
            "compras_mwh": round(mes_compras_mwh, 3),
            "excedentes_mwh": round(mes_excedentes_mwh, 3),
            "compras_cop": round(mes_compras_cop, 0),
            "excedentes_cop": round(mes_excedentes_cop, 0),
            "contratos": contratos_mes,
        })

    return {
        "year": year,
        "month_from": month_from,
        "month_to": month_to,
        "totales": {
            "compras_bolsa_mwh": round(gran_total_compras_mwh, 3),
            "excedentes_bolsa_mwh": round(gran_total_excedentes_mwh, 3),
            "compras_bolsa_cop": round(gran_total_compras_cop, 0),
            "excedentes_bolsa_cop": round(gran_total_excedentes_cop, 0),
            "exposicion_neta_cop": round(gran_total_compras_cop - gran_total_excedentes_cop, 0),
        },
        "meses": meses_data,
    }
