"""Tareas programadas del dominio `monitoreo`. Ver `apps/energia/tasks.py`."""

import logging

from celery import shared_task

logger = logging.getLogger("operaciones.tareas")


@shared_task(name="monitoreo.sondeo_mgs")
def sondeo_mgs() -> dict:
    """Un ciclo de monitoreo: evalúa los nodos de Quoia y persiste alarmas.

    Cada 15 min. Incluye las alarmas de desconexión (inversores contra medidor),
    aisladas para que un fallo suyo no tumbe el ciclo.

    **Una sola corrida a la vez, y siempre en el mismo proceso.** El motor
    recuerda entre ciclos qué alarmas están activas, y de ahí sale cuáles se
    superaron; dos corridas solapadas se pisarían ese estado. Con un solo worker
    de Celery se cumple — ver el docstring de `alarmas/sondeo.py` para qué hacer
    si algún día hay varios.
    """
    from apps.monitoreo.services.alarmas.sondeo import sondear

    try:
        return sondear()
    except Exception:
        # Se traga la excepción a propósito: el ciclo vuelve en 15 min y un
        # reintento inmediato solo repetiría la misma llamada a una API caída.
        logger.exception("el ciclo de monitoreo falló")
        return {"error": "el ciclo falló, ver el log"}


@shared_task(name="monitoreo.avisar_fallas_programadas")
def avisar_fallas_programadas() -> dict:
    """Avisa en la campana de las fallas programadas para hoy. Una vez al día;
    reintentarla no duplica avisos (`avisos.programadas_hoy` es idempotente)."""
    from apps.monitoreo.services.fallas import avisos

    return {"avisos": avisos.programadas_hoy()}
