"""Portafolios: las capas con las que se agrupan los proyectos.

Un portafolio agrupa plantas vía `portafolio_proyectos`, y una planta puede estar
en varios. Es la fuente de verdad del agrupamiento que usan los informes «Por
portafolio». Se gestiona a mano desde el frontend. (La siembra inicial desde los
inversionistas se retiró: la tabla nunca vuelve a estar vacía.)

Vive en el dominio y no en la API porque `agrupamiento()` lo necesita también el
generador de informes, no solo esta pantalla.

**El matching de nombres es difuso, no exacto** (`apps/comun/nombre_matching`):
los nombres de portafolio son razones sociales de clientes, así que
"FONSAR S.A.S." y "Fonsar SAS" tienen que reconocerse como el mismo y no acabar
en dos capas distintas.
"""

from django.db import transaction
from django.db.models import Q

from apps.comun.nombre_matching import mejor_candidato
from apps.contratos.services import plantas
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos import models as py_models


def operativos_q() -> Q:
    """Criterio de proyecto operativo: en operación, o con contrato de operación
    vigente aunque todavía no lo esté. Escrito UNA vez: lo usan tres consultas."""
    return plantas.filtro_proyectos_con("operacion", hoy_col()) | Q(estado="en_operacion")


def ids_operativos() -> set[int]:
    """Los ids que cumplen `operativos_q()`, en UNA consulta: para quien recorre
    todos los proyectos y pregunta por cada uno."""
    return set(
        py_models.Proyecto.objects.filter(operativos_q()).values_list("id", flat=True)
    )


def parecido(nombre: str, excluir_id: int | None = None):
    """Un portafolio con nombre PARECIDO. Solo avisa; no bloquea."""
    consulta = py_models.Portafolio.objects.all()
    if excluir_id is not None:
        consulta = consulta.exclude(pk=excluir_id)
    item, _score = mejor_candidato(
        nombre, [(pt, [pt.nombre]) for pt in consulta]
    )
    return item


def aviso_de_parecido(portafolio) -> dict:
    """Cuerpo del 409 que el frontend usa para ofrecer «crear de todos modos».

    Mismas claves que el aviso de nombre parecido de proyectos y operadores.
    """
    return {
        "mensaje": (
            f"Ya existe un portafolio con un nombre muy parecido: "
            f"'{portafolio.nombre}'."
        ),
        "duplicado_nombre": True,
        "candidato_id": portafolio.id,
        "candidato_nombre": portafolio.nombre,
    }


def poner(proyecto_id: int, portafolio_id: int) -> None:
    """Mete la planta en el portafolio. Idempotente."""
    py_models.PortafolioProyecto.objects.get_or_create(
        proyecto_id=proyecto_id, portafolio_id=portafolio_id
    )


def quitar(proyecto_id: int, portafolio_id: int) -> None:
    """Saca la planta del portafolio; las demás pertenencias quedan."""
    py_models.PortafolioProyecto.objects.filter(
        proyecto_id=proyecto_id, portafolio_id=portafolio_id
    ).delete()


def mover(proyecto_id: int, portafolio_id: int | None) -> None:
    """Deja la planta SOLO en ese portafolio (`None` = en ninguno).

    Es lo que hacía `asignar` cuando una planta tenía un único portafolio. Queda
    para el frontend anterior, que arrastra la planta de una capa a otra, mientras
    se promueve el nuevo.
    """
    with transaction.atomic():
        py_models.PortafolioProyecto.objects.filter(proyecto_id=proyecto_id).exclude(
            portafolio_id=portafolio_id
        ).delete()
        if portafolio_id is not None:
            poner(proyecto_id, portafolio_id)


def agrupamiento() -> dict[str, list[str]]:
    """`{nombre de portafolio: [nombre comercial, …]}` para el wizard de informes.

    Solo plantas operativas. Una planta que está en dos portafolios sale en los dos.
    """
    filas = (
        py_models.PortafolioProyecto.objects
        .filter(proyecto__in=py_models.Proyecto.objects.filter(
            operativos_q(), deleted_at__isnull=True))
        .select_related("portafolio", "proyecto")
        .order_by("portafolio__nombre", "proyecto__nombre_comercial")
    )
    salida: dict[str, list[str]] = {}
    for fila in filas:
        nombres = salida.setdefault(fila.portafolio.nombre, [])
        if fila.proyecto.nombre_comercial not in nombres:
            nombres.append(fila.proyecto.nombre_comercial)
    return salida
