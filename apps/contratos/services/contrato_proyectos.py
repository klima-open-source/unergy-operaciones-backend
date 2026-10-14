"""Qué planta cubre cada contrato de servicio, y a qué participación representa.

`contrato_proyectos` tiene una fila por contrato y planta para todos los contratos
(`contratos/0017`). Las de los PPA las escribe `ppa.services.contratos.fijar_proyectos`,
como siempre. Las de los contratos de servicio salen de `contratos.proyecto_id`, que
sigue siendo lo que escriben las pantallas: mientras convivan, `Contrato.save()`
llama a `sincronizar` y la fila se deriva de ahí, igual que hicimos con las partes.

La participación (`proyecto_inversionista`) solo va en los contratos de
representación (Sara, 2026-10-13): el inversionista al que representamos en esa
planta. Se resuelve así, y solo así:

1. la participación de esa planta cuyo cliente es `contratos.inversionista_id`;
2. si el contrato no dice inversionista y la planta tiene UNA sola participación,
   esa;
3. si no, queda vacía y sale como pendiente en `registrar_proyectos_contratos`.
   Nunca se adivina entre varias.
"""
from __future__ import annotations

from apps.contratos.models import ContratoProyecto, GrupoContrato


def participacion_de(contrato):
    """La participación que representa este contrato, o None si no se puede saber."""
    from apps.proyectos.models import ProyectoInversionista

    if contrato.grupo != GrupoContrato.REPRESENTACION_CGM or not contrato.proyecto_id:
        return None
    de_la_planta = ProyectoInversionista.objects.filter(proyecto_id=contrato.proyecto_id)
    if contrato.inversionista_id:
        return de_la_planta.filter(inversionista__cliente_id=contrato.inversionista_id).first()
    candidatas = list(de_la_planta[:2])
    return candidatas[0] if len(candidatas) == 1 else None


def sincronizar(contrato) -> None:
    """Deja la fila de `contrato_proyectos` de un contrato de servicio como dice el contrato.

    Los PPA no se tocan. Si en representación la participación no se puede resolver,
    se conserva la que ya tuviera la fila (si es de la misma planta): una asignada a
    mano no se pierde por volver a guardar el contrato."""
    if contrato.grupo == GrupoContrato.PPA:
        return
    filas = ContratoProyecto.objects.filter(contrato_id=contrato.pk)
    if not contrato.proyecto_id:
        filas.delete()
        return
    filas.exclude(proyecto_id=contrato.proyecto_id).delete()

    participacion = participacion_de(contrato)
    fila = filas.filter(proyecto_id=contrato.proyecto_id).first()
    if fila is None:
        ContratoProyecto.objects.create(
            contrato_id=contrato.pk, proyecto_id=contrato.proyecto_id,
            proyecto_inversionista=participacion,
        )
        return
    if contrato.grupo != GrupoContrato.REPRESENTACION_CGM:
        participacion_id = None
    else:
        participacion_id = participacion.pk if participacion else fila.proyecto_inversionista_id
    if fila.proyecto_inversionista_id != participacion_id:
        fila.proyecto_inversionista_id = participacion_id
        fila.save(update_fields=["proyecto_inversionista"])
