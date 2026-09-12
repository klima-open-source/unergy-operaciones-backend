"""Consultas de la vista de pólizas.

**El recurso es el PROYECTO, no la póliza.** `GET /polizas` devuelve una fila por
proyecto vivo, tenga póliza o no (LEFT JOIN), porque la pantalla es el listado de
proyectos con el estado de su póliza al lado. Por eso el identificador de la ruta
es `proyecto_id` y no el id de la póliza, y por eso el PUT hace upsert.
"""

from django.db.models import Prefetch, Q

from apps.contratos import models as ct_models
from apps.fronteras import models as fr_models
from apps.proyectos import models as py_models


def proyectos_con_poliza(search=None, tipo_proyecto=None, poliza_om=None):
    """Proyectos vivos con su info técnica, operador y póliza precargados.

    Los `select_related`/`prefetch_related` no son opcionales: el serializer lee
    `info_tecnica`, `operador_red` y las fronteras con SU operador de red para
    resolver el
    operador de red, y sin precargar son cuatro consultas por fila.
    """
    consulta = (
        py_models.Proyecto.objects.filter(deleted_at__isnull=True)
        .select_related("operador_red")
        .prefetch_related(
            "info_tecnica",
            Prefetch(
                "fronteras",
                queryset=fr_models.Frontera.objects.filter(
                    deleted_at__isnull=True
                ).select_related("operador_red"),
                to_attr="fronteras_vivas",
            ),
            Prefetch(
                "polizas",
                queryset=ct_models.Poliza.objects.all(),
                to_attr="polizas_cargadas",
            ),
        )
    )

    if search:
        consulta = consulta.filter(
            Q(nombre_comercial__icontains=search)
            | Q(municipio__icontains=search)
            | Q(departamento__icontains=search)
        )
    if tipo_proyecto:
        consulta = consulta.filter(tipo_proyecto=tipo_proyecto)
    if poliza_om is not None:
        consulta = consulta.filter(polizas__poliza_om=poliza_om)

    return consulta.order_by("nombre_comercial").distinct()


def build_filas(proyectos):
    """Aplana proyecto + info técnica + póliza en la fila que espera la pantalla.

    Se anota sobre la instancia del proyecto en vez de devolver dicts para que el
    serializer sea un `ModelSerializer` normal y no una traducción a mano campo
    por campo.
    """
    filas = []
    for proyecto in proyectos:
        info = next(iter(proyecto.info_tecnica.all()), None)
        proyecto.info = info
        proyecto.poliza = next(iter(getattr(proyecto, "polizas_cargadas", [])), None)
        # Nombre aparte: `operador_red` es el FK y no acepta un string.
        proyecto.operador_red_nombre = _operador_red_legal(proyecto)
        filas.append(proyecto)
    return filas


def _operador_red_legal(proyecto) -> str | None:
    """El vínculo propio del proyecto y, si no lo tiene, el de su primera frontera VIVA.

    Una frontera borrada no debe seguir prestando su operador: el caso existe
    porque hay proyectos cuyo vínculo aún no se sincronizó.
    """
    if proyecto.operador_red_id and proyecto.operador_red:
        return proyecto.operador_red.nombre_legal
    for frontera in getattr(proyecto, "fronteras_vivas", []):
        if frontera.operador_red_id and frontera.operador_red:
            return frontera.operador_red.nombre_legal
    return None
