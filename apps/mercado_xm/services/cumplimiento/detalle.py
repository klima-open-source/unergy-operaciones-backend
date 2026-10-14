"""El detalle de UN contrato en UN mes: `GET /cumplimiento/ppa/{id}`.

Puerto del endpoint homónimo (244 líneas). Es la vista que abre el usuario al
hacer clic en una fila del resumen: planta por planta, cuánto generó, cuánto le
tocaba al contrato según su % de despacho, y la valoración en COP contra el
precio de bolsa del mes.
"""

from __future__ import annotations

import calendar
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from rest_framework.exceptions import NotFound

from apps.plataforma.services.fechas import hoy_col
from apps.contratos.services import tarifas as tarifas_service
from apps.ppa.models import PpaCompromisoEnergia, PpaContrato

from .consultas import _get_bolsa_avg, _resolve_gescon
from .xm_api import _fetch_month, _fetch_recent_avg, _unergy_token

logger = logging.getLogger("operaciones.cumplimiento")


def cumplimiento_de_contrato(contrato_id: int, year: int, month: int) -> dict:
    """Cumplimiento energético de un contrato PPA en un mes.

    Consulta la API de Unergy en paralelo, hasta 10 plantas simultáneas.
    """
    # ── 1. Contrato ───────────────────────────────────────────
    contrato = PpaContrato.objects.filter(pk=contrato_id).first()
    if not contrato:
        raise NotFound("Contrato PPA no encontrado")

    # ── 2. Compromisos y tarifa ───────────────────────────────
    compromiso = PpaCompromisoEnergia.objects.filter(
        contrato_id=contrato_id, año=year, mes=month,
    ).first()
    precio_mes = tarifas_service.precios_ppa(year, month, contrato_ids=[contrato_id]).get(
        contrato_id)

    # ── 3. Período ────────────────────────────────────────────
    today = hoy_col()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 4. GESCON assignments ─────────────────────────────────
    if not contrato.numero_codigo_contrato:
        assignments = []
    else:
        assignments = _resolve_gescon(contrato.numero_codigo_contrato, year, month)

    # ── 5. Generación desde API Unergy (paralelo) ─────────────
    plantas_data = []
    sin_api_id: list[str] = []

    plants_with_id = []
    plants_without_id = []
    for asic in assignments:
        proyecto = asic.proyecto
        nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
        pct = float(asic.porcentaje_despacho or 0)
        is_dup = bool(asic.es_duplicado)
        if proyecto and proyecto.sub_project:
            plants_with_id.append({
                "nombre": nombre,
                "sub_project": proyecto.sub_project,
                "pct_despacho": pct,
                "es_duplicado": is_dup,
            })
        else:
            sin_api_id.append(nombre)
            plants_without_id.append({"nombre": nombre, "pct_despacho": pct, "es_duplicado": is_dup})

    if plants_with_id:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("No se pudo autenticar con la API de Unergy: %s", exc)
            token = None

        if token:
            def _fetch_one(p: dict) -> dict:
                if es_mes_futuro:
                    recent = _fetch_recent_avg(token, p["sub_project"])
                    avg = recent["avg_daily_mwh"]
                    gen_planta = round(avg * total_dias, 3) if avg is not None else None
                    n_registros = recent["n_days_used"]
                    ultimo_dia = None
                else:
                    gen = _fetch_month(token, p["sub_project"], year, month)
                    gen_planta = gen["mwh"]
                    n_registros = gen["n_records"]
                    ultimo_dia = gen["ultimo_dia"]
                gen_contrato = round(gen_planta * p["pct_despacho"], 3) if gen_planta is not None else None
                return {
                    "nombre": p["nombre"],
                    "sub_project": p["sub_project"],
                    "pct_despacho": p["pct_despacho"],
                    "es_duplicado": p["es_duplicado"],
                    "gen_planta_mwh": gen_planta,
                    "gen_contrato_mwh": gen_contrato,
                    "n_registros": n_registros,
                    "ultimo_dia": ultimo_dia,
                    "sin_datos": gen_planta is None,
                    "sin_api_id": False,
                }

            max_workers = min(len(plants_with_id), 10)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_fetch_one, plants_with_id))
            plantas_data.extend(results)
        else:
            for p in plants_with_id:
                plantas_data.append({
                    "nombre": p["nombre"], "sub_project": p["sub_project"],
                    "pct_despacho": p["pct_despacho"], "es_duplicado": p["es_duplicado"],
                    "gen_planta_mwh": None, "gen_contrato_mwh": None,
                    "n_registros": 0, "ultimo_dia": None, "sin_datos": True, "sin_api_id": False,
                })

    for p in plants_without_id:
        plantas_data.append({
            "nombre": p["nombre"],
            "sub_project": None,
            "pct_despacho": p["pct_despacho"],
            "es_duplicado": p["es_duplicado"],
            "gen_planta_mwh": None,
            "gen_contrato_mwh": None,
            "n_registros": 0,
            "ultimo_dia": None,
            "sin_datos": True,
            "sin_api_id": True,
        })

    # ── 6. Totales ────────────────────────────────────────────
    # gen_total cuenta TODO lo suministrado al contrato (real + compra en bolsa);
    # bolsa_dup es el subconjunto informativo proveniente de duplicados (origen bolsa).
    gen_total = round(
        sum(p["gen_contrato_mwh"] for p in plantas_data if p["gen_contrato_mwh"] is not None),
        3,
    )
    bolsa_dup = round(
        sum(p["gen_contrato_mwh"] for p in plantas_data if p["gen_contrato_mwh"] is not None and p["es_duplicado"]),
        3,
    )
    plantas_sin_datos = [p["nombre"] for p in plantas_data if p["sin_datos"]]

    # Proyección lineal al fin de mes (solo mes actual)
    if es_mes_actual and dia_actual > 0 and gen_total > 0:
        gen_proyectada = round(gen_total * total_dias / dia_actual, 3)
    else:
        gen_proyectada = gen_total

    # Día del último dato más antiguo entre plantas con datos
    dias_ultimo_dato = [p["ultimo_dia"] for p in plantas_data if p["ultimo_dia"] is not None]
    dia_min_datos = min(dias_ultimo_dato) if dias_ultimo_dato else None
    dia_max_datos = max(dias_ultimo_dato) if dias_ultimo_dato else None

    # ── 7. Balance ────────────────────────────────────────────
    min_mwh: Optional[float] = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None
    max_mwh: Optional[float] = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else None

    val_balance = gen_proyectada if (es_mes_actual or es_mes_futuro) else gen_total

    if min_mwh is not None or max_mwh is not None:
        effective_min = min_mwh if min_mwh is not None else 0.0
        effective_max = max_mwh if max_mwh is not None else float('inf')
        if val_balance < effective_min:
            estado = "deficit"
        elif val_balance > effective_max:
            estado = "excedente"
        else:
            estado = "ok"
        compras = round(max(0.0, effective_min - val_balance), 3)
        excedentes = round(max(0.0, val_balance - effective_max), 3) if max_mwh is not None else 0.0
        margen = round(effective_max - val_balance, 3) if max_mwh is not None else None
    else:
        estado = "sin_compromisos"
        compras = excedentes = margen = None

    # ── 8. Valoración COP con precios de bolsa ───────────────
    bolsa = _get_bolsa_avg(year, month)
    tarifa_ppa = float(precio_mes) if precio_mes else None
    precio_bolsa = bolsa["precio_promedio"]

    valoracion = None
    if precio_bolsa is not None and (compras or excedentes):
        compras_cop = round(compras * 1000 * precio_bolsa, 0) if compras else 0
        excedentes_cop = round(excedentes * 1000 * precio_bolsa, 0) if excedentes else 0
        delta_vs_ppa = None
        if tarifa_ppa and compras:
            delta_vs_ppa = round(compras * 1000 * (precio_bolsa - tarifa_ppa), 0)
        valoracion = {
            "precio_bolsa_avg_cop_kwh": precio_bolsa,
            "precio_bolsa_min_cop_kwh": bolsa["precio_min"],
            "precio_bolsa_max_cop_kwh": bolsa["precio_max"],
            "precio_escasez_cop_kwh": bolsa["precio_escasez"],
            "dias_con_precios": bolsa["dias_con_datos"],
            "tarifa_ppa_cop_kwh": tarifa_ppa,
            "compras_bolsa_cop": compras_cop,
            "excedentes_bolsa_cop": excedentes_cop,
            "sobrecosto_vs_ppa_cop": delta_vs_ppa,
        }

    return {
        "contrato": {
            "id": contrato.id,
            "nombre_interno": contrato.nombre_interno,
            "numero_codigo_contrato": contrato.numero_codigo_contrato,
            "comprador_nombre": contrato.comprador_nombre,
            "fecha_inicio": contrato.fecha_inicio.isoformat() if contrato.fecha_inicio else None,
            "fecha_fin": contrato.fecha_fin.isoformat() if contrato.fecha_fin else None,
        },
        "periodo": {
            "year": year,
            "month": month,
            "dia_actual": dia_actual,
            "dias_mes": total_dias,
            "es_mes_actual": es_mes_actual,
            "es_mes_futuro": es_mes_futuro,
            "tipo_datos": "proyeccion_historica" if es_mes_futuro else ("proyeccion_lineal" if es_mes_actual else "real"),
            "dia_min_datos": dia_min_datos,
            "dia_max_datos": dia_max_datos,
        },
        "compromisos": {
            "energia_minima_mwh": min_mwh,
            "energia_maxima_mwh": max_mwh,
        },
        "generacion": {
            "gen_total_mwh": gen_total,
            "gen_proyectada_mwh": gen_proyectada,
            "exposicion_bolsa_duplicados_mwh": bolsa_dup if bolsa_dup > 0 else None,
            "tarifa_cop_kwh": tarifa_ppa,
            "plantas": plantas_data,
            "plantas_sin_datos": plantas_sin_datos,
            "sin_api_id": sin_api_id,
            "n_plantas_activas": len(assignments),
        },
        "balance": {
            "estado": estado,
            "compras_bolsa_mwh": compras,
            "excedentes_bolsa_mwh": excedentes,
            "margen_mwh": margen,
        },
        "valoracion_bolsa": valoracion,
    }
