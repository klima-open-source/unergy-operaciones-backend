"""Resumen de la flota desde Solenium — con caché en proceso.

El cliente HTTP (`app/services/mgs/solenium_client.py`) se reusa tal cual: no
toca la base y no sabe de framework.

`ponytail: caché en un dict de módulo, no en django.core.cache`. Vale mientras
el despliegue corra con un solo proceso web (`WORKERS=1`, ver README). Al sacar
el scheduler a Celery y subir los workers, cada proceso tendrá su propia copia y
esto pasa a `django.core.cache` con un backend compartido.
"""

import logging
import time

logger = logging.getLogger("operaciones.dashboard")

TTL_SEGUNDOS = 180
_cache: dict = {"datos": None, "ts": 0.0}


def resumen() -> dict:
    """(potencia kW, plantas en línea, plantas totales). Nunca levanta.

    Si Solenium no responde el dashboard debe salir igual con estos campos en
    `null`: es un dato de apoyo, no el motivo de la pantalla.
    """
    ahora = time.monotonic()
    if _cache["datos"] and (ahora - _cache["ts"]) < TTL_SEGUNDOS:
        return _cache["datos"]

    vacio = {"fleet_power_kw": None, "fleet_online": None, "fleet_total": None}
    try:
        from app.services.mgs.solenium_client import SoleniumClient

        cliente = SoleniumClient()
        if not cliente.enabled:
            return vacio
        plantas = cliente.get_project_summary()
        datos = {
            "fleet_power_kw": round(sum(p.get("power_kw") or 0 for p in plantas), 1),
            "fleet_online": sum(1 for p in plantas if (p.get("power_kw") or 0) > 0),
            "fleet_total": len(plantas),
        }
    except Exception:
        logger.debug("resumen de flota de Solenium no disponible", exc_info=True)
        return vacio

    _cache["datos"], _cache["ts"] = datos, ahora
    return datos
