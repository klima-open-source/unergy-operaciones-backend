"""¿Ya existe este contrato? El aviso que faltaba al crear.

Producción acumuló el mismo contrato escrito por tres fuentes distintas y
ninguna reconocía a las otras: MGS Naos 2 llegó a tener **tres filas** siendo un
solo contrato (`representacion_dedup`). Para eso se construyó un informe de
duplicados y una fusión manual, pero nada impedía crear el cuarto: el alta no
miraba si ya había uno igual.

Clientes, proyectos y fronteras sí avisan desde hace tiempo. Esto pone a los
contratos en el mismo sitio, con el mismo contrato de API: 409 con el candidato,
y `?forzar=true` para crear de todos modos. **Avisa, no bloquea** --hay razones
reales para dos contratos parecidos, y una regla que se equivoque no puede dejar
a nadie sin poder registrar lo que de verdad existe--.

## Qué cuenta como duplicado

Un contrato **vivo**, de la **misma planta**, del **mismo grupo de servicio**, y
del **mismo inversionista** cuando ambos lo nombran.

Las tres condiciones salen de definiciones que ya existían, no de criterios
nuevos: la vigencia de `vigencia.filtro_vivos`, el grupo de `grupos`, y la
comparación de inversionista de `representacion_dedup.norm`, que es la que ya se
usa para agrupar los duplicados que hay.

El **grupo** y no el subservicio: representación y CGM viajan en el mismo
contrato, así que un segundo "contrato de CGM" en una planta que ya tiene
representación es la forma exacta en que nacieron los duplicados de producción.
En cambio mantenimiento, arriendo e internet son contratos distintos de un mismo
grupo... y por eso Operación se compara por subservicio: dos contratos de
arriendo en una planta sí es sospechoso, pero un arriendo y un internet no.

El **inversionista** desempata las minigranjas: ahí hay un contrato de
representación POR inversionista, y sin esta condición cada uno avisaría contra
el anterior. Si a alguno de los dos le falta el inversionista no se puede saber
a cuál pertenece --el mismo problema que `representacion_dedup` resuelve
dejándolos aparte-- y entonces se avisa: es preferible una pregunta de más a
otro Naos 2.

Una renovación creada mientras el anterior sigue vigente también avisa. Es
deliberado (decidido con Sara el 2026-09-19): el aviso dice algo cierto --"ya hay
uno vigente acá"-- y cuesta un clic.
"""

from datetime import date

from apps.contratos.models import ContratoServicio
from apps.contratos.services import grupos, vigencia
from apps.contratos.services.representacion_dedup import norm

#: Los grupos cuyos contratos se comparan por SUBSERVICIO y no por grupo. En
#: Operación cada subservicio es su propio contrato --mantenimiento, arriendo e
#: internet conviven en la misma planta-- así que comparar por grupo avisaría
#: cada vez que se agrega el segundo servicio de una planta.
POR_SUBSERVICIO = frozenset({grupos.OPERACION})


def _mismo_inversionista(nuevo_id, nuevo_nombre, existente) -> bool:
    """Si los dos contratos hablan del mismo inversionista.

    Devuelve True también cuando no se puede saber --a alguno le falta el dato--
    para que el aviso salga: es el caso que dejó tres filas de Naos 2.
    """
    if nuevo_id and existente.inversionista_id:
        return nuevo_id == existente.inversionista_id

    izq, der = norm(nuevo_nombre), norm(existente.inversionista_nombre)
    if not izq or not der:
        return True
    return izq == der


def buscar_duplicado(
    *,
    proyecto_id: int | None,
    servicio_aplica: str | None,
    hoy: date,
    inversionista_id: int | None = None,
    inversionista_nombre: str | None = None,
    excluir_id: int | None = None,
):
    """El contrato vivo que ya cubre esto, o `None`.

    Sin planta no se compara nada: un contrato sin `proyecto_id` no se puede
    situar, y hay 10 así en producción. Tampoco se compara si el servicio no se
    reconoce.
    """
    if proyecto_id is None or not servicio_aplica:
        return None

    grupo = grupos.grupo_de(servicio_aplica)
    if grupo is None:
        return None

    candidatos = ContratoServicio.objects.filter(
        vigencia.filtro_vivos(hoy),
        proyecto_id=proyecto_id,
        proyecto__deleted_at__isnull=True,
    )
    if excluir_id:
        candidatos = candidatos.exclude(pk=excluir_id)

    for existente in candidatos:
        if grupo in POR_SUBSERVICIO:
            if existente.servicio_aplica != servicio_aplica:
                continue
        elif grupos.grupo_de_contrato(existente) != grupo:
            continue

        if not _mismo_inversionista(inversionista_id, inversionista_nombre, existente):
            continue
        return existente
    return None


def descripcion(contrato) -> str:
    """Cómo se nombra el contrato en el aviso, con lo que tenga."""
    return (
        contrato.numero_contrato
        or contrato.inversionista_nombre
        or contrato.nombre_proyecto_ref
        or f"Contrato {contrato.id}"
    )
