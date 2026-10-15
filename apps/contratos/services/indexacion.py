"""Carga masiva de las filas de indexación de O&M.

Recibe `[{proyecto, filas: [{anio, ipc_aplicado, valor}]}]` y las guarda en el
JSONB del contrato de mantenimiento de cada proyecto. El proyecto se resuelve
por NOMBRE, que es como viene del Excel.
"""

from django.db import transaction

from apps.comun import proyecto_matching
from apps.contratos import models as ct_models

CAMPO_POR_TIPO = {
    "anual": "indexacion_anual",
    "mensual": "indexacion_mensual",
}
SERVICIO_OM = "mantenimiento"


@transaction.atomic
def importar(tipo: str, entradas: list[dict]) -> dict:
    """Devuelve `{actualizados, no_encontrados}` con nombres, no ids.

    Un proyecto que no se encuentra —o que no tiene contrato de
    mantenimiento— se reporta en vez de fallar: la carga viene de un Excel con
    decenas de filas y no debe abortarse entera por una.
    """
    campo = CAMPO_POR_TIPO[tipo]
    actualizados, no_encontrados = [], []

    for entrada in entradas:
        nombre = (entrada["proyecto"] or "").strip()
        proyecto = proyecto_matching.buscar_por_nombre(nombre)
        if proyecto is None:
            no_encontrados.append(nombre)
            continue

        contrato = ct_models.ContratoServicio.objects.filter(
            proyectos_vinculados__proyecto=proyecto, servicio_aplica=SERVICIO_OM
        ).first()
        if contrato is None:
            no_encontrados.append(nombre)
            continue

        setattr(contrato, campo, list(entrada["filas"]))
        contrato.save(update_fields=[campo])
        actualizados.append(proyecto.nombre_comercial or nombre)

    return {"actualizados": actualizados, "no_encontrados": no_encontrados}
