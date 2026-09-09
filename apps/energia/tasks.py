"""Tareas programadas del dominio `energia`.

Puerto de los jobs del `BackgroundScheduler` que vivían DENTRO del proceso web
de FastAPI (`app/main.py`). Ese acoplamiento es la razón de `WORKERS=1`: con más
de un worker de uvicorn cada uno arrancaba su propio scheduler y los jobs
corrían duplicados. Con el worker de Celery como servicio aparte esa
restricción desaparece.

**El horario no vive acá.** Cada tarea dice QUÉ hace; CUÁNDO lo hace está en
`config/horarios.py`, que siembra `django_celery_beat`. Separarlos permite
cambiar una franja sin tocar el código, y deja los horarios en un solo archivo
en vez de repartidos por 19 decoradores.

Las tareas no reintentan: todas son idempotentes y vuelven a correr en su
próxima franja. Un reintento automático sobre una que lee un buzón IMAP o
consulta una API con rate limit hace más daño que esperar.
"""

import logging
from datetime import timedelta

from celery import shared_task

from apps.plataforma.services.fechas import hoy_col

logger = logging.getLogger("operaciones.tareas")


@shared_task(name="energia.reporte_diario")
def reporte_diario() -> str:
    """Clasifica el Reporte de Energía (Generación + Consumo) del día anterior.

    A las 3:30am hora Bogotá el reporte CGM de Quoia ya suele estar asentado: el
    de un día llega completo entre las 9 y las 10am del día siguiente. Se
    adelantó de 4:00 a 3:30 el 2026-08-21 porque la corrida del 20-ago tardó más
    de 45 min sin terminar (vs. 23-50 min los diez días previos), y hacía falta
    media hora más de margen antes de que alguien lo revise en la mañana.

    `ejecutar_dia_background` maneja su propia sesión, su logging y su registro
    de últimas corridas — el mismo mecanismo que usa `POST /reporte-energia/ejecutar`.

    **Sin `cancelable`, a propósito.** Esta corrida no consulta la bandera de
    "Detener" en ninguna de sus ~100 iteraciones: nada externo la puede parar,
    igual que cuando ese estado era un diccionario en la memoria de este mismo
    proceso. Lo que sí comparte con la manual es el registro del resultado, que
    va a la caché — es lo que permite que la pantalla muestre en la mañana si
    esta corrida terminó con fronteras fallidas, en vez de que quede solo en
    estos logs. Para pararla: `docker compose restart worker`.
    """
    from apps.energia.services.reporte.orquestador import ejecutar_dia_background

    fecha = hoy_col() - timedelta(days=1)
    ejecutar_dia_background(fecha)
    return f"reporte de energía lanzado para {fecha}"


@shared_task(name="energia.drift_medidores")
def drift_medidores() -> str:
    """Vuelve a consultar Quoia por las filas del día anterior sin revisar.

    Marca las que tengan un medidor que cambió desde que se clasificó, para que
    quien entre a reportar lo vea en la lista sin abrir cada frontera (pedido de
    2026-08-26).

    Corre varias veces entre las 4:00 y las 5:30am: después de la clasificación
    de las 3:30, con margen para que termine, y dándole varias oportunidades de
    detectar un valor que Quoia siga asentando esa madrugada. El costo por
    corrida no crece: las filas ya marcadas se excluyen de la consulta
    siguiente.
    """
    fecha = hoy_col() - timedelta(days=1)
    from apps.energia.services.reporte.drift_medidores import (
        verificar_drift_medidores_background,
    )

    marcadas = verificar_drift_medidores_background(fecha)
    logger.info("drift de medidores fecha=%s marcadas=%s", fecha, marcadas)
    return f"{marcadas} filas marcadas para {fecha}"


@shared_task(name="energia.excel_cedillanos")
def excel_cedillanos() -> str:
    """Busca en el buzón el correo de Cedillanos con su Excel de CGM.

    Reemplaza la carga manual. El reporte debe estar listo antes de las 6am,
    pero ese correo llega históricamente entre las 3:25 y las 6:10 (con
    tendencia a correrse más tarde), así que se revisa cada 15 min de 4 a 6 en
    vez de una sola vez. Sin correo nuevo la corrida es solo un IMAP SEARCH que
    no toca la base.
    """
    from apps.energia.services.reporte.excel_terceros_email import (
        revisar_correo_cedillanos,
    )

    revisar_correo_cedillanos()
    return "buzón de Cedillanos revisado"


@shared_task(name="energia.backfill_comercializacion")
def backfill_comercializacion() -> str:
    """Rellena la fecha de inicio de comercialización (primer día con generación
    real) de los proyectos que aún no la tienen.

    Idempotente y respeta las fechas editadas a mano, así que una planta nueva
    entra a Cumplimiento en cuanto registra su primera generación.
    """
    from apps.energia.services.comercializacion import backfill_comercializacion as backfill

    res = backfill(force=False, dry_run=False)
    resumen = (
        f"{len(res.get('actualizados', []))} fechas nuevas, "
        f"{len(res.get('sin_generacion', []))} sin generación, "
        f"{len(res.get('sin_identificador', []))} sin identificador"
    )
    logger.info("backfill de comercialización: %s", resumen)
    return resumen
