"""Qué plantas tienen un servicio HOY: la regla que reemplaza las banderas `srv_*`.

La regla (Sara, 2026-09-17):

> Una planta tiene un servicio si **existe un contrato de ese servicio y está
> vigente**. Sin el estado del proyecto y sin bandera.

Y la exclusión de comunidades (2026-09-18, aplicada aquí el 2026-10-06): a una planta
en comunidad energética **no se le presta representación ni CGM**, aunque su contrato
siga firmado. La comunidad es una característica del PPA, que llega a la planta por
sus vínculos (`contrato_proyectos`); la resuelve `comunidades.plantas_en_comunidad()`,
la misma que bloquea crear un contrato de representación ahí.

Las tres piezas vienen de un solo lugar cada una:

- **Qué servicios cubre un contrato:** la tabla `servicios` (`services/servicios.py`).
- **Si el contrato rige hoy:** `vigencia` — estado no cerrado y fecha que todavía rige
  para los contratos de servicio; solo la fecha para los PPA, que no tienen estado
  decidido por una persona.
- **A qué plantas llega:** la FK `proyecto` del contrato y, para los PPA, sus vínculos
  en `contrato_proyectos`. Se miran las dos para todo contrato.

Se pide por grupo (`operacion`, `representacion_cgm`, `ppa`) o por subservicio
(`mantenimiento`, `cgm`, `venta`…). "Tiene operación" es tener contrato de
mantenimiento, de arriendo O de internet (`grupos.SUBSERVICIOS`).

Dos formas, según quién pregunte:

- `filtro_proyectos_con()` → condición para un `filter()` de `Proyecto`. Para los
  listados: sondeo, informe FMO, paneles.
- `servicios_por_proyecto()` → `{proyecto_id: {subservicios}}`. Para quien necesita
  todos los servicios de cada planta a la vez (la ficha del proyecto, el pipeline).
"""
from __future__ import annotations

from datetime import date

from django.db.models import Q

from apps.contratos.services import comunidades, grupos, vigencia


def subservicios_de_nombre(servicio: str) -> tuple[str, ...]:
    """Un grupo se expande a sus subservicios; un subservicio queda solo."""
    subs = grupos.SUBSERVICIOS.get(servicio, (servicio,))
    if not set(subs) <= set(grupos.GRUPO_DE_SUBSERVICIO):
        raise ValueError(f"Servicio desconocido: {servicio!r}.")
    return subs


def _contratos_vivos(hoy: date):
    from apps.contratos.models import Contrato

    es_ppa = Q(grupo=grupos.PPA)
    return Contrato.objects.filter(deleted_at__isnull=True).filter(
        (es_ppa & vigencia.filtro_ppa_vivos(hoy))
        | (~es_ppa & vigencia.filtro_vivos(hoy))
    )


def filtro_proyectos_con(servicio: str, hoy: date) -> Q:
    """Las plantas que hoy tienen `servicio`, como condición sobre `Proyecto`."""
    from apps.contratos.models import ContratoProyecto

    subs = subservicios_de_nombre(servicio)
    contratos = _contratos_vivos(hoy).filter(servicios__servicio__in=subs)
    directos = contratos.filter(proyecto__isnull=False).values("proyecto_id")
    vinculados = ContratoProyecto.objects.filter(contrato__in=contratos).values("proyecto_id")
    condicion = Q(pk__in=directos) | Q(pk__in=vinculados)

    if set(subs) & comunidades.EXCLUIDOS_EN_COMUNIDAD:
        condicion &= ~Q(pk__in=comunidades.plantas_en_comunidad(hoy))
    return condicion


def filtro_operadas(hoy: date) -> Q:
    """Las plantas que se monitorean: generan Y las operamos.

    Dos preguntas distintas, cada una con su fuente (decisión del 2026-10-06):

    - **¿Genera?** El estado del proyecto, `en_operacion`. Es la condición técnica
      para sondearla: un contrato de arriendo se firma antes de construir, y sin
      ella esas plantas entrarían al sondeo y darían "sin comunicación" falsas.
    - **¿La operamos?** Un contrato de operación vigente. Reemplaza a
      la bandera de operación del proyecto.

    La usan el sondeo MGS, las alarmas de desconexión, las tarjetas de Generación
    Solar, los reconectadores, el informe de puesta en marcha y los paneles de O&M
    y arriendos. Cada uno le suma sus filtros técnicos (tipo, id de SolarView).
    """
    return filtro_proyectos_con(grupos.OPERACION, hoy) & Q(estado="en_operacion")


def servicios_por_proyecto(hoy: date) -> dict[int, set[str]]:
    """Los subservicios que hoy tiene cada planta con al menos uno. Cuatro consultas."""
    from apps.contratos.models import ContratoProyecto, Servicio

    vivos = _contratos_vivos(hoy)
    salida: dict[int, set[str]] = {}
    filas = Servicio.objects.filter(
        contrato__in=vivos, contrato__proyecto__isnull=False,
    ).values_list("contrato__proyecto_id", "servicio")
    for proyecto_id, sub in filas:
        salida.setdefault(proyecto_id, set()).add(sub)

    por_contrato: dict[int, set[str]] = {}
    for contrato_id, sub in Servicio.objects.filter(
        contrato__in=vivos, contrato__proyectos_vinculados__isnull=False,
    ).values_list("contrato_id", "servicio"):
        por_contrato.setdefault(contrato_id, set()).add(sub)
    for contrato_id, proyecto_id in ContratoProyecto.objects.filter(
        contrato_id__in=por_contrato,
    ).values_list("contrato_id", "proyecto_id"):
        salida.setdefault(proyecto_id, set()).update(por_contrato[contrato_id])

    for proyecto_id in comunidades.plantas_en_comunidad(hoy) & salida.keys():
        salida[proyecto_id] -= comunidades.EXCLUIDOS_EN_COMUNIDAD
    return {pid: subs for pid, subs in salida.items() if subs}
