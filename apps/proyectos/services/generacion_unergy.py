"""Generación diaria traída de la API de Unergy a `generacion_diaria`.

Reemplaza al sync de Solenium que vivía acá hasta el 2026-09-15 (borrado en
el mismo commit) y que **dejó de escribir el 6 de septiembre**: nueve días sin datos en una tabla que
alimenta el dashboard, el cumplimiento PPA y el pipeline comercial. Un
cumplimiento cortado se lee como "generó poco", no como "no se midió", que es
peor que no tener nada.

Por qué esta fuente y no otra. Hay tres, y se compararon:

    Solenium         57 proyectos con id    detenida
    SolarView        37 proyectos con id    viva, pero cubre MENOS que Solenium
    API de Unergy   ~90 con `sub_project`   viva, verificada el 2026-09-15

La de Unergy cubre casi el doble y ya sabíamos procesarla: es exactamente lo que
hace el Histórico de Generación. La API devuelve un CONTADOR ACUMULADO, no
energía por día, y `unergy_api.deltas` es quien lo convierte restando la lectura
anterior -- por eso se pide con dos días de margen, para que el primer día del
rango tenga con qué restarse.

**La fuente se guarda como "unergy", no como "solenium".** `_persistir` no pisa
filas de otra fuente, así que las de Solenium se quedan intactas y las nuevas
llenan los días vacíos: no se reescribe historia, se tapa el hueco. Y si
Solenium vuelve algún día, las dos conviven sin pelearse.

Se mantiene la ventana de varios días hacia atrás: la API corrige datos viejos,
y así un día que llegó incompleto se completa solo en una corrida posterior.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.db.models import Q

from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import GeneracionDiaria, Proyecto

logger = logging.getLogger("operaciones.generacion_unergy")

DIAS_VENTANA = 7
FUENTE = "unergy"


def _kwh_por_dia(entradas: list[dict]) -> list[tuple[str, float]]:
    """`[(fecha, kwh)]` sumando los intervalos de cada día.

    `deltas` devuelve un punto por lectura --varios por día-- ya convertidos de
    contador a energía del intervalo. Acá se suman por fecha.

    Los días en cero se descartan, igual que hacía el sync de Solenium: un cero
    no distingue "no generó" de "no reportó", y escribirlo pisaría un dato bueno
    de otra fuente.
    """
    por_fecha: dict[str, float] = defaultdict(float)
    for item in entradas or []:
        fecha = (item or {}).get("date")
        kwh = (item or {}).get("kwh")
        if not fecha or kwh is None:
            continue
        try:
            por_fecha[fecha] += float(kwh)
        except (TypeError, ValueError):
            continue
    return [(f, round(v, 3)) for f, v in sorted(por_fecha.items()) if v > 0]


def sincronizar() -> dict:
    """Trae la ventana y la persiste. Devuelve `{proyectos, filas}`."""
    from apps.energia.services import unergy_api

    proyectos = list(
        Proyecto.objects.filter(estado="en_operacion", deleted_at__isnull=True)
        .exclude(Q(sub_project__isnull=True) | Q(sub_project=""))
        .values_list("id", "sub_project")
    )
    if not proyectos:
        logger.info("ningún proyecto en operación con `sub_project`")
        return {"proyectos": 0, "filas": 0}

    try:
        token = unergy_api.token()
    except Exception:
        logger.exception("no se pudo autenticar contra la API de Unergy")
        return {"proyectos": 0, "filas": 0}
    if not token:
        logger.info("API de Unergy sin credenciales — sincronización omitida")
        return {"proyectos": 0, "filas": 0}

    hasta = hoy_col()
    desde = hasta - timedelta(days=DIAS_VENTANA)
    # `ventana_utc` agrega el margen para que el primer delta tenga una lectura
    # previa con la que restarse. No se calcula a mano: es la misma función que
    # usa el Histórico, y el margen es parte de su contrato.
    (pedir_desde, pedir_hasta), (desde_dt, hasta_dt) = unergy_api.ventana_utc(desde, hasta)

    total = 0
    con_datos = 0
    for proyecto_id, sub_project in proyectos:
        try:
            lecturas, _fuente = unergy_api.lecturas_con_respaldo(
                token, sub_project, pedir_desde, pedir_hasta
            )
        except Exception:
            logger.exception("la API de Unergy falló para el proyecto %s", proyecto_id)
            continue

        dias = _kwh_por_dia(unergy_api.deltas(lecturas, desde_dt, hasta_dt))
        if not dias:
            continue
        con_datos += 1

        try:
            total += _persistir(proyecto_id, dias)
        except Exception:
            logger.exception("no se pudo persistir el proyecto %s", proyecto_id)

    logger.info(
        "generación de Unergy: %d días de %d proyectos (%d con datos)",
        total, len(proyectos), con_datos,
    )
    return {"proyectos": len(proyectos), "filas": total, "con_datos": con_datos}


def _persistir(proyecto_id: int, dias: list[tuple[str, float]]) -> int:
    """UPSERT de los días de un proyecto. Devuelve cuántos escribió.

    **Solo pisa lo que ya venía de esta misma fuente.** Un valor cargado a mano,
    o traído de Solenium cuando esa sincronización corría, manda sobre este: la
    condición sobre `fuente` es lo que lo garantiza.
    """
    fechas = [f for f, _ in dias]
    existentes = dict(
        GeneracionDiaria.objects.filter(proyecto_id=proyecto_id, fecha__in=fechas)
        .values_list("fecha", "fuente")
    )

    nuevas, actualizadas = [], []
    for fecha, kwh in dias:
        fuente_actual = existentes.get(_fecha(fecha))
        if fuente_actual is None:
            nuevas.append(GeneracionDiaria(
                proyecto_id=proyecto_id, fecha=fecha, kwh_real=kwh, fuente=FUENTE))
        elif fuente_actual == FUENTE:
            actualizadas.append((fecha, kwh))

    if nuevas:
        GeneracionDiaria.objects.bulk_create(nuevas, ignore_conflicts=True)
    for fecha, kwh in actualizadas:
        GeneracionDiaria.objects.filter(
            proyecto_id=proyecto_id, fecha=fecha, fuente=FUENTE,
        ).update(kwh_real=kwh)

    return len(nuevas) + len(actualizadas)


def _fecha(texto: str):
    """`'2026-09-15'` -> `date`. Las claves de `existentes` son `date`."""
    from datetime import date

    if isinstance(texto, date):
        return texto
    try:
        return date.fromisoformat(str(texto)[:10])
    except ValueError:
        return None
