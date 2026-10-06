"""Comunicación de cada planta, por fuente: inversores (SolarView) y medidor (Quoia).

Una fuente está **sin comunicación** si su último dato llegó hace más de
`UMBRAL` (2 h), o si hoy no llegó ninguno. Las dos fuentes se evalúan por
separado: una planta puede no comunicar por inversores y sí por medidor (Puya, el
2026-10-05: inversores sin un dato en todo el día, medidor al día).

Reemplaza a la "disponibilidad" de SolarView (`/kpis/availability/`) en la vista
de Generación Solar. Esa categoría mide qué porcentaje del tiempo la planta
estuvo disponible, no si están llegando datos: llamaba "sin comunicación" a
plantas cuyo medidor reportaba al minuto.

**Cuándo se evalúa.** En el sondeo MGS (cada 15 min), con las respuestas que las
alarmas de desconexión ya piden: no hace ninguna llamada propia. Solo de día
(la misma ventana de las alarmas): de noche los inversores no reportan y todas
las plantas quedarían marcadas. Fuera de esa ventana se conserva el último
estado del día.

**Qué cuenta como dato.** Cualquier punto con valor, aunque sea 0 kW: un equipo
que reporta cero está comunicando.

**Qué no se evalúa.** Una planta sin medidor vinculado no tiene fuente "medidor"
(`None`), no "sin comunicación": no tiene un medidor que falle. Si la llamada a
una fuente FALLA (red, timeout), esa fuente conserva su estado anterior: un
error nuestro no es una planta sin comunicación.

**Cuando el servicio no responde.** Si TODAS las consultas de una fuente
fallaron en la corrida, se guarda en `consultas()` y la pantalla avisa ("SolarView
no respondió en la última consulta"): las plantas conservan su último estado y
ese aviso dice que puede estar viejo. Las fallas sueltas no avisan.

**Dónde vive.** En el caché de Django (Redis): se recalcula cada 15 min, así que
si Redis se vacía se llena solo en la siguiente corrida. Mientras tanto la
planta sale sin evaluar.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apps.plataforma.services.fechas import COL_TZ

logger = logging.getLogger("operaciones.comunicacion")

UMBRAL = timedelta(hours=2)
CLAVE = "monitoreo:comunicacion"
CLAVE_CONSULTAS = "monitoreo:comunicacion:consultas"
# Más de un día: lo evaluado a las 17:00 tiene que seguir ahí a las 7:00.
TTL = 36 * 3600

FUENTES = ("inversores", "medidor")


def _hora_bogota(texto: str | None) -> datetime | None:
    """Una hora de SolarView o de Quoia → instante con zona.

    SolarView manda `2026-10-05 17:30`, sin zona, en hora de Bogotá (las
    tarjetas la muestran así desde siempre). Quoia manda ISO con la zona
    (`2026-09-03T13:00:00-05:00`); si alguna vez viene en UTC (`Z`), se respeta.
    """
    if not texto:
        return None
    try:
        instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instante.tzinfo is None:
        return instante.replace(tzinfo=COL_TZ)
    return instante.astimezone(COL_TZ)


def ultimo_dato_inversores(respuesta: dict | None) -> datetime | None:
    """Hora del último punto CON valor de `GET /solarview/measurements/power/`.

    La serie trae la grilla del día completa; los puntos que aún no llegan vienen
    en `None` y no cuentan.
    """
    serie = ((respuesta or {}).get("results") or {}).get("power") or {}
    con_valor = [t for t, kw in serie.items() if kw is not None]
    return _hora_bogota(max(con_valor)) if con_valor else None


def ultimo_dato_medidor(snapshot: dict | None) -> datetime | None:
    """`last_time` del snapshot de Quoia: el punto más reciente de cualquier variable."""
    return _hora_bogota((snapshot or {}).get("last_time"))


def evaluar(ultimo: datetime | None, ahora: datetime) -> dict:
    """La fuente, lista para guardar y para la API."""
    return {
        "sin_comunicacion": ultimo is None or ahora - ultimo > UMBRAL,
        "ultimo_dato": ultimo.isoformat() if ultimo else None,
    }


def _cache():
    from django.core.cache import cache

    return cache


def leer() -> dict[int, dict]:
    """`{proyecto_id: {"inversores": …, "medidor": …, "evaluado_en": iso}}`."""
    try:
        return _cache().get(CLAVE) or {}
    except Exception:
        logger.warning("no se pudo leer la comunicación de las plantas", exc_info=True)
        return {}


def registrar(nuevos: dict[int, dict], ahora: datetime) -> None:
    """Guarda lo evaluado en esta corrida encima de lo anterior.

    `nuevos[pid][fuente]` puede faltar: esa fuente no se pudo consultar y
    conserva lo que tenía. Las plantas que no vienen en `nuevos` también se
    conservan (hasta su TTL).
    """
    guardado = leer()
    for pid, fuentes in nuevos.items():
        fila = dict(guardado.get(pid) or {})
        fila.update(fuentes)
        fila["evaluado_en"] = ahora.isoformat()
        guardado[pid] = fila
    try:
        _cache().set(CLAVE, guardado, TTL)
    except Exception:
        logger.warning("no se pudo guardar la comunicación de las plantas", exc_info=True)


def registrar_consulta(fuente: str, *, fallo: bool, ahora: datetime) -> None:
    """Si la consulta de esta corrida a una fuente falló por completo."""
    try:
        guardado = _cache().get(CLAVE_CONSULTAS) or {}
        guardado[fuente] = {"fallo": fallo, "consultado_en": ahora.isoformat()}
        _cache().set(CLAVE_CONSULTAS, guardado, TTL)
    except Exception:
        logger.warning("no se pudo guardar el estado de la consulta", exc_info=True)


def consultas() -> dict:
    """`{"inversores": {"fallo", "consultado_en"}, "medidor": {…}}`, lo que haya."""
    try:
        return _cache().get(CLAVE_CONSULTAS) or {}
    except Exception:
        return {}
