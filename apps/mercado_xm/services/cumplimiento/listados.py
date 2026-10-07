"""Los dos listados baratos de Cumplimiento: contratos y totales del año.

Puerto de `/ppa` y `/ppa/resumen-anual`. Ninguno llama a la API de generación, así
que responden al instante y sirven para pintar la vista antes de que llegue lo
pesado.
"""

from __future__ import annotations

from collections import defaultdict

from apps.ppa.models import PpaCompromisoEnergia, PpaContrato
from apps.contratos.services import contrato_partes

from .consultas import _asc_nulls_last, _contratos_vigentes, _filtro_responsable_relevante
from .periodos import _contrato_vigente_en_mes, _responsable_payload


def listar_contratos(incluir_todos: bool = False) -> list[dict]:
    """Todos los contratos PPA, para el selector."""
    qs = (
        PpaContrato.objects
        .select_related("responsable")
        .prefetch_related(contrato_partes.CON_PARTES)
        .filter(deleted_at__isnull=True)
    )
    if not incluir_todos:
        clausula = _filtro_responsable_relevante()
        if clausula is not None:
            qs = qs.filter(clausula)
    rows = qs.order_by(_asc_nulls_last("nombre_interno"), "id")
    return [
        {
            "id": r.id,
            "nombre_interno": r.nombre_interno,
            "numero_codigo_contrato": r.numero_codigo_contrato,
            "comprador_nombre": r.comprador_nombre,
            "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
            "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
            **_responsable_payload(r),
        }
        for r in rows
    ]


def resumen_anual(year: int, incluir_todos: bool = False) -> list[dict]:
    """Totales de compromiso por contrato en el año. **Solo base**, sin llamar a
    la API de Unergy: es instantáneo y sirve de cabecera de la vista anual.

    Solo cuenta los compromisos de meses en que el contrato estuvo VIGENTE:
    antes sumaba los 12 sin filtrar y contratos terminados en abril mostraban
    compromiso de mayo a diciembre.
    """
    contratos = _contratos_vigentes(year, solo_relevantes=not incluir_todos)
    compromisos = PpaCompromisoEnergia.objects.filter(año=year)
    comp_by_c: dict = defaultdict(list)
    for c in compromisos:
        comp_by_c[c.contrato_id].append(c)

    result = []
    for c in contratos:
        # Solo contar compromisos de meses en los que el contrato estuvo vigente:
        # excluye meses posteriores a fecha_fin (contrato terminado) y anteriores a
        # fecha_inicio. Antes sumaba los 12 meses sin filtrar (p.ej. Naos 2/3 mostraban
        # compromiso may-dic pese a terminar el 30-abr-2026).
        rows = [r for r in comp_by_c.get(c.id, []) if _contrato_vigente_en_mes(c, year, r.mes)]
        total_min = sum(float(r.energia_minima) for r in rows if r.energia_minima is not None)
        total_max = sum(float(r.energia_maxima) for r in rows if r.energia_maxima is not None)
        plantas_vals = [int(r.cantidad_proyectos) for r in rows if r.cantidad_proyectos is not None]
        result.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            "total_min_mwh": round(total_min, 1) if rows else None,
            "total_max_mwh": round(total_max, 1) if rows else None,
            "meses_con_compromisos": len(rows),
            # Plantas esperadas (denominador): valor máximo definido entre los meses del año.
            "plantas_esperadas": max(plantas_vals) if plantas_vals else None,
        })
    return result
