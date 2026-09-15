"""Tareas programadas del dominio `proyectos`. Ver `apps/energia/tasks.py`."""

import logging

from celery import shared_task

logger = logging.getLogger("operaciones.tareas")


@shared_task(name="proyectos.recalcular_gen_promedio")
def recalcular_gen_promedio() -> str:
    """Recalcula `gen_mensual_promedio_mwh` (ventana móvil de 30 días).

    Antes solo se recalculaba por llamada manual al endpoint, sin scheduler ni
    botón en el frontend que lo disparara: quedaba desactualizado en silencio
    —17 días sin tocar, confirmado el 2026-08-27— mientras las vistas de
    contrato lo seguían tratando como dato confiable.

    `force=False` respeta los valores cargados a mano, mismo criterio que el
    backfill de comercialización, con quien comparte franja horaria. Va 10 min
    antes que ese: ambos leen la misma API de generación de Unergy y separarlos
    evita que compitan por el rate limit en el mismo segundo.
    """
    from apps.proyectos.services import gen_promedio

    res = gen_promedio.recalcular(dry_run=False, force=False)
    if "error" in res:
        logger.error("recálculo de generación promedio falló: %s", res["error"])
        return f"error: {res['error']}"
    resumen = (f"{res['n_actualizados']} actualizados, {res['n_sin_datos']} sin datos, "
               f"{res['n_saltados']} saltados, {res['n_fallidos']} fallidos")
    logger.info("recálculo de generación promedio: %s", resumen)
    return resumen


@shared_task(name="proyectos.sincronizar_tsf")
def sincronizar_tsf() -> str:
    """Trae el pipeline de TSF a la tabla `proyectos`. Cada 6 horas.

    `enrich_dates=True` porque acá sí se pueden pagar las ~99 llamadas HTTP que
    consultan los hitos de cada proyecto: no hay un request esperando del otro
    lado. El botón on-demand corre la versión barata.
    """
    from apps.proyectos.services.tsf_sync import sync_tsf_projects

    stats = sync_tsf_projects(enrich_dates=True)
    resumen = (f"creados={stats.get('creados', 0)} "
               f"actualizados={stats.get('actualizados', 0)} "
               f"errores={stats.get('errores', 0)}")
    logger.info("sincronización de TSF: %s", resumen)
    return resumen


@shared_task(name="proyectos.sincronizar_generacion_diaria")
def sincronizar_generacion_diaria() -> str:
    """Trae de la API de Unergy la generación diaria de los últimos días.

    **El nombre ya no dice la fuente, a proposito.** Se llamaba
    `sincronizar_generacion_solenium` y leia de Solenium; el 2026-09-15 se
    cambio a la API de Unergy, que cubre casi el doble de plantas (~90 con
    `sub_project` contra 57 con id de Solenium) y estaba viva cuando la otra
    llevaba nueve dias sin escribir. Atar el nombre de la tarea a la fuente
    obliga a renombrarla --y a tocar el horario y `django_celery_beat`-- cada
    vez que la fuente cambia.

    Ventana movil y no solo ayer: la API corrige hacia atras, y un dia que llego
    incompleto se completa en una corrida posterior sin intervencion.

    El UPSERT solo pisa filas cuya `fuente` ya es la suya: un valor cargado a
    mano, o traido de Solenium cuando esa sincronizacion corria, manda sobre
    este. Es el criterio de toda la tabla.
    """
    from apps.proyectos.services.generacion_unergy import sincronizar

    stats = sincronizar()
    resumen = (f"{stats['filas']} días de {stats['proyectos']} proyectos")
    logger.info("sincronización de generación desde Solenium: %s", resumen)
    return resumen
