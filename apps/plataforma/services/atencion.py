"""Lo que pide atención, para el Dashboard (fase 5 de la propuesta de UX).

Tres listas cortas que hoy había que armar a mano mirando varias pantallas:

- **Contratos por vencer**: vivos que terminan en los próximos `DIAS_POR_VENCER`
  días (PPA y de servicio), del más cercano al más lejano.
- **Plantas en operación sin contrato de operación**: generan
  (`estado=en_operacion`) pero ningún contrato vigente cubre su operación, así
  que quedan fuera del monitoreo y de los paneles de O&M (`plantas.filtro_operadas`).
- **Arrendadores sin cliente**: activos en un arriendo pero sin ficha de cliente,
  así que su cuenta de cobro no tiene a quién facturarse.

Cada fila trae la ruta de la ficha donde se resuelve.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Q

from apps.arriendos.models import ArrArrendador
from apps.contratos.models import Contrato
from apps.contratos.services import grupos
from apps.contratos.services import plantas as plantas_service
from apps.contratos.services import vigencia
from apps.plataforma.services.busqueda import titulo_de_contrato
from apps.proyectos.models import Proyecto

DIAS_POR_VENCER = 60
MAXIMO = 8


def _por_vencer(hoy: date) -> dict:
    hasta = hoy + timedelta(days=DIAS_POR_VENCER)
    es_ppa = grupos.PPA
    consulta = (
        Contrato.objects.filter(deleted_at__isnull=True, fecha_fin__gte=hoy, fecha_fin__lte=hasta)
        .filter(
            # Los de servicio, solo si siguen vivos (un terminado no vence).
            (Q(grupo=es_ppa) & vigencia.filtro_ppa_vivos(hoy))
            | (~Q(grupo=es_ppa) & vigencia.filtro_vivos(hoy))
        )
        .select_related("proyecto")
        .order_by("fecha_fin", "id")
    )
    return {
        "total": consulta.count(),
        "items": [{
            "id": c.id,
            "titulo": titulo_de_contrato(c),
            "detalle": f"vence el {c.fecha_fin.isoformat()} · en {(c.fecha_fin - hoy).days} días",
            "url": f"/contratos/{c.id}",
        } for c in consulta[:MAXIMO]],
    }


def _sin_operacion(hoy: date) -> dict:
    consulta = (
        Proyecto.objects.filter(deleted_at__isnull=True, estado="en_operacion")
        .exclude(plantas_service.filtro_proyectos_con(grupos.OPERACION, hoy))
        .order_by("nombre_comercial")
    )
    return {
        "total": consulta.count(),
        "items": [{
            "id": p.id,
            "titulo": p.nombre_comercial,
            "detalle": "en operación, sin contrato de operación vigente",
            "url": f"/proyectos/{p.id}?tab=servicios&servicio=operacion",
        } for p in consulta[:MAXIMO]],
    }


def _arrendadores_sin_cliente() -> dict:
    consulta = (
        ArrArrendador.objects.filter(activo=True, cliente__isnull=True, contrato__deleted_at__isnull=True)
        .select_related("contrato__proyecto")
        .order_by("nombre")
    )
    return {
        "total": consulta.count(),
        "items": [{
            "id": a.id,
            "titulo": a.nombre,
            "detalle": (
                f"arriendo de {a.contrato.proyecto.nombre_comercial}"
                if a.contrato.proyecto_id and a.contrato.proyecto else "arriendo sin planta"
            ),
            "url": f"/contratos/{a.contrato_id}",
        } for a in consulta[:MAXIMO]],
    }


def atencion(hoy: date) -> dict:
    return {
        "contratos_por_vencer": _por_vencer(hoy),
        "plantas_sin_operacion": _sin_operacion(hoy),
        "arrendadores_sin_cliente": _arrendadores_sin_cliente(),
        "dias_por_vencer": DIAS_POR_VENCER,
    }
