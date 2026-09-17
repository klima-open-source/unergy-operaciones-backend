"""Consulta de servicios: trae contratos de servicio y PPA en la forma común.

Separado de `unificado.py` a propósito: ahí va la FORMA (funciones puras sobre
objetos, probables sin base) y acá las CONSULTAS. Ver `docs/SERVICIOS_AGRUPACION.md`.

El filtro es opcional. Sin parámetros devuelve todo con un tope, que es lo que
necesita la vista Servicios general —muestra todas las filas y deja que el
usuario filtre en el navegador—; con `proyecto_id` o `cliente_id` devuelve solo
lo de esa planta o ese cliente, que es lo que piden la pestaña de un proyecto y
el panel 360.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Q

from apps.clientes.services.vistas import _enlace_documento
from apps.contratos.models import ContratoServicio
from apps.contratos.services import unificado
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato

#: Mismo tope que `api/v1/contratos_servicio`: la vista general los pinta todos.
LIMITE_MAXIMO = 500

#: La relación de documentos comerciales, que `_enlace_documento` recorre. Se
#: precarga siempre: sin esto es una consulta por fila.
_DOCS_SERVICIO = "cliente_documentos_comerciales_por_contrato_servicio_id"


def _vigente(proyecto):
    """El proyecto, o `None` si fue borrado lógicamente.

    `proyectos.deleted_at` es borrado lógico y la fila sigue ahí, así que un
    `select_related` la trae igual. El resto del sistema las omite --
    `apps/clientes/services/vistas.py::_proyectos_por_id` filtra
    `deleted_at__isnull=True` y deja el contrato sin planta-- y acá se hace lo
    mismo: un contrato cuya planta se borró sale como huérfano, no mostrando
    una planta que ya no existe.
    """
    return proyecto if proyecto is not None and proyecto.deleted_at is None else None


def _contratos_servicio(proyecto_id: int | None, cliente_id: int | None,
                        plantas_del_cliente: set[int]):
    consulta = (
        ContratoServicio.objects
        .select_related("proyecto")
        .prefetch_related(_DOCS_SERVICIO)
    )
    if proyecto_id:
        return consulta.filter(proyecto_id=proyecto_id)
    if cliente_id:
        # Las tres formas en que un contrato es "de" un cliente. El camino por
        # planta no es opcional: `contratante_id`/`prestador_id` casi nunca se
        # pueblan --el campo del wizard es texto libre-- y sin él un cliente con
        # contratos reales sale vacío. Mismo criterio que
        # `apps/clientes/services/vistas.py::_contratos_del_cliente`.
        criterio = (
            Q(contratante_id=cliente_id) | Q(prestador_id=cliente_id)
            | Q(inversionista_id=cliente_id)
        )
        if plantas_del_cliente:
            criterio |= Q(proyecto_id__in=plantas_del_cliente)
        return consulta.filter(criterio)
    return consulta


def _contratos_ppa(proyecto_id: int | None, cliente_id: int | None):
    consulta = (
        PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .prefetch_related("documentos_comerciales", "proyectos_vinculados__proyecto")
    )
    if proyecto_id:
        return consulta.filter(proyectos_vinculados__proyecto_id=proyecto_id)
    if cliente_id:
        return consulta.filter(
            Q(comprador_id=cliente_id) | Q(vendedor_id=cliente_id)
        )
    return consulta


def listar(proyecto_id: int | None = None, cliente_id: int | None = None,
           hoy: date | None = None, limite: int = LIMITE_MAXIMO) -> list[dict]:
    """Servicios en la forma común: contratos de servicio y PPA juntos.

    El tope se aplica a cada fuente por separado, no al total: cortar el
    conjunto mezclado dejaría fuera todos los PPA si hubiera muchos contratos
    de servicio.
    """
    hoy = hoy or hoy_col()

    plantas_del_cliente: set[int] = set()
    if cliente_id and not proyecto_id:
        from apps.clientes.services.panel import proyectos_por_cliente

        plantas_del_cliente = proyectos_por_cliente({cliente_id}).get(cliente_id, set())

    servicios = [
        unificado.desde_contrato_servicio(
            contrato, hoy,
            proyecto=_vigente(contrato.proyecto),
            enlace=_enlace_documento(getattr(contrato, _DOCS_SERVICIO).all()),
        )
        for contrato in _contratos_servicio(
            proyecto_id, cliente_id, plantas_del_cliente
        ).order_by("-fecha_inicio", "-id")[:limite]
    ]

    servicios += [
        unificado.desde_ppa(
            contrato, hoy,
            proyectos=[
                p for p in (
                    _vigente(v.proyecto)
                    for v in contrato.proyectos_vinculados.all()
                ) if p is not None
            ],
            enlace=_enlace_documento(contrato.documentos_comerciales.all()),
        )
        # `distinct` porque filtrar por `proyectos_vinculados__proyecto_id`
        # atraviesa la M2M y repetiría el contrato por cada planta que empata.
        for contrato in _contratos_ppa(proyecto_id, cliente_id)
        .distinct().order_by("-fecha_inicio", "-id")[:limite]
    ]

    return servicios


def agrupados(proyecto_id: int | None = None, cliente_id: int | None = None,
              hoy: date | None = None, limite: int = LIMITE_MAXIMO) -> list[dict]:
    """Lo que devuelve `GET /api/v1/servicios`: los tres grupos con sus conteos."""
    return unificado.agrupar(
        listar(proyecto_id=proyecto_id, cliente_id=cliente_id,
               hoy=hoy, limite=limite)
    )
