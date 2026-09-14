"""Derivación de la fecha de inicio de comercialización de un proyecto.

Puerto de `app/services/comercializacion.py`. Regla de negocio (sin hardcode): la
fecha de inicio de comercialización de una planta es el PRIMER día calendario
(hora Colombia) en que registró generación real. Se obtiene consultando la API de
generación de Unergy (`project_generation`) y detectando el primer incremento del
contador acumulado.

Las funciones puras (`primer_dia_con_generacion`) están separadas de la E/S
(`_fetch_*`) para poder probarlas sin red — eso vino así del original y se
conserva. Solo `backfill_comercializacion` y
`proyectos_sin_fecha_comercializacion` se reescribieron al ORM.

`derivar_fecha_comercializacion` usa `hoy_col()` donde el original usaba
`date.today()`: el contenedor corre en UTC (CLAUDE.md).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from apps.comun.config import settings
from apps.plataforma.services.fechas import hoy_col

logger = logging.getLogger("operaciones.comercializacion")

_COL_TZ = timezone(timedelta(hours=-5))

# Piso global de búsqueda: ningún proyecto de la plataforma generó antes de esto.
# Es solo un límite inferior de la ventana de consulta, NO la fecha resultante
# (la fecha sale siempre de los datos de generación).
_PISO_BUSQUEDA = date(2021, 1, 1)


def identificador_monitoreo(proyecto) -> str | None:
    """Identificador que acepta la API de generación para este proyecto."""
    return getattr(proyecto, "sub_project", None)


def unergy_token() -> str:
    """Token de la API de Unergy (mismo flujo que cumplimiento._unergy_token)."""
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{settings.UNERGY_API_URL}/api/accounts/{settings.UNERGY_ACCOUNT_ID}/",
            json={"login": settings.UNERGY_LOGIN, "password": settings.UNERGY_PASSWORD},
            headers={"User-Agent": "PostmanRuntime/7.50.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("access") or data.get("token") or data.get("key") or ""


# Cuanto vale el listado de proyectos de Unergy antes de volver a pedirlo.
#
# 30 minutos: es un catalogo de plantas, no un dato operativo -- cambia cuando
# entra una planta nueva, no durante el dia. El mismo criterio y el mismo TTL
# que el catalogo de Quoia en `reporte/curvas.py`.
TTL_PROYECTOS_UNERGY = 1800

_CLAVE_PROYECTOS_UNERGY = "unergy_api:proyectos"

_cache_proyectos: dict[str, tuple[float, list[dict]]] = {}


def fetch_unergy_projects_cacheado(token: str) -> list[dict]:
    """`fetch_unergy_projects` con cache de dos niveles.

    Existe porque `/proyectos/pendientes` lo consulta en CADA llamada, y esa API
    no es barata. El mismo patron del monitoreo solar: memoria del proceso
    adelante --gratis, y evita el viaje dentro de la misma request-- y Redis
    atras, compartido por los workers de gunicorn.

    **Una lista vacia no se cachea.** Un fallo de la API devuelve `[]`, igual
    que "no hay proyectos", y guardarlo seria congelar el error media hora.

    Los backfills siguen usando `fetch_unergy_projects` directo: corren a mano y
    quieren el dato fresco.
    """
    import time

    ahora = time.monotonic()
    guardado = _cache_proyectos.get(_CLAVE_PROYECTOS_UNERGY)
    if guardado and (ahora - guardado[0]) < TTL_PROYECTOS_UNERGY:
        return guardado[1]

    try:
        from django.core.cache import cache

        de_redis = cache.get(_CLAVE_PROYECTOS_UNERGY)
    except Exception:
        de_redis = None
    if de_redis:
        _cache_proyectos[_CLAVE_PROYECTOS_UNERGY] = (ahora, de_redis)
        return de_redis

    datos = fetch_unergy_projects(token)
    if datos:
        _cache_proyectos[_CLAVE_PROYECTOS_UNERGY] = (ahora, datos)
        try:
            from django.core.cache import cache

            cache.set(_CLAVE_PROYECTOS_UNERGY, datos, TTL_PROYECTOS_UNERGY)
        except Exception:
            pass  # sin Redis el cache queda por proceso
    return datos


def fetch_unergy_projects(token: str) -> list[dict]:
    """Lista completa de proyectos registrados en la plataforma Unergy original
    (no Quoia ni Solenium) -- cada item trae ``nombre_topico`` (= el valor que
    va en ``Proyecto.sub_project``), ``nombre_proyecto`` y ``nombre_corto``.

    Usado para emparejar por nombre los proyectos locales que todavía no
    tienen ``sub_project`` asignado (ver /proyectos/pendientes-unergy) --
    reemplaza la carga manual que antes hacia scripts/cargar_topics_tsf.py
    desde un JSON exportado a mano."""
    try:
        with httpx.Client(timeout=60) as client:
            resp = client.get(
                f"{settings.UNERGY_API_URL}/api/admin/operations/project/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PostmanRuntime/7.50.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("comercializacion: error listando proyectos Unergy: %s", exc)
        return []


def _fetch_readings(token: str, identificador: str, gte: date, lte: date) -> list[dict]:
    """Lecturas crudas del contador acumulado entre [gte, lte] (inclusive).

    Colombia = UTC-5. La ventana se manda en UTC. ``limit`` alto pero acotamos la
    ventana a un mes desde el llamador, así nunca nos acercamos al tope.
    """
    tz_off = timedelta(hours=5)
    start_utc = datetime(gte.year, gte.month, gte.day, 0, 0, 0) + tz_off
    end_utc = datetime(lte.year, lte.month, lte.day, 23, 59, 59) + tz_off
    try:
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            resp = client.get(
                f"{settings.UNERGY_API_URL}/api/admin/operations/project_generation/",
                params={
                    "time_stamp__gte": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "time_stamp__lte": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sub_project": identificador,
                    "limit": "10000",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PostmanRuntime/7.50.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("results", [])
    except Exception as exc:
        logger.warning("comercializacion: error API ident=%s %s..%s: %s", identificador, gte, lte, exc)
        return []


def _dia_col(ts_raw: str) -> date | None:
    """Día calendario en hora Colombia a partir del time_stamp de la API."""
    if not ts_raw:
        return None
    try:
        s = ts_raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s) if "T" in s else datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=_COL_TZ)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_COL_TZ)
        return dt.astimezone(_COL_TZ).date()
    except Exception:
        return None


def primer_dia_con_generacion(readings: list[dict], prev_cum: float | None = None):
    """Primer día (Col) con generación en una lista de lecturas acumuladas.

    Función PURA. Recorre las lecturas en orden cronológico y devuelve
    ``(dia, ultimo_acumulado)``:
      - ``dia`` = primer día en que el contador acumulado es > 0 (= ya hubo energía
        para ese día). Los contadores de estas plantas arrancan en 0 al energizar,
        así que "primer día con acumulado > 0" es exactamente el primer día con
        generación real. ``None`` si en estas lecturas nunca hubo generación.
      - ``ultimo_acumulado`` = último valor acumulado visto (para encadenar bloques
        mes a mes: si un mes vino todo en 0, se pasa al siguiente).

    ``prev_cum`` es el último acumulado del bloque anterior (para telemetría/
    encadenado); la detección de "primer día con energía" es absoluta (val > 0),
    no depende del bloque previo.
    """
    vals = []
    for r in readings:
        g = r.get("generacion")
        if g is None:
            g = r.get("generation")
        if g is None:
            continue
        d = _dia_col(r.get("time_stamp") or r.get("timestamp") or "")
        if d is None:
            continue
        vals.append((r.get("time_stamp") or r.get("timestamp") or "", d, float(g)))
    vals.sort(key=lambda x: x[0])

    last_cum = prev_cum
    for _, dia, val in vals:
        last_cum = val
        if val > 0:
            return dia, val
    return None, last_cum


def _limite_inferior(proyecto) -> date:
    """Ventana de búsqueda: arranca un poco antes de la fecha conocida más
    temprana del proyecto (operación / energización estimada), o el piso global.
    Es solo un límite de consulta; el resultado sale de los datos.
    """
    candidatas = [
        getattr(proyecto, "fecha_entrada_operacion", None),
        getattr(proyecto, "fecha_estimada_energizacion", None),
    ]
    candidatas = [c for c in candidatas if c]
    if candidatas:
        base = min(candidatas) - timedelta(days=60)
        return max(_PISO_BUSQUEDA, base)
    return _PISO_BUSQUEDA


def derivar_fecha_comercializacion(proyecto, token: str, hoy: date | None = None) -> date | None:
    """Deriva la fecha de inicio de comercialización de un proyecto.

    Consulta la generación mes a mes desde el límite inferior hasta hoy y devuelve
    el primer día con energía. Se detiene en cuanto lo encuentra. ``None`` si el
    proyecto no tiene identificador de monitoreo o nunca registró generación.
    """
    ident = identificador_monitoreo(proyecto)
    if not ident:
        return None

    hoy = hoy or hoy_col()
    cursor = _limite_inferior(proyecto)
    prev_cum: float | None = None
    guard = 0  # tope defensivo: máx ~ 12 años de meses
    while cursor <= hoy and guard < 160:
        guard += 1
        ult_dia_mes = calendar.monthrange(cursor.year, cursor.month)[1]
        fin_mes = min(date(cursor.year, cursor.month, ult_dia_mes), hoy)
        readings = _fetch_readings(token, ident, date(cursor.year, cursor.month, 1), fin_mes)
        if readings:
            dia, prev_cum = primer_dia_con_generacion(readings, prev_cum)
            if dia is not None:
                return dia
        # avanzar al primer día del mes siguiente
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return None


def backfill_comercializacion(*, force: bool = False, dry_run: bool = False) -> dict:
    """Rellena `fecha_inicio_comercializacion` derivándola de la generación real.

    - Sin `force`: solo toca proyectos con la fecha en NULL y NO editados a mano.
      Idempotente: correrlo dos veces no cambia nada la segunda vez.
    - Con `force`: recalcula TODOS los que no estén editados a mano.
    - `dry_run`: no escribe; solo reporta lo que haría.

    **`fecha_comercializacion_editada_manual` se respeta siempre**, incluso con
    `force`: si alguien corrigió la fecha a mano, la generación no la pisa.
    """
    from apps.proyectos.models import Proyecto

    qs = Proyecto.objects.filter(
        deleted_at__isnull=True, fecha_comercializacion_editada_manual=False,
    )
    if not force:
        qs = qs.filter(fecha_inicio_comercializacion__isnull=True)
    proyectos = list(qs.order_by("nombre_comercial"))

    token = ""
    if proyectos:
        try:
            token = unergy_token()
        except Exception as exc:
            logger.warning("comercializacion: no se pudo obtener token Unergy: %s", exc)
            return {"ok": False, "error": f"token Unergy: {exc}", "procesados": 0}

    actualizados: list[dict] = []
    sin_generacion: list[dict] = []
    sin_identificador: list[dict] = []
    a_guardar: list = []

    for p in proyectos:
        ident = identificador_monitoreo(p)
        if not ident:
            sin_identificador.append({"id": p.id, "nombre": p.nombre_comercial})
            continue
        fecha = derivar_fecha_comercializacion(p, token)
        if fecha is None:
            sin_generacion.append({"id": p.id, "nombre": p.nombre_comercial, "identificador": ident})
            continue
        actualizados.append({
            "id": p.id, "nombre": p.nombre_comercial,
            "identificador": ident, "fecha": fecha.isoformat(),
            "anterior": p.fecha_inicio_comercializacion.isoformat()
            if p.fecha_inicio_comercializacion else None,
        })
        if not dry_run:
            p.fecha_inicio_comercializacion = fecha
            a_guardar.append(p)

    if a_guardar:
        Proyecto.objects.bulk_update(a_guardar, ["fecha_inicio_comercializacion"])

    return {
        "ok": True,
        "dry_run": dry_run,
        "force": force,
        "procesados": len(proyectos),
        "actualizados": actualizados,
        "sin_generacion": sin_generacion,
        "sin_identificador": sin_identificador,
    }


def proyectos_sin_fecha_comercializacion() -> list[dict]:
    """Proyectos en operación sin fecha de inicio de comercialización.

    Es la lista que interesa al final: plantas que (aún) no entran a Cumplimiento
    por no tener fecha, con el motivo probable — sin identificador de monitoreo o
    sin generación registrada.
    """
    from apps.proyectos.models import Proyecto

    proyectos = (
        Proyecto.objects
        .filter(deleted_at__isnull=True, fecha_inicio_comercializacion__isnull=True)
        # `!=` de SQL descarta los NULL; el `exclude()` de Django los CONSERVA
        # (añade un `IS NOT NULL` al negar). El segundo exclude reproduce el
        # comportamiento del original, que no listaba los proyectos sin tipo.
        .exclude(tipo_proyecto="autoconsumo")
        .exclude(tipo_proyecto__isnull=True)
        .order_by("nombre_comercial")
    )
    salida = []
    for p in proyectos:
        ident = identificador_monitoreo(p)
        salida.append({
            "id": p.id,
            "nombre": p.nombre_comercial,
            "estado": p.estado,
            "srv_representacion": bool(p.srv_representacion),
            "identificador_monitoreo": ident,
            "motivo": "sin_identificador_monitoreo" if not ident else "sin_generacion_registrada",
        })
    return salida
