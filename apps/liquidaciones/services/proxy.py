"""Lo que el proxy de Liquidaciones cruza con ESTA base.

El cliente HTTP está en `api_externa.py` (portado tal cual). Acá va lo que
necesita la base local: el tópico con el que la API externa conoce cada planta y
el nombre con el que el equipo la conoce.
"""

from apps.proyectos import models as py_models


def topico(proyecto) -> str | None:
    """El tópico con el que la API de Liquidaciones conoce esta planta.

    Manda `topico_liquidaciones` cuando está: hay plantas que los dos sistemas
    de Unergy nombran distinto, y consultar generación con el tópico equivocado
    devuelve cero registros. Por eso no se pueden unificar.
    """
    return proyecto.topico_liquidaciones or proyecto.sub_project


def nombres_por_topico() -> dict[str, str]:
    """Nombre comercial de esta base, indexado por el tópico de la API externa.

    La API identifica los proyectos por `nombre_topico`; en pantalla se muestra
    el nombre con el que el equipo los conoce.
    """
    filas = py_models.Proyecto.objects.filter(
        deleted_at__isnull=True
    ).values_list("sub_project", "topico_liquidaciones", "nombre_comercial")
    return {
        (liquidaciones or sub): nombre
        for sub, liquidaciones, nombre in filas
        if (liquidaciones or sub)
    }


def proyectos_por_topico() -> dict[str, dict]:
    """Igual que :func:`nombres_por_topico`, pero además con el `id`.

    Quien cruza cifras necesita el id para agrupar (dos tópicos distintos pueden
    ser el mismo proyecto) y el nombre para mostrarlo.
    """
    filas = py_models.Proyecto.objects.filter(
        deleted_at__isnull=True
    ).values_list("id", "sub_project", "topico_liquidaciones", "nombre_comercial")
    return {
        (liquidaciones or sub): {"id": pk, "nombre": nombre}
        for pk, sub, liquidaciones, nombre in filas
        if (liquidaciones or sub)
    }


def proyectos_vivos():
    return py_models.Proyecto.objects.filter(
        deleted_at__isnull=True
    ).order_by("nombre_comercial")


def fila_de_proyecto(proyecto, datos: dict) -> dict:
    """Proyecto local + su configuración en la API externa, en una fila."""
    from apps.liquidaciones.services import api_externa

    return {
        "proyecto_id": proyecto.id,
        "nombre_comercial": proyecto.nombre_comercial,
        "tipo_proyecto": proyecto.tipo_proyecto,
        "estado": proyecto.estado,
        "nombre_topico": topico(proyecto),
        "en_api": bool(datos),
        **{campo: datos.get(campo) for campo in api_externa.CAMPOS_PROYECTO},
        "subproyectos": datos.get("subproyectos") or [],
    }
