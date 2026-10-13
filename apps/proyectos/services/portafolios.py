"""Portafolios: las capas con las que se agrupan los proyectos.

Un portafolio agrupa proyectos vía `proyectos.portafolio_id` y es la fuente de
verdad del agrupamiento que usan los informes «Por portafolio». Se siembra UNA
vez desde el agrupamiento por inversionista para no perder la relación que ya
existía; de ahí en adelante se gestiona a mano (drag-and-drop en el frontend).

Vive en el dominio y no en la API porque `agrupamiento()` lo necesita también el
generador de informes, no solo esta pantalla.

**El matching de nombres es difuso, no exacto** (`apps/comun/nombre_matching`):
los nombres de portafolio son razones sociales de clientes, así que
"FONSAR S.A.S." y "Fonsar SAS" tienen que reconocerse como el mismo y no acabar
en dos capas distintas.
"""

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.comun.nombre_matching import mejor_candidato
from apps.contratos.services import plantas
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos import models as py_models


class SiembraConcurrente(RuntimeError):
    """Otra petición estaba sembrando los portafolios al mismo tiempo."""


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


def sembrar_si_esta_vacio() -> None:
    """Crea los portafolios desde el primer inversionista de cada proyecto.

    Idempotente por diseño: solo corre cuando la tabla está VACÍA, así no pisa
    ninguna asignación manual posterior.
    """
    if py_models.Portafolio.objects.exists():
        return

    proyectos = (
        py_models.Proyecto.objects
        .filter(operativos_q(), deleted_at__isnull=True, portafolio__isnull=True)
        .prefetch_related("inversionistas__cliente")
    )

    creados: list = []
    try:
        with transaction.atomic():
            for proyecto in proyectos:
                nombre = _nombre_del_inversionista(proyecto)
                if not nombre:
                    continue
                portafolio, _score = mejor_candidato(
                    nombre, [(pt, [pt.nombre]) for pt in creados]
                )
                if portafolio is None:
                    portafolio = py_models.Portafolio.objects.create(nombre=nombre)
                    creados.append(portafolio)
                proyecto.portafolio = portafolio
                proyecto.save(update_fields=["portafolio"])
    except IntegrityError as exc:
        # Carrera: dos peticiones casi simultáneas viendo la tabla vacía. El
        # UNIQUE del nombre evita el duplicado, pero sin esto la segunda
        # transacción reventaba con un IntegrityError crudo en vez de un 409.
        raise SiembraConcurrente(
            "No se pudo sembrar los portafolios iniciales: otra solicitud ya lo "
            "estaba haciendo. Reintenta."
        ) from exc


def _nombre_del_inversionista(proyecto) -> str | None:
    for vinculo in proyecto.inversionistas.all():
        if vinculo.cliente and vinculo.cliente.razon_social_nombre:
            return vinculo.cliente.razon_social_nombre
    return None


def agrupamiento() -> dict[str, list[str]]:
    """`{nombre de portafolio: [nombre comercial, …]}` para el wizard de informes.

    Siembra si hace falta y solo considera portafolios ACTIVOS y proyectos
    operativos.
    """
    sembrar_si_esta_vacio()
    proyectos = (
        py_models.Proyecto.objects
        .filter(operativos_q(), deleted_at__isnull=True, portafolio__activo=True)
        .select_related("portafolio")
    )
    salida: dict[str, list[str]] = {}
    for proyecto in proyectos:
        nombres = salida.setdefault(proyecto.portafolio.nombre, [])
        if proyecto.nombre_comercial not in nombres:
            nombres.append(proyecto.nombre_comercial)
    return salida
