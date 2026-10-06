"""Los servicios que cubre un contrato (tabla `servicios`): el ÚNICO lugar que los escribe.

Lo llama `Contrato.save()`, así que cualquier camino que guarde un contrato —la API de
servicios, la de PPA, la firma del CRM, el admin— los deja al día sin acordarse. La API
de contratos de servicio lo llama además con la lista explícita cuando el formulario la
manda (plan `docs/refactor/08-plan-django-contratos.md`, §3).

Por grupo:

- **PPA:** una fila, `compra` o `venta`, de `tipo_contrato`. Las dos cosas se escriben
  desde aquí y nunca por separado (decisión 4 del plan).
- **Operación:** una fila, la de `servicio_aplica`: cada subservicio de operación tiene
  su propio contrato, siempre.
- **Representación / CGM:** los que se pidan (`representacion`, `cgm` o los dos). Si no
  se piden, se conservan los que tenga; un contrato nuevo sin lista cubre representación.
  Aquí `servicio_aplica` vale siempre `representacion` y nombra al GRUPO, no al servicio
  (`'cgm'` no se usa: liquidaciones, costos y la pestaña filtran por `representacion`),
  así que no dice nada de si el contrato cubre representación. Un contrato solo de CGM
  tiene `servicio_aplica='representacion'` y una sola fila, `cgm`.

Ya no se deduce nada de las tarifas cargadas: la tarifa puede llegar después, o nunca,
sin cambiar qué servicios cubre el contrato. La única deducción que queda es la de la
copia inicial (`backfill_contratos_unificados`), una sola vez.
"""
from __future__ import annotations

from apps.contratos.services import grupos


class ServiciosInvalidos(ValueError):
    """Una lista de servicios que no corresponde al grupo del contrato (→ 400)."""


def deseados(contrato, servicios=None, actuales=None) -> set[str]:
    """Los servicios que el contrato debe tener. Puro: no toca la base.

    `servicios` es la lista que mandó quien escribe (o None si no mandó ninguna);
    `actuales`, los que ya tiene registrados.
    """
    grupo = contrato.grupo
    pedidos = None if servicios is None else set(servicios)

    if grupo == grupos.PPA:
        unico = {grupos.subservicio_de_tipo_contrato(contrato.tipo_contrato)}
        if pedidos is not None and pedidos != unico:
            raise ServiciosInvalidos(
                f"Un PPA cubre un solo servicio, el de su tipo_contrato: {sorted(unico)}."
            )
        return unico

    if grupo == grupos.OPERACION:
        unico = {contrato.servicio_aplica}
        if pedidos is not None and pedidos != unico:
            raise ServiciosInvalidos(
                "Un contrato de operación cubre un solo servicio, el de su "
                f"servicio_aplica: {sorted(unico)}."
            )
        return unico

    if grupo == grupos.REPRESENTACION_CGM:
        validos = set(grupos.SUBSERVICIOS[grupos.REPRESENTACION_CGM])
        if pedidos is None:
            return set(actuales or ()) or {grupos.REPRESENTACION}
        if not pedidos or not pedidos <= validos:
            raise ServiciosInvalidos(
                f"Un contrato de representación/CGM cubre {sorted(validos)}, "
                "uno o los dos."
            )
        return pedidos

    raise ServiciosInvalidos(f"Grupo de contrato desconocido: {grupo!r}.")


def registrar(contrato, servicios=None) -> list[str]:
    """Deja en `servicios` exactamente los de este contrato. Devuelve la lista."""
    from apps.contratos.models import Servicio

    actuales = set(Servicio.objects.filter(contrato_id=contrato.pk)
                   .values_list("servicio", flat=True))
    objetivo = deseados(contrato, servicios, actuales)
    sobran = actuales - objetivo
    if sobran:
        Servicio.objects.filter(contrato_id=contrato.pk, servicio__in=sobran).delete()
    faltan = objetivo - actuales
    if faltan:
        Servicio.objects.bulk_create(
            [Servicio(contrato_id=contrato.pk, servicio=s) for s in sorted(faltan)]
        )
    # Lo que se haya precargado ya no vale.
    getattr(contrato, "_prefetched_objects_cache", {}).pop("servicios", None)
    return grupos.en_orden(objetivo)
