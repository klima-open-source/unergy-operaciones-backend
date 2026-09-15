"""Cuándo corre cada tarea programada. El QUÉ está en `apps/<dominio>/tasks.py`.

Un solo archivo en vez de 19 decoradores repartidos: así se ve de un vistazo qué
compite con qué por una franja, que es justo la información que hacía falta al
escribir estos horarios (dos jobs que leen la misma API con rate limit no pueden
salir el mismo segundo).

**Todo en hora de Bogotá.** `CELERY_TIMEZONE` es `America/Bogota` (UTC−5, sin
horario de verano) aunque el contenedor corra en UTC, igual que hacía el
`BackgroundScheduler` de FastAPI con `timezone=settings.TIMEZONE`. Sin eso, la
clasificación de las 3:30am correría a las 10:30pm del día anterior.

`django_celery_beat` guarda el horario en la BASE, pero su `DatabaseScheduler`
sincroniza este diccionario al arrancar: la fuente de verdad sigue siendo el
código, versionado y revisable, y la tabla es solo su reflejo. Editar una franja
desde el admin funciona hasta el próximo arranque, que la vuelve a pisar.
"""

from celery.schedules import crontab

# Franjas heredadas tal cual del scheduler de FastAPI. Los comentarios explican
# las que NO son obvias — por qué esa hora y no otra.
HORARIOS = {
    # ── Reporte de Energía ───────────────────────────────────────────────────
    "reporte-energia-clasificar": {
        "task": "energia.reporte_diario",
        "schedule": crontab(hour=3, minute=30),
    },
    # Cada 5 min de 4:00 a 5:30. Dos entradas porque un crontab no expresa
    # minutos distintos según la hora.
    "reporte-energia-drift-4am": {
        "task": "energia.drift_medidores",
        "schedule": crontab(hour=4, minute="*/5"),
    },
    "reporte-energia-drift-5am": {
        "task": "energia.drift_medidores",
        "schedule": crontab(hour=5, minute="0,5,10,15,20,25,30"),
    },
    # Cada 15 min de 4 a 6, más un último intento a las 6:00 en punto: el correo
    # de Cedillanos llega entre las 3:25 y las 6:10 y el reporte cierra a las 6.
    "excel-cedillanos": {
        "task": "energia.excel_cedillanos",
        "schedule": crontab(hour="4,5", minute="*/15"),
    },
    "excel-cedillanos-ultimo": {
        "task": "energia.excel_cedillanos",
        "schedule": crontab(hour=6, minute=0),
    },

    # ── Madrugada: los tres que leen la API de generación de Unergy ──────────
    # Separados 10 y 15 min entre sí a propósito, para no competir por el mismo
    # rate limit en el mismo segundo.
    "gen-promedio-recalcular": {
        "task": "proyectos.recalcular_gen_promedio",
        "schedule": crontab(hour=3, minute=20),
    },
    "backfill-comercializacion": {
        "task": "energia.backfill_comercializacion",
        "schedule": crontab(hour=3, minute=30),
    },
    "comercial-backfill": {
        "task": "comercial.backfill_oportunidades",
        "schedule": crontab(hour=3, minute=35),
    },

    # ── Ingesta de fuentes externas ──────────────────────────────────────────
    "gen-sync-am": {
        "task": "proyectos.sincronizar_generacion_diaria",
        "schedule": crontab(hour=7, minute=0),
    },
    "gen-sync-pm": {
        "task": "proyectos.sincronizar_generacion_diaria",
        "schedule": crontab(hour=19, minute=0),
    },
    "bolsa-ingest": {
        "task": "mercado_xm.ingesta_bolsa",
        "schedule": crontab(hour=11, minute=0),
    },
    "evo-forecast-ingest": {
        "task": "mercado_xm.ingesta_pronostico_clima",
        "schedule": crontab(hour=6, minute=0),
    },
    # Cada 6 h. Es el unico con intervalo y no franja fija: no depende de que
    # una fuente publique a cierta hora, solo de no quedarse atras.
    "tsf-sync": {
        "task": "proyectos.sincronizar_tsf",
        "schedule": crontab(hour="*/6", minute=10),
    },

    # ── Monitoreo ────────────────────────────────────────────────────────────
    # Cada 15 min, como el ciclo de MGS de siempre. `MGS_POLL_INTERVAL_MINUTES`
    # dejo de leerse al pasar a Celery: la franja vive aca, no en el .env.
    "sondeo-mgs": {
        "task": "monitoreo.sondeo_mgs",
        "schedule": crontab(minute="*/15"),
    },

    # ── Alertas de vencimiento ───────────────────────────────────────────────
    # Las dos salen por el mismo SMTP; 15 min de separacion para no competir por
    # la misma franja exacta. Comparten destinatarios a proposito.
    "alertas-representacion": {
        "task": "contratos.alertas_representacion",
        "schedule": crontab(hour=8, minute=0),
    },
    "alertas-vencimiento-ppa": {
        "task": "ppa.alertas_vencimiento",
        "schedule": crontab(hour=8, minute=15),
    },

    # ── Mandatos ─────────────────────────────────────────────────────────────
    # Cada hora de 7am a 7pm: la revisoria y los envios a inversionistas van en
    # horario laboral. Sin correos nuevos es solo un IMAP SEARCH.
    "correos-mandatos": {
        "task": "mandatos.revisar_correos",
        "schedule": crontab(hour="7-19", minute=5),
    },

    # ── Anual ────────────────────────────────────────────────────────────────
    # 1 de enero. El DANE publica el IPC a principios de enero, asi que esto no
    # trae el numero: deja la fila abierta para que el anio aparezca esperando el
    # dato en vez de faltar en silencio.
    "om-ipc-del-anio": {
        "task": "om.revisar_ipc_del_anio",
        "schedule": crontab(month_of_year=1, day_of_month=1, hour=9, minute=0),
    },

    # ── Comercial ────────────────────────────────────────────────────────────
    # Justo después del cambio de día, para que el tablero amanezca correcto.
    "comercial-cierres": {
        "task": "comercial.cerrar_contratos_vencidos",
        "schedule": crontab(hour=0, minute=20),
    },
}
