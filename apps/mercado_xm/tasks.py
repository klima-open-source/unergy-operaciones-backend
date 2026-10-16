"""Tareas programadas del dominio `mercado_xm`. Ver `apps/energia/tasks.py`."""

import logging

from celery import shared_task

logger = logging.getLogger("operaciones.tareas")


@shared_task(name="mercado_xm.ingesta_bolsa")
def ingesta_bolsa() -> str:
    """Trae de EVO los precios de bolsa del día y los persiste.

    Descarta el dato rancio: si EVO reporta más de dos días de atraso, guardar
    ese precio como si fuera el de hoy es peor que no tener ninguno.
    """
    from apps.mercado_xm.services import evo

    try:
        datos = evo.get("/dailyspot/latest")
    except evo.EvoNoConfigurado:
        logger.info("EVO_API_URL sin configurar — ingesta de bolsa omitida")
        return "omitida: EVO sin configurar"

    fecha = datos.get("date")
    if not fecha:
        logger.warning("EVO respondió sin fecha — no se persiste nada")
        return "omitida: respuesta sin fecha"
    atraso = datos.get("stale_days", 0)
    if atraso > 2:
        logger.warning("dato de bolsa rancio: %s (%s días) — no se persiste", fecha, atraso)
        return f"omitida: dato de {fecha}, {atraso} días de atraso"

    evo.guardar_dailyspot(datos)
    return f"precios de bolsa de {fecha} persistidos"

