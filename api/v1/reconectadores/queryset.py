"""Qué proyectos tienen reconectador que consultar."""

from apps.proyectos import models as py_models


def proyectos_con_relay():
    """Minigranjas en operación, con servicio de O&M y con id de SolarView.

    Los cuatro filtros juntos son el criterio: sin `project_id_solarview` no hay
    a qué preguntarle (el estado se lee de SolarView), y una planta sin
    `srv_operacion` no la operamos nosotros.
    """
    return py_models.Proyecto.objects.filter(
        estado="en_operacion",
        project_id_solarview__isnull=False,
        tipo_proyecto="minigranja",
        srv_operacion=True,
    )
