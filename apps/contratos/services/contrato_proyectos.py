"""Qué planta cubre cada contrato de servicio, y a qué participación representa.

`contrato_proyectos` tiene una fila por contrato y planta para todos los contratos
(`contratos/0017`). Las de los PPA las escribe `ppa.services.contratos.fijar_proyectos`.
Las de los contratos de servicio las escribe `Contrato.save()` llamando a
`sincronizar` con lo que se asignó a los atajos `contrato.proyecto_id` /
`contrato.inversionista_id` (que desde el paso 4b ya no son columnas).

La participación (`proyecto_inversionista`) solo va en los contratos de
representación (Sara, 2026-10-13): el inversionista al que representamos en esa
planta.

- Si se asigna un inversionista, es su participación en la planta; si no participa
  en ella, `InversionistaFueraDeLaPlanta`: no se inventa una participación (el %
  y las fechas son de la planta, no del contrato).
- Si no se asigna y la planta tiene UNA sola participación, esa.
- Si no, queda la que tuviera (si sigue siendo de la planta) o vacía. Nunca se
  adivina entre varias.
"""
from __future__ import annotations

from django.db.models import Prefetch

from apps.contratos.models import _SIN_CAMBIO, ContratoProyecto, GrupoContrato

#: Para un listado: `Contrato.objects.prefetch_related(CON_PLANTA)` trae la planta y
#: el inversionista de cada contrato en una consulta, en vez de una por contrato.
CON_PLANTA = Prefetch(
    "proyectos_vinculados",
    queryset=ContratoProyecto.objects.select_related(
        "proyecto", "proyecto_inversionista__inversionista__cliente",
    ),
)

#: Lookups de `Contrato` para filtrar por planta o por inversionista (cliente).
PLANTA = "proyectos_vinculados__proyecto"
INVERSIONISTA = "proyectos_vinculados__proyecto_inversionista__inversionista__cliente"


class InversionistaFueraDeLaPlanta(ValueError):
    """El inversionista asignado al contrato no tiene participación en esa planta."""


def participacion_en(proyecto_id, cliente_id):
    """La participación de ese cliente en esa planta, o None."""
    from apps.proyectos.models import ProyectoInversionista

    if not proyecto_id or not cliente_id:
        return None
    return ProyectoInversionista.objects.filter(
        proyecto_id=proyecto_id, inversionista__cliente_id=cliente_id,
    ).first()


def validar_inversionista(proyecto_id, cliente_id) -> None:
    """Lanza `InversionistaFueraDeLaPlanta` si el cliente no participa en la planta."""
    if proyecto_id and cliente_id and participacion_en(proyecto_id, cliente_id) is None:
        raise InversionistaFueraDeLaPlanta(
            "El inversionista no tiene participación en esa planta: regístralo primero "
            "en los inversionistas del proyecto."
        )


def _unica_de(proyecto_id):
    from apps.proyectos.models import ProyectoInversionista

    candidatas = list(ProyectoInversionista.objects.filter(proyecto_id=proyecto_id)[:2])
    return candidatas[0] if len(candidatas) == 1 else None


def sincronizar(contrato, *, proyecto_id=_SIN_CAMBIO, inversionista_id=_SIN_CAMBIO) -> None:
    """Deja la fila de `contrato_proyectos` de un contrato de servicio como se pidió.

    `proyecto_id` / `inversionista_id` son lo asignado a los atajos; `_SIN_CAMBIO`
    si no se tocaron. Los PPA no se tocan."""
    if contrato.grupo == GrupoContrato.PPA:
        return
    filas = ContratoProyecto.objects.filter(contrato_id=contrato.pk)
    fila = filas.first()
    if proyecto_id is _SIN_CAMBIO:
        proyecto_id = fila.proyecto_id if fila else None
    if not proyecto_id:
        filas.delete()
        return

    es_representacion = contrato.grupo == GrupoContrato.REPRESENTACION_CGM
    if not es_representacion:
        participacion_id = None
    elif inversionista_id is not _SIN_CAMBIO and inversionista_id is not None:
        validar_inversionista(proyecto_id, inversionista_id)
        participacion_id = participacion_en(proyecto_id, inversionista_id).pk
    else:
        # Sin inversionista nuevo: el que ya representaba, si está en esta planta.
        anterior = (fila.proyecto_inversionista if fila and inversionista_id is _SIN_CAMBIO
                    else None)
        if anterior is not None and anterior.proyecto_id != proyecto_id:
            anterior = participacion_en(proyecto_id, anterior.inversionista.cliente_id)
        if anterior is None:
            anterior = _unica_de(proyecto_id)
        participacion_id = anterior.pk if anterior else None

    if fila is None:
        ContratoProyecto.objects.create(
            contrato_id=contrato.pk, proyecto_id=proyecto_id,
            proyecto_inversionista_id=participacion_id,
        )
        return
    filas.exclude(pk=fila.pk).delete()
    if (fila.proyecto_id, fila.proyecto_inversionista_id) != (proyecto_id, participacion_id):
        fila.proyecto_id = proyecto_id
        fila.proyecto_inversionista_id = participacion_id
        fila.save(update_fields=["proyecto", "proyecto_inversionista"])
