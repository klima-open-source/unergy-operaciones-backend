"""Las partes de cada contrato (tabla `contrato_partes`): cómo se escriben y cómo se consultan.

Una fila por contrato, papel y cliente. El nombre y el NIT de cada parte son los de la
ficha del cliente: no se guardan en el contrato (decisión de Sara, 2026-10-07; las
columnas `comprador_*`, `vendedor_*`, `contratante_*` y `prestador_*` de `contratos`
se retiraron).

Se escriben asignando en el contrato y guardándolo —`contrato.contratante_id = 5;
contrato.save()`—: `Contrato.save()` llama a `asignar`. Así la API de contratos, la de
PPA, la firma del CRM y el admin escriben por el mismo camino.
"""
from __future__ import annotations

from django.db.models import Prefetch

from apps.contratos.models import ContratoParte, RolParte

#: Las partes de un PPA y las de un contrato de servicio.
ROLES_PPA = (RolParte.COMPRADOR, RolParte.VENDEDOR)
ROLES_SERVICIO = (RolParte.CONTRATANTE, RolParte.PRESTADOR)
ROLES = ROLES_PPA + ROLES_SERVICIO

#: Para un listado: `Contrato.objects.prefetch_related(CON_PARTES)` trae las partes y
#: sus fichas en dos consultas, en vez de una por contrato.
CON_PARTES = Prefetch(
    "partes", queryset=ContratoParte.objects.select_related("cliente").order_by("id"),
)


def asignar(contrato, partes: dict) -> None:
    """Deja como única parte de cada papel la que se indica (`None` la quita).

    `partes` es `{rol: Cliente | None}`. Un papel que no aparece no se toca. Hoy cada
    papel tiene una sola parte; cuando un arriendo admita varios arrendadores, esto
    recibirá listas."""
    for rol, cliente in partes.items():
        ContratoParte.objects.filter(contrato_id=contrato.pk, rol=rol).delete()
        if cliente is not None:
            ContratoParte.objects.create(contrato_id=contrato.pk, rol=rol, cliente=cliente)
    contrato.__dict__.pop("_partes_cache", None)
    getattr(contrato, "_prefetched_objects_cache", {}).pop("partes", None)


def contratos_de(cliente_ids, roles=ROLES):
    """Subconsulta con los ids de los contratos donde alguno de esos clientes es parte
    con alguno de esos papeles. Para filtrar: `.filter(pk__in=contratos_de({5}))`.

    Es una subconsulta y no un join para que un contrato donde el cliente tiene dos
    papeles (contratante Y prestador) no salga repetido."""
    return (
        ContratoParte.objects
        .filter(cliente_id__in=list(cliente_ids), rol__in=list(roles))
        .values("contrato_id")
    )
