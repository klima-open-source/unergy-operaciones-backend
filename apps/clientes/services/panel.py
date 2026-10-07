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


from apps.clientes.models import Contacto
from apps.contratos.models import ContratoParte, ContratoServicio, GrupoContrato
from apps.contratos.services.contrato_partes import ROLES_PPA, ROLES_SERVICIO
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
    for cid, pid in _partes_de_servicio(cliente_ids).filter(
        contrato__proyecto_id__isnull=False
    ).values_list("cliente_id", "contrato__proyecto_id"):
        res[cid].add(pid)

    # Comprador o vendedor de un PPA: las plantas que el PPA cubre.
    for cid, pid in _partes_de_ppa(cliente_ids).filter(
        contrato__proyectos_vinculados__proyecto_id__isnull=False
    ).values_list("cliente_id", "contrato__proyectos_vinculados__proyecto_id"):
        res[cid].add(pid)
    return res


def _partes_de_servicio(cliente_ids):
    """Las filas de `contrato_partes` de esos clientes como contratante o prestador
    de un contrato de servicio."""
    return (
        ContratoParte.objects
        .filter(cliente_id__in=cliente_ids, rol__in=ROLES_SERVICIO)
        .exclude(contrato__grupo=GrupoContrato.PPA)
    )


def _partes_de_ppa(cliente_ids):
    """Las de esos clientes como comprador o vendedor de un PPA no borrado."""
    return ContratoParte.objects.filter(
        cliente_id__in=cliente_ids, rol__in=ROLES_PPA,
        contrato__grupo=GrupoContrato.PPA, contrato__deleted_at__isnull=True,
    )


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

    for cid, tipo in _partes_de_servicio(cliente_ids).values_list(
        "cliente_id", "contrato__servicio_aplica"
    ):
        res[cid].add(tipo)

    plantas = plantas if plantas is not None else proyectos_por_cliente(cliente_ids)
    por_proyecto = _proyecto_a_clientes(plantas)
    if por_proyecto:
        for pid, tipo in ContratoServicio.objects.filter(
            proyecto_id__in=por_proyecto.keys()
        ).values_list("proyecto_id", "servicio_aplica"):
            for cid in por_proyecto[pid]:
                res[cid].add(tipo)

    for cid in _partes_de_ppa(cliente_ids).values_list("cliente_id", flat=True):
        res[cid].add("ppa")
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

    for cid, fecha_fin, estado in _partes_de_servicio(cliente_ids).values_list(
        "cliente_id", "contrato__fecha_fin", "contrato__estado"
    ):
        if estado != "terminado":
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

    for cid, fecha_fin in _partes_de_ppa(cliente_ids).values_list(
        "cliente_id", "contrato__fecha_fin"
    ):
        _anotar(cid, fecha_fin)

    return {
        cid: {
            "alerta": peor_semaforo(sems) if peor_semaforo(sems) != "vigente" else None,
            "proximo_vencimiento": min(vencimientos[cid]) if vencimientos.get(cid) else None,
        }
        for cid, sems in semaforos.items()
    }
