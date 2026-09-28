"""Agregados por cliente: la vista comercial de /clientes y el panel 360.

Puerto de `app/services/clientes_panel.py`.

**"Planta con nosotros"** = proyecto donde el cliente cumple cualquiera de:
es inversionista (`proyecto_inversionistas`), es contratante O prestador de un
contrato de servicio sobre el proyecto, o es comprador o vendedor de un PPA que
cubre el proyecto.

**`contratante_id`/`prestador_id` casi nunca se pobla** — el campo del wizard de
contrato es texto libre (auditoría de Clientes, 2026-08-27). De ahí el fallback
por planta del cliente en `servicios_por_cliente` y `alerta_contratos_por_cliente`:
sin él, la columna Servicios y el semáforo de /clientes ignoraban en silencio los
contratos de servicio reales y solo veían PPAs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from django.db.models import Q

from apps.clientes.models import Contacto
from apps.contratos.models import ContratoServicio
from apps.ppa.models import PpaContrato, PpaContratoProyecto
from apps.proyectos.models import ProyectoInversionista

UMBRAL_POR_VENCER_DIAS = 90
_ORDEN_SEMAFORO = {"vigente": 0, "por_vencer": 1, "vencido": 2}


def semaforo_contrato(fecha_fin: date | None, hoy: date,
                      umbral_dias: int = UMBRAL_POR_VENCER_DIAS) -> str:
    """Sin fecha fin = contrato indefinido = vigente."""
    if fecha_fin is None:
        return "vigente"
    if fecha_fin < hoy:
        return "vencido"
    if (fecha_fin - hoy).days <= umbral_dias:
        return "por_vencer"
    return "vigente"


def peor_semaforo(semaforos: list[str]) -> str | None:
    if not semaforos:
        return None
    return max(semaforos, key=lambda s: _ORDEN_SEMAFORO.get(s, 0))


def renovacion_combinada(valores: list[bool | None]) -> bool | None:
    """True si algún contrato renueva; False si hay dato y ninguno renueva; None
    si no hay ningún dato (la UI muestra '—')."""
    con_dato = [v for v in valores if v is not None]
    if not con_dato:
        return None
    return any(con_dato)


def proyectos_por_cliente(cliente_ids: set[int]) -> dict[int, set[int]]:
    res: dict[int, set[int]] = defaultdict(set)
    if not cliente_ids:
        return res

    for cid, pid in ProyectoInversionista.objects.filter(
        cliente_id__in=cliente_ids
    ).values_list("cliente_id", "proyecto_id"):
        res[cid].add(pid)

    # Contratante y prestador: un cliente que PRESTA el servicio también tiene
    # esa planta "con nosotros" (mismo criterio que `servicios_por_cliente`).
    for campo in ("contratante_id", "prestador_id"):
        for cid, pid in ContratoServicio.objects.filter(
            **{f"{campo}__in": cliente_ids}, proyecto_id__isnull=False
        ).values_list(campo, "proyecto_id"):
            res[cid].add(pid)

    filas_ppa = (
        PpaContratoProyecto.objects
        .filter(contrato__deleted_at__isnull=True)
        .filter(
            Q(contrato__comprador_id__in=cliente_ids)
            | Q(contrato__vendedor_id__in=cliente_ids)
        )
        .values_list("contrato__comprador_id", "contrato__vendedor_id", "proyecto_id")
    )
    for comprador_id, vendedor_id, pid in filas_ppa:
        if comprador_id in cliente_ids:
            res[comprador_id].add(pid)
        if vendedor_id in cliente_ids:
            res[vendedor_id].add(pid)
    return res


def _proyecto_a_clientes(plantas: dict[int, set[int]]) -> dict[int, set[int]]:
    salida: dict[int, set[int]] = defaultdict(set)
    for cid, pids in plantas.items():
        for pid in pids:
            salida[pid].add(cid)
    return salida


def servicios_por_cliente(cliente_ids: set[int],
                          plantas: dict[int, set[int]] | None = None) -> dict[int, set[str]]:
    res: dict[int, set[str]] = defaultdict(set)
    if not cliente_ids:
        return res

    for campo in ("contratante_id", "prestador_id"):
        for cid, tipo in ContratoServicio.objects.filter(
            **{f"{campo}__in": cliente_ids}
        ).values_list(campo, "servicio_aplica"):
            res[cid].add(tipo)

    plantas = plantas if plantas is not None else proyectos_por_cliente(cliente_ids)
    por_proyecto = _proyecto_a_clientes(plantas)
    if por_proyecto:
        for pid, tipo in ContratoServicio.objects.filter(
            proyecto_id__in=por_proyecto.keys()
        ).values_list("proyecto_id", "servicio_aplica"):
            for cid in por_proyecto[pid]:
                res[cid].add(tipo)

    filas_ppa = (
        PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .filter(Q(comprador_id__in=cliente_ids) | Q(vendedor_id__in=cliente_ids))
        .values_list("comprador_id", "vendedor_id")
    )
    for comprador_id, vendedor_id in filas_ppa:
        if comprador_id in cliente_ids:
            res[comprador_id].add("ppa")
        if vendedor_id in cliente_ids:
            res[vendedor_id].add("ppa")
    return res


def contacto_comercial_por_cliente(cliente_ids: set[int]) -> dict[int, dict]:
    """El primer contacto de tipo 'comercial' de cada cliente, más cuántos
    comerciales adicionales hay. La tabla muestra el principal; el detalle, todos."""
    res: dict[int, dict] = {}
    if not cliente_ids:
        return res

    por_cliente: dict[int, list] = defaultdict(list)
    for c in Contacto.objects.filter(
        cliente_id__in=cliente_ids, tipo="comercial"
    ).order_by("cliente_id", "id"):
        por_cliente[c.cliente_id].append(c)

    for cid, contactos in por_cliente.items():
        principal = contactos[0]
        res[cid] = {
            "nombre": principal.nombre,
            "telefono": principal.telefono,
            "correo": principal.email,
            "adicionales": len(contactos) - 1,
        }
    return res


def alerta_contratos_por_cliente(cliente_ids: set[int], hoy: date,
                                 plantas: dict[int, set[int]] | None = None) -> dict[int, dict]:
    """El peor semáforo entre los contratos NO terminados del cliente, y la fecha
    de fin futura más cercana."""
    if not cliente_ids:
        return {}

    semaforos: dict[int, list[str]] = defaultdict(list)
    vencimientos: dict[int, list[date]] = defaultdict(list)

    def _anotar(cid, fecha_fin):
        semaforos[cid].append(semaforo_contrato(fecha_fin, hoy))
        if fecha_fin and fecha_fin >= hoy:
            vencimientos[cid].append(fecha_fin)

    filas_serv = (
        ContratoServicio.objects
        .filter(Q(contratante_id__in=cliente_ids) | Q(prestador_id__in=cliente_ids))
        .values_list("contratante_id", "prestador_id", "fecha_fin", "estado")
    )
    for contratante_id, prestador_id, fecha_fin, estado in filas_serv:
        if estado == "terminado":
            continue
        for cid in (contratante_id, prestador_id):
            if cid in cliente_ids:
                _anotar(cid, fecha_fin)

    plantas = plantas if plantas is not None else proyectos_por_cliente(cliente_ids)
    por_proyecto = _proyecto_a_clientes(plantas)
    if por_proyecto:
        for pid, fecha_fin, estado in ContratoServicio.objects.filter(
            proyecto_id__in=por_proyecto.keys()
        ).values_list("proyecto_id", "fecha_fin", "estado"):
            if estado == "terminado":
                continue
            for cid in por_proyecto[pid]:
                _anotar(cid, fecha_fin)

    filas_ppa = (
        PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .filter(Q(comprador_id__in=cliente_ids) | Q(vendedor_id__in=cliente_ids))
        .values_list("comprador_id", "vendedor_id", "fecha_fin")
    )
    for comprador_id, vendedor_id, fecha_fin in filas_ppa:
        for cid in (comprador_id, vendedor_id):
            if cid in cliente_ids:
                _anotar(cid, fecha_fin)

    return {
        cid: {
            "alerta": peor_semaforo(sems) if peor_semaforo(sems) != "vigente" else None,
            "proximo_vencimiento": min(vencimientos[cid]) if vencimientos.get(cid) else None,
        }
        for cid, sems in semaforos.items()
    }
