"""Qué proyectos tienen reconectador que consultar."""

from apps.contratos.services import plantas
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos import models as py_models


def proyectos_con_relay():
    """Minigranjas en operación, con contrato de operación vigente y con id de SolarView.

    Los cuatro filtros juntos son el criterio: sin `project_id_solarview` no hay
    a qué preguntarle (el estado se lee de SolarView), y una planta sin contrato
    de operación vigente no la operamos nosotros.
    """
    return py_models.Proyecto.objects.filter(
        plantas.filtro_operadas(hoy_col()),
        deleted_at__isnull=True,
        project_id_solarview__isnull=False,
        tipo_proyecto="minigranja",
    )
