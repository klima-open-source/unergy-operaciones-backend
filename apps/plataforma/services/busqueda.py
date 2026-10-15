"""Búsqueda global (⌘K): plantas, clientes, contratos y fronteras en una consulta.

Fase 5 de la propuesta de UX del 2026-10-14. Desde cualquier página se busca por
lo que la gente recuerda —el nombre o el código de una planta, la razón social o
el NIT de un cliente, el número o el nombre de un contrato, el código de una
frontera— y cada resultado trae la ruta de su ficha. Solo lee, y solo lo que las
listas ya muestran a cualquier usuario autenticado.
"""
from __future__ import annotations

from django.db.models import Q

from apps.clientes.models import Cliente
from apps.contratos.models import Contrato
from apps.contratos.services import grupos
from apps.fronteras.models import Frontera
from apps.proyectos.models import Proyecto

MINIMO = 2
POR_TIPO = 6

#: Cómo se nombra un contrato de servicio que no tiene nombre propio.
SERVICIO_LABEL = {
    "representacion": "Representación",
    "cgm": "CGM",
    "mantenimiento": "Mantenimiento",
    "arriendo": "Arriendo",
    "internet": "Internet",
}


def titulo_de_contrato(c: Contrato) -> str:
    """«Santa Fe 2» para un PPA; «Arriendo · MGS 0077» para uno de servicio."""
    if c.grupo == grupos.PPA:
        return c.nombre_interno or c.numero_codigo_contrato or f"PPA #{c.id}"
    servicio = SERVICIO_LABEL.get(c.servicio_aplica or "", c.servicio_aplica or "Contrato")
    planta = c.proyecto.nombre_comercial if c.proyecto_id and c.proyecto else None
    return f"{servicio} · {planta}" if planta else f"{servicio} #{c.id}"


def _proyectos(q: str) -> list[dict]:
    filas = (
        Proyecto.objects.filter(deleted_at__isnull=True)
        .filter(
            Q(nombre_comercial__icontains=q) | Q(codigo_tsf__icontains=q)
            | Q(origina_code__icontains=q) | Q(municipio__icontains=q)
        )
        .order_by("nombre_comercial")[:POR_TIPO]
    )
    return [{
        "id": p.id,
        "titulo": p.nombre_comercial,
        "detalle": " · ".join(x for x in (p.codigo_tsf, p.municipio, p.estado) if x),
        "url": f"/proyectos/{p.id}",
    } for p in filas]


def _clientes(q: str) -> list[dict]:
    filas = (
        Cliente.objects.filter(deleted_at__isnull=True)
        .filter(Q(razon_social_nombre__icontains=q) | Q(nit_cedula__icontains=q))
        .order_by("razon_social_nombre")[:POR_TIPO]
    )
    return [{
        "id": c.id,
        "titulo": c.razon_social_nombre,
        "detalle": f"NIT {c.nit_cedula}" if c.nit_cedula else "",
        "url": f"/clientes/{c.id}",
    } for c in filas]


def _contratos(q: str) -> list[dict]:
    filas = (
        Contrato.objects.filter(deleted_at__isnull=True)
        .filter(
            Q(numero_contrato__icontains=q) | Q(nombre_interno__icontains=q)
            | Q(numero_codigo_contrato__icontains=q)
            | Q(proyectos_vinculados__proyecto__nombre_comercial__icontains=q)
        )
        .distinct()
        # La planta vive en `contrato_proyectos` (`contrato.proyecto` es un atajo).
        .prefetch_related("proyectos_vinculados__proyecto")
        .order_by("-fecha_inicio", "-id")[:POR_TIPO]
    )
    return [{
        "id": c.id,
        "titulo": titulo_de_contrato(c),
        "detalle": " · ".join(x for x in (
            "PPA" if c.grupo == grupos.PPA else None,
            c.numero_contrato or c.numero_codigo_contrato,
            c.estado,
        ) if x),
        # `?tipo=ppa`: la ficha va directo al PPA sin probar antes el endpoint de servicio.
        "url": f"/contratos/{c.id}?tipo=ppa" if c.grupo == grupos.PPA else f"/contratos/{c.id}",
    } for c in filas]


def _fronteras(q: str) -> list[dict]:
    filas = (
        Frontera.objects.filter(Q(codigo_frontera__icontains=q) | Q(nombre_frontera__icontains=q))
        .select_related("proyecto")
        .order_by("codigo_frontera")[:POR_TIPO]
    )
    return [{
        "id": f.id,
        "titulo": f.codigo_frontera or f.nombre_frontera,
        "detalle": " · ".join(x for x in (
            f.nombre_frontera if f.codigo_frontera else None,
            f.proyecto.nombre_comercial if f.proyecto_id and f.proyecto else None,
            f.estado,
        ) if x),
        # La frontera se ve en la ficha de su planta; sin planta, en la lista.
        "url": (
            f"/proyectos/{f.proyecto_id}?tab=info&seccion=fronteras"
            if f.proyecto_id else f"/mem/fronteras?q={f.codigo_frontera or ''}"
        ),
    } for f in filas]


def buscar(q: str) -> dict:
    """Hasta `POR_TIPO` resultados de cada tipo. Vacío si `q` tiene menos de `MINIMO` letras."""
    q = (q or "").strip()
    if len(q) < MINIMO:
        return {"proyectos": [], "clientes": [], "contratos": [], "fronteras": []}
    return {
        "proyectos": _proyectos(q),
        "clientes": _clientes(q),
        "contratos": _contratos(q),
        "fronteras": _fronteras(q),
    }
