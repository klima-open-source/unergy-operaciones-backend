"""Patrimonios autónomos: la jerarquía y a quién se le factura una participación.

Modelo de Sara (2026-10-14): el inversionista es el DUEÑO de su participación; si
la tiene a través de un patrimonio, al patrimonio se le factura y se le liquida.
Jerarquía de dos niveles: general (sin padre) → subpatrimonio.
"""
from __future__ import annotations

from django.db.models import Prefetch


class ReglaPatrimonio(ValueError):
    """Un cambio que rompe la jerarquía o deja participaciones huérfanas."""


def validar_padre(patrimonio, padre) -> None:
    """Un subpatrimonio cuelga de un GENERAL, y un general con subpatrimonios no
    puede pasar a ser subpatrimonio: la jerarquía es de dos niveles."""
    if padre is None:
        return
    if patrimonio.pk and padre.pk == patrimonio.pk:
        raise ReglaPatrimonio("Un patrimonio no puede ser su propio general.")
    if padre.padre_id is not None:
        raise ReglaPatrimonio(
            f'"{padre.nombre}" es un subpatrimonio: el general tiene que ser un '
            "patrimonio sin general."
        )
    if patrimonio.pk and patrimonio.subpatrimonios.exists():
        raise ReglaPatrimonio(
            f'"{patrimonio.nombre}" tiene subpatrimonios: no puede depender de otro.'
        )


def validar_borrado(patrimonio) -> None:
    """No se borra un patrimonio con participaciones o subpatrimonios: quedarían
    sin a quién facturar, o sin su general."""
    participaciones = patrimonio.participaciones.count()
    if participaciones:
        raise ReglaPatrimonio(
            f'"{patrimonio.nombre}" tiene {participaciones} participación(es). '
            "Quítaselas primero."
        )
    if patrimonio.subpatrimonios.exists():
        raise ReglaPatrimonio(
            f'"{patrimonio.nombre}" tiene subpatrimonios. Bórralos o muévelos primero.'
        )


def cliente_que_factura(patrimonio):
    """La ficha a la que se le factura un patrimonio: la suya, o la de su general si
    el subpatrimonio no tiene ficha propia."""
    if patrimonio is None:
        return None
    if patrimonio.cliente_id:
        return patrimonio.cliente
    if patrimonio.padre_id and patrimonio.padre.cliente_id:
        return patrimonio.padre.cliente
    return None


def destinatario(participacion):
    """A quién se le factura y liquida una participación: su patrimonio (o el
    general de este) si la tiene a través de uno; si no, su inversionista."""
    return (cliente_que_factura(participacion.patrimonio)
            or participacion.inversionista.cliente)


def arbol():
    """Los generales con sus subpatrimonios, y las participaciones de cada uno.
    `[Patrimonio(general, subpatrimonios=[…])…]` en tres consultas."""
    from apps.proyectos.models import Patrimonio, ProyectoInversionista

    participaciones = Prefetch(
        "participaciones",
        queryset=ProyectoInversionista.objects
        .select_related("proyecto", "inversionista__cliente")
        .order_by("proyecto__nombre_comercial"),
    )
    return list(
        Patrimonio.objects.filter(padre__isnull=True)
        .select_related("cliente")
        .prefetch_related(
            participaciones,
            Prefetch(
                "subpatrimonios",
                queryset=Patrimonio.objects.select_related("cliente")
                .prefetch_related(participaciones),
            ),
        )
    )
