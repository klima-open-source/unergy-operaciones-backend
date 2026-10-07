"""Consultas de contratos de servicio."""

import re

from django.db.models import Q

from apps.contratos import models as ct_models
from apps.contratos.services import contrato_partes
from apps.proyectos import models as py_models

LIMITE_MAXIMO = 500
# Los nombres de proyecto traen su código de cuatro dígitos: "MGS 0010 -
# Villanueva" → "0010". Es el respaldo cuando el contrato no tiene ni
# `proyecto_id` ni código Sun Factory.
CODIGO_EN_NOMBRE = re.compile(r"\d{4}")


def con_relaciones():
    """Base del listado y del detalle.

    Los `prefetch_related` no son opcionales: `enlace_drive` es una propiedad
    que recorre los documentos comerciales en cada fila, y el nombre del
    proyecto se pinta en todas. Con 112 contratos de representación, sin esto
    son 112 consultas por relación.
    """
    return (
        ct_models.ContratoServicio.objects
        .select_related("proyecto", "inversionista")
        .prefetch_related(contrato_partes.CON_PARTES)
        .prefetch_related(
            "contrato_frontera_por_contrato_servicio_id__frontera",
            "cliente_documentos_comerciales_por_contrato_servicio_id",
            "servicios",
        )
    )


def listar(tipo=None, proyecto_id=None, codigo_tsf=None, limite=LIMITE_MAXIMO):
    consulta = con_relaciones()
    if tipo:
        consulta = consulta.filter(servicio_aplica=tipo)

    if proyecto_id:
        # Tres formas de que un contrato pertenezca a un proyecto, en orden de
        # fiabilidad: el FK, el código Sun Factory y —último recurso— el código
        # de cuatro dígitos dentro del nombre de referencia, que es texto libre.
        criterio = Q(proyecto_id=proyecto_id)
        if codigo_tsf:
            criterio |= Q(codigo_sun_factory=codigo_tsf)
        nombre = (
            py_models.Proyecto.objects.filter(pk=proyecto_id)
            .values_list("nombre_comercial", flat=True).first()
        )
        for codigo in CODIGO_EN_NOMBRE.findall(nombre or ""):
            criterio |= Q(nombre_proyecto_ref__icontains=codigo)
        consulta = consulta.filter(criterio)
    elif codigo_tsf:
        consulta = consulta.filter(codigo_sun_factory=codigo_tsf)

    return consulta.order_by("-fecha_inicio", "-id")[:limite]
