"""Las partes de cada contrato (tabla `contrato_partes`): quién las escribe y cómo se consultan.

Lo llama `Contrato.save()`, así que cualquier camino que guarde un contrato —la API de
servicios, la de PPA, la firma del CRM, el admin, los comandos— las deja al día sin
acordarse. Es el mismo mecanismo que mantiene `servicios`.

Por ahora las partes se DERIVAN de las columnas comprador/vendedor/contratante/prestador
del contrato: esas columnas siguen siendo lo que escriben las pantallas. Cuando las
pantallas escriban aquí directamente, esta derivación desaparece junto con las columnas.

Dos caminos no pasan por `save()` y por eso se ocupan ellos mismos de esta tabla: la
fusión de clientes (`apps/clientes/services/gestion.py`, SQL directo) y la copia inicial
(`manage.py registrar_partes_contratos`).
"""
from __future__ import annotations

from apps.contratos.models import RolParte

#: Las partes de un PPA y las de un contrato de servicio.
ROLES_PPA = (RolParte.COMPRADOR, RolParte.VENDEDOR)
ROLES_SERVICIO = (RolParte.CONTRATANTE, RolParte.PRESTADOR)
ROLES = ROLES_PPA + ROLES_SERVICIO


def deseadas(contrato) -> set[tuple[str, int]]:
    """Las partes `(rol, cliente_id)` que el contrato debe tener. Puro: no toca la base."""
    return {
        (str(rol), cliente_id)
        for rol in ROLES
        if (cliente_id := getattr(contrato, f"{rol}_id", None))
    }


def registrar(contrato) -> set[tuple[str, int]]:
    """Deja en `contrato_partes` exactamente las de este contrato. Devuelve las que quedan."""
    from apps.contratos.models import ContratoParte

    actuales = set(
        ContratoParte.objects.filter(contrato_id=contrato.pk).values_list("rol", "cliente_id")
    )
    objetivo = deseadas(contrato)
    for rol, cliente_id in actuales - objetivo:
        ContratoParte.objects.filter(
            contrato_id=contrato.pk, rol=rol, cliente_id=cliente_id
        ).delete()
    faltan = objetivo - actuales
    if faltan:
        ContratoParte.objects.bulk_create([
            ContratoParte(contrato_id=contrato.pk, rol=rol, cliente_id=cliente_id)
            for rol, cliente_id in sorted(faltan)
        ])
    getattr(contrato, "_prefetched_objects_cache", {}).pop("partes", None)
    return objetivo


def contratos_de(cliente_ids, roles=ROLES):
    """Subconsulta con los ids de los contratos donde alguno de esos clientes es parte
    con alguno de esos papeles. Para filtrar: `.filter(pk__in=contratos_de({5}))`.

    Es una subconsulta y no un join para que un contrato donde el cliente tiene dos
    papeles (contratante Y prestador) no salga repetido."""
    from apps.contratos.models import ContratoParte

    return (
        ContratoParte.objects
        .filter(cliente_id__in=list(cliente_ids), rol__in=list(roles))
        .values("contrato_id")
    )
