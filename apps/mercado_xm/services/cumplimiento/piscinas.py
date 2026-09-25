"""Qué planta está en qué contrato durante un mes: las piscinas a-g.

Puerto de `get_plantas_contratos` (`app/api/v1/cumplimiento.py`), que era un
endpoint de 252 líneas y es en realidad el NÚCLEO del módulo: `/vista-contratos`,
`/balance-energia` y `/clasificacion-energia` lo llaman y ninguno reimplementa
nada de esto. Al portarlo deja de ser una vista que otras vistas invocan pasándole
`db=..., _=None` y pasa a ser lo que siempre fue: un servicio.

**La bolsa es el RESIDUO en DÍAS, no el complemento del conjunto de plantas.**
Se restan los tramos ya cubiertos por un contrato y cada tramo libre genera su
propia fila. Con el criterio viejo ("plantas no asignadas"), una planta liberada
el 23 de julio quedaba en (a) los 31 días y la piscina (e) salía vacía.

**(b) es GESCON puro.** Las compras de UNGC salen de `asic_solicitudes`
—publicados con `codigo_sic_comprador == 'UNGC'`, agrupados por
`contrato_interno`—, no del módulo PPA. Los PPA `tipo_contrato='compra'` que
nunca llegaron a GESCON son la piscina (g), aparte, porque están fuera del MEM.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date

from django.db.models import Q

from apps.mercado_xm.models import AsicSolicitud
from apps.mercado_xm.services.gescon_vigencia import resolver_vigencias
from apps.ppa.models import PpaContratoProyecto
from apps.proyectos.models import Proyecto

from .consultas import (
    GESCON_PUBLICADA, _asc_nulls_first, _clasificar_remanente_bolsa,
    _contratos_venta_ocultos, _contratos_vigentes, _fin_efectivo_asic, _query_contratos_venta,
    _resolve_gescon,
)
from .periodos import (
    UNGC_COMERCIALIZADOR, _con_segmento, _estado_segmento, _fecha_corte,
    _recortar, _responsable_payload, _restar_intervalos,
)


def _plantas_del_mes(first_day: date) -> list[Proyecto]:
    """Las plantas que entran a Cumplimiento en el mes.

    Entra si YA tiene fecha de inicio de comercialización (primer día con
    generación real, autoderivada) O si tiene `sub_project` — aditivo, no saca
    ninguna que ya salía. Exige representación activa: el flag de Proyectos →
    Servicios es la fuente correcta, no `contratos_servicio`.
    """
    return list(
        Proyecto.objects
        .filter(estado="en_operacion", srv_representacion=True)
        # `!=` de SQL descarta los NULL; `exclude()` de Django los conserva.
        .exclude(tipo_proyecto="autoconsumo")
        .exclude(tipo_proyecto__isnull=True)
        .filter(
            Q(fecha_inicio_comercializacion__isnull=False) | Q(sub_project__isnull=False)
        )
        # Fuera las plantas cuya representación terminó antes del mes.
        .filter(
            Q(fecha_fin_representacion__isnull=True)
            | Q(fecha_fin_representacion__gte=first_day)
        )
        .order_by("nombre_comercial")
    )


def _proyectos_por_contrato_ppa(contrato_ids: list[int]) -> dict[int, list[Proyecto]]:
    """Las plantas vinculadas a mano a cada PPA (tabla `ppa_contrato_proyectos`).

    Una consulta para todos los contratos: el original recorría la relación
    perezosa dentro del bucle, una por contrato.
    """
    vinculos = (
        PpaContratoProyecto.objects
        .filter(contrato_id__in=contrato_ids)
        .values_list("contrato_id", "proyecto_id")
    )
    por_contrato: dict[int, list[int]] = defaultdict(list)
    for contrato_id, proyecto_id in vinculos:
        por_contrato[contrato_id].append(proyecto_id)

    todos = {p.id: p for p in Proyecto.objects.filter(
        id__in={pid for ids in por_contrato.values() for pid in ids}
    )}
    return {
        cid: [todos[pid] for pid in ids if pid in todos]
        for cid, ids in por_contrato.items()
    }


def _piscina_compra_ungc(first_day: date, last_day: date, corte: date) -> list[dict]:
    """(b) Compras de UNGC, resueltas sobre GESCON."""
    sics_ungc = list(
        AsicSolicitud.objects
        .filter(
            GESCON_PUBLICADA,
            codigo_sic_comprador=UNGC_COMERCIALIZADOR,
            codigo_sic_contrato__isnull=False,
        )
        .values_list("codigo_sic_contrato", flat=True)
        .distinct()
    )
    if not sics_ungc:
        return []

    # Se resuelve sobre TODO el historial de esos SIC (un relevo puede venir de
    # una fila con otro comprador) y solo después se filtra a UNGC.
    registros = list(
        AsicSolicitud.objects
        .select_related("proyecto")
        .filter(GESCON_PUBLICADA, codigo_sic_contrato__in=sics_ungc)
        .order_by(
            _asc_nulls_first("fecha_inicio"),
            _asc_nulls_first("fecha_solicitud"),
            "created_at",
        )
    )
    vigencias = resolver_vigencias(registros, hasta=last_day)

    por_contrato: dict[str, dict] = {}
    for r in registros:
        v = vigencias[r.id]
        if not v.procesado or r.tipo_solicitud == "terminacion":
            continue
        if not v.vigente and not v.saliente_por_relevo:
            continue  # superada en sitio: la representa su versión nueva
        if (r.codigo_sic_comprador or "") != UNGC_COMERCIALIZADOR:
            continue
        fin_ef = v.fecha_fin_efectiva
        if fin_ef is not None and fin_ef < first_day:
            continue
        if r.fecha_inicio and r.fecha_inicio > last_day:
            continue

        key = r.contrato_interno or f"SIC {r.codigo_sic_contrato}"
        card = por_contrato.setdefault(key, {
            "id": r.contrato_ppa_id or f"gescon-{r.codigo_sic_contrato}",
            "contrato_ppa_id": r.contrato_ppa_id,
            "contrato_interno": r.contrato_interno,
            "nombre": r.nombre_interno or key,
            "vendedor_nombre": r.codigo_sic_vendedor or "—",
            "fecha_inicio": None,
            "fecha_fin": None,
            "plantas": [],
        })
        if r.contrato_ppa_id and not card["contrato_ppa_id"]:
            card["contrato_ppa_id"] = r.contrato_ppa_id
            card["id"] = r.contrato_ppa_id
        if r.proyecto_id:
            fin_mostrado = fin_ef or r.fecha_fin
            card["plantas"].append(_con_segmento({
                "id": r.proyecto_id,
                "nombre": r.proyecto.nombre_comercial if r.proyecto else f"Proyecto {r.proyecto_id}",
                "codigo_sic": r.codigo_sic_contrato,
                "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
                # fin EFECTIVO (recortado por relevos), no la fecha cruda
                "fecha_fin": fin_mostrado.isoformat() if fin_mostrado else None,
            }, r.fecha_inicio, fin_mostrado, first_day, last_day, corte))

    return sorted(por_contrato.values(), key=lambda c: c["nombre"])


def plantas_contratos(year: int, month: int, incluir_todos: bool = False) -> dict:
    """Todas las plantas agrupadas por contrato: venta, compra, bolsa y externas."""
    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)

    plantas_db = _plantas_del_mes(first_day)
    plantas_map = {p.id: p for p in plantas_db}
    corte = _fecha_corte(year, month)

    # ── (a) VENTA: GESCON resuelve qué planta está en qué contrato ──────────
    venta_out = []
    # Días del mes que cada planta pasó asignada a un contrato de venta. La bolsa
    # es el RESIDUO de esto, no el complemento del conjunto de plantas: así una
    # planta liberada a mitad de mes aparece en las dos.
    assigned_windows: dict[int, list] = defaultdict(list)
    for c in _query_contratos_venta(year, month, solo_relevantes=not incluir_todos):
        plantas_list = []
        if c.numero_codigo_contrato:
            for asic in _resolve_gescon(c.numero_codigo_contrato, year, month):
                if asic.proyecto_id and asic.proyecto_id in plantas_map:
                    p = plantas_map[asic.proyecto_id]
                    seg = _recortar(asic.fecha_inicio, asic.fecha_fin, first_day, last_day)
                    if seg:
                        assigned_windows[p.id].append(seg)
                    plantas_list.append(_con_segmento({
                        "id": p.id,
                        "nombre": p.nombre_comercial,
                        "codigo_sic": asic.codigo_sic_contrato,
                        "fecha_inicio": asic.fecha_inicio.isoformat() if asic.fecha_inicio else None,
                        "fecha_fin": asic.fecha_fin.isoformat() if asic.fecha_fin else None,
                        "pct_despacho": float(asic.porcentaje_despacho or 0),
                        "es_duplicado": bool(asic.es_duplicado),
                        "uso_del_recurso": bool(getattr(asic, "uso_del_recurso", False)),
                        # 'plg' | 'plc': una planta repartida entre dos contratos,
                        # uno de cada modalidad, no está duplicada.
                        "modalidad_pago": getattr(asic, "modalidad_pago", None),
                    }, asic.fecha_inicio, asic.fecha_fin, first_day, last_day, corte))
        venta_out.append({
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            # Clave GESCON (contrato_interno en asic_solicitudes) para el detalle
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            "plantas": plantas_list,
        })

    # Contratos escondidos por responsable: no se muestran, pero sus plantas
    # siguen asignadas. Sus días cuentan como asignados para que la planta no
    # reaparezca como bolsa (ver `_contratos_venta_ocultos`).
    if not incluir_todos:
        for c in _contratos_venta_ocultos(year, month):
            if not c.numero_codigo_contrato:
                continue
            for asic in _resolve_gescon(c.numero_codigo_contrato, year, month):
                if asic.proyecto_id and asic.proyecto_id in plantas_map:
                    seg = _recortar(asic.fecha_inicio, asic.fecha_fin, first_day, last_day)
                    if seg:
                        assigned_windows[asic.proyecto_id].append(seg)

    # ── (b) COMPRA UNGC ─────────────────────────────────────────────────────
    compra_out = _piscina_compra_ungc(first_day, last_day, corte)

    # ── (g) COMPRA EXTERNA: PPAs de compra FUERA de GESCON ──────────────────
    # Plantas de terceros a las que Unergy compra con un PPA firmado a mano y sin
    # registro en GESCON/ASIC. Un PPA de compra que sí llegó a GESCON ya está en
    # (b) y se excluye acá para no duplicarlo.
    gescon_compra_ids = {c["contrato_ppa_id"] for c in compra_out if c.get("contrato_ppa_id")}
    vigentes = _contratos_vigentes(year, month, solo_relevantes=not incluir_todos)
    externos = [
        c for c in vigentes
        if (c.tipo_contrato or "venta") == "compra" and c.id not in gescon_compra_ids
    ]
    proyectos_por_contrato = _proyectos_por_contrato_ppa([c.id for c in externos])

    compra_externa_out = [
        {
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            "numero_codigo_contrato": c.numero_codigo_contrato,
            **_responsable_payload(c),
            "vendedor_nombre": c.vendedor_nombre,
            "vendedor_nit": c.vendedor_nit,
            "tarifa_base": float(c.tarifa_base) if c.tarifa_base is not None else None,
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            # Plantas externas vinculadas al PPA: no se les exige representación
            # ni estado en_operacion, no son plantas del portafolio Unergy.
            "plantas": [
                _con_segmento({
                    "id": p.id,
                    "nombre": p.nombre_comercial,
                    "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
                    "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
                }, c.fecha_inicio, c.fecha_fin, first_day, last_day, corte)
                for p in proyectos_por_contrato.get(c.id, [])
            ],
        }
        for c in externos
    ]

    # ── (c/e/f) BOLSA: los días del mes sin contrato PPA ────────────────────
    bolsa_plantas, bolsa_comercializador, bolsa_libre = [], [], []
    for p in plantas_db:
        # Ventana operativa dentro del mes: no se inventan días en bolsa antes de
        # que la planta empiece a comercializar ni después de que termine su
        # representación. Ambos campos suelen ser NULL → mes completo.
        operativa = _recortar(
            p.fecha_inicio_comercializacion, p.fecha_fin_representacion, first_day, last_day
        )
        for seg_ini, seg_fin in _restar_intervalos(operativa, assigned_windows.get(p.id) or []):
            # La modalidad se evalúa SOBRE EL TRAMO, no sobre el mes: una planta
            # que salió de contrato el 23 se juzga por lo que tenga del 24 al 31.
            piscina, asic = _clasificar_remanente_bolsa(p.id, seg_ini, seg_fin)
            fin_efectivo = _fin_efectivo_asic(asic, seg_fin) if asic else None
            entry = {
                "id": p.id,
                "nombre": p.nombre_comercial,
                "piscina": piscina,
                "codigo_sic": asic.codigo_sic_contrato if asic else None,
                "codigo_sic_comprador": asic.codigo_sic_comprador if asic else None,
                # Ventana de la modalidad: inicio del registro SIC y fin EFECTIVO
                # (recortado por relevos). Nulos en la piscina libre — ahí la
                # ventana que importa es el propio tramo (segmento_*).
                "fecha_inicio": asic.fecha_inicio.isoformat() if asic and asic.fecha_inicio else None,
                "fecha_fin": fin_efectivo.isoformat() if fin_efectivo else None,
                "segmento_inicio": seg_ini.isoformat(),
                "segmento_fin": seg_fin.isoformat(),
                "estado": _estado_segmento(seg_ini, seg_fin, corte),
            }
            bolsa_plantas.append(entry)
            (bolsa_comercializador if piscina == "comercializador" else bolsa_libre).append(entry)

    out = {
        "year": year,
        "month": month,
        # Fecha contra la que se evalúa "vigente" (hoy si el mes es el actual, fin
        # de mes si no). El front la usa para explicar los contadores.
        "fecha_corte": corte.isoformat(),
        "venta": venta_out,
        "compra": compra_out,
        # "bolsa" sigue siendo la lista COMPLETA del remanente con el shape de
        # siempre más el campo "piscina"; las dos sub-listas apuntan a los mismos
        # objetos, para el front que quiera consumirlas directas.
        "bolsa": bolsa_plantas,
        "bolsa_comercializador": bolsa_comercializador,
        "bolsa_libre": bolsa_libre,
        "compra_externa": compra_externa_out,
    }
    # Piscinas estandarizadas a-f (misma fuente que GET /clasificacion-energia):
    # aditivo, re-agrupa lo anterior sin tocar las claves existentes.
    from apps.mercado_xm.services.clasificacion_energia import derivar_pools

    out.update(derivar_pools(out))
    return out
