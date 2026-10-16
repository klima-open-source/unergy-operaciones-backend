"""Cliente de EVO y el guardado del precio de bolsa diario.

EVO es un servicio interno al que se llega por Tailscale. Lo único que queda de
él es la tarea `mercado_xm.ingesta_bolsa`: trae el DailySpot del día y lo guarda
en `precios_bolsa_diario`, que leen el tablero, Descubrimientos y Cumplimiento.

El proxy `/api/v1/evo`, el clima y el detalle por hora se retiraron el
2026-10-16 (ver `apps/mercado_xm/migrations/0004_borrar_clima_y_bolsa_horario.py`):
el front ya no los mostraba y el que sí los mostraba hablaba con EVO directo.

`ponytail: SQL crudo, no un modelo de Django`. La tabla nunca tuvo modelo (ver
apps/README.md) y solo la toca este módulo, con `INSERT … ON CONFLICT DO UPDATE`.
"""

import json
import logging
import os

import httpx
from django.db import connection

logger = logging.getLogger("operaciones.evo")

TIMEOUT = httpx.Timeout(10.0, read=30.0)


class EvoNoConfigurado(RuntimeError):
    pass


class EvoInalcanzable(RuntimeError):
    pass


class EvoTimeout(RuntimeError):
    pass


class EvoRespondioError(RuntimeError):
    def __init__(self, status_code: int, texto: str):
        super().__init__(texto)
        self.status_code = status_code
        self.texto = texto


def get(ruta: str, params: dict | None = None) -> dict:
    """GET contra EVO. Traduce cada fallo de red a su propia excepción."""
    base = os.environ.get("EVO_API_URL", "")
    if not base:
        raise EvoNoConfigurado("EVO_API_URL not configured")

    cabeceras = {}
    token = os.environ.get("EVO_API_TOKEN", "")
    if token:
        cabeceras["X-EVO-Token"] = token

    url = f'{base.rstrip("/")}/{ruta.lstrip("/")}'
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            respuesta = http.get(url, headers=cabeceras, params=params)
            respuesta.raise_for_status()
            return respuesta.json()
    except httpx.ConnectError as exc:
        raise EvoInalcanzable("EVO unreachable") from exc
    except httpx.TimeoutException as exc:
        raise EvoTimeout("EVO timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise EvoRespondioError(exc.response.status_code, exc.response.text) from exc


# ---------------------------------------------------------------------------
# Persistencia — «lo mejor que se pueda»: si falla, se loguea y se sigue
# ---------------------------------------------------------------------------

_SQL_BOLSA_DIARIO = """
    INSERT INTO precios_bolsa_diario
        (fecha, precio_promedio, precio_min, precio_max, precio_escasez,
         demanda_gwh, hidro_pct, termica_pct, renovable_pct, menor_pct,
         hora_pico, spread, source_data)
    VALUES (%(fecha)s, %(avg)s, %(min)s, %(max)s, %(escasez)s,
            %(demanda)s, %(hidro)s, %(termica)s, %(renovable)s, %(menor)s,
            %(pico)s, %(spread)s, %(source)s)
    ON CONFLICT (fecha) DO UPDATE SET
        precio_promedio = EXCLUDED.precio_promedio,
        precio_min = EXCLUDED.precio_min,
        precio_max = EXCLUDED.precio_max,
        precio_escasez = EXCLUDED.precio_escasez,
        demanda_gwh = EXCLUDED.demanda_gwh,
        hidro_pct = EXCLUDED.hidro_pct,
        termica_pct = EXCLUDED.termica_pct,
        renovable_pct = EXCLUDED.renovable_pct,
        menor_pct = EXCLUDED.menor_pct,
        hora_pico = EXCLUDED.hora_pico,
        spread = EXCLUDED.spread,
        source_data = EXCLUDED.source_data
"""


def guardar_dailyspot(datos: dict) -> None:
    """Guarda el resumen del DailySpot del día. No levanta nunca."""
    fecha = datos.get("date")
    if not fecha:
        return
    resumen = datos.get("summary", {})

    try:
        with connection.cursor() as cursor:
            cursor.execute(_SQL_BOLSA_DIARIO, {
                "fecha": fecha,
                "avg": resumen.get("price_avg"),
                "min": resumen.get("price_min"),
                "max": resumen.get("price_max"),
                "escasez": datos.get("scarcity_price"),
                "demanda": resumen.get("total_gwh"),
                "hidro": resumen.get("hydro_pct"),
                "termica": resumen.get("thermal_pct"),
                "renovable": resumen.get("renewable_pct"),
                "menor": resumen.get("minor_pct"),
                "pico": resumen.get("peak_hour"),
                "spread": resumen.get("spread"),
                "source": json.dumps({"fetched_at": datos.get("fetched_at")}),
            })
        logger.info("DailySpot guardado para %s", fecha)
    except Exception:
        logger.exception("no se pudo guardar el DailySpot")
