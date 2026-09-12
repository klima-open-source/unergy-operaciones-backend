"""Generación solar en tiempo real, desde la API de inversores de SolarView.

Puerto de `app/api/v1/generacion_solar.py`, migrado de Solenium a SolarView el
2026-09-03. Reemplaza a `solenium_monitoreo.py`, que quedó obsoleto entero: los
ids de los dos proveedores son esquemas DISTINTOS que no se derivan uno del
otro, así que la resolución va por `Proyecto.project_id_solarview`, reconciliado
por el equipo y poblado por `apps/proyectos/services/backfill_solarview.py`.

A propósito NO se empareja por nombre. Antes, si a un proyecto le faltaba el id,
se buscaba en el catálogo del proveedor por coincidencia de nombre —exacta
primero y luego por subcadena de ≥ 5 caracteres— y el resultado se persistía
como efecto secundario de un GET. Eso es una adivinanza silenciosa que se
recalcula en cada request y puede cambiar de respuesta si el proveedor renombra
algo. Mismo criterio que Reporte de Energía: un proyecto sin id reconciliado no
tiene inversores, y el hueco queda visible para que lo resuelva el backfill.

Los clientes HTTP (`SolarViewClient`, `GaiaClient`, `medidor_tiempo_real`) se
reusan de `app/services/mgs/` tal cual: no tocan la base ni saben de framework.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from calendar import monthrange
from datetime import datetime, timedelta

from django.db import close_old_connections
from rest_framework.exceptions import NotFound

from api.exceptions import NoProcesable, ServicioNoDisponible
from apps.fronteras.models import Frontera
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto

logger = logging.getLogger("operaciones.generacion_solar")

_cliente = None
_gaia = None

# ── TTL cache: memoria del proceso + Redis ───────────────────────────────────
# Evita llamar a SolarView/Gaia en cada request; se invalida solo pasado el TTL.
#
# Eran DOS niveles desde el 2026-09-11. Antes era solo el dict de módulo, con
# esta nota: "vale mientras el despliegue corra con un solo proceso web
# (WORKERS=1)". Ya no corre con uno: gunicorn levanta `--workers 3`, así que
# había tres caches independientes y una de cada tres recargas caía en un
# proceso frío y pagaba las ~235 llamadas externas completas. El usuario lo veía
# como "a veces carga al instante y a veces se demora", sin patrón.
#
# El nivel de proceso se conserva adelante porque es gratis y ahorra el viaje a
# Redis dentro de la misma request.
#
# **Si Redis no responde no se rompe nada**: queda el cache por proceso, que es
# exactamente el comportamiento anterior. Un panel de monitoreo no se cae porque
# el cache no esté.
_cache: dict[str, tuple[float, int, object]] = {}

_PREFIJO_REDIS = "solar_monitoreo:"

CACHE_TTL_FLOTA = 60     # segundos — monitoreo de flota
CACHE_TTL_DETALLE = 90   # segundos — detalle por proyecto
CACHE_TTL_GENHOY = 120   # segundos — generación de hoy

TIPOS_GENERACION = ["generacion", "generacion_consumo"]


def _cache_get(clave: str):
    entrada = _cache.get(clave)
    if entrada and time.monotonic() - entrada[0] < entrada[1]:
        return entrada[2]

    try:
        from django.core.cache import cache

        guardado = cache.get(_PREFIJO_REDIS + clave)
    except Exception:
        return None
    if not guardado:
        return None

    # Lo que queda de vida, no el TTL entero: si se reiniciara acá, una entrada
    # a punto de vencer viviría otro TTL completo en este proceso y el dato
    # podría mostrarse hasta el doble de viejo de lo que dice CACHE_TTL_*.
    restante = guardado["expira"] - time.time()
    if restante <= 0:
        return None
    _cache[clave] = (time.monotonic(), restante, guardado["datos"])
    return guardado["datos"]


def _cache_set(clave: str, ttl: int, datos) -> None:
    _cache[clave] = (time.monotonic(), ttl, datos)
    try:
        from django.core.cache import cache

        cache.set(
            _PREFIJO_REDIS + clave,
            {"expira": time.time() + ttl, "datos": datos},
            ttl,
        )
    except Exception:
        pass  # sin Redis el cache queda por proceso, como antes


def _get_cliente():
    global _cliente
    if _cliente is None:
        from app.services.mgs.solarview_client import SolarViewClient

        _cliente = SolarViewClient()
    if not _cliente.enabled:
        raise ServicioNoDisponible("SolarView credentials not configured")
    return _cliente


def _get_gaia():
    """El GaiaClient si hay credenciales, si no None (no es fatal)."""
    global _gaia
    if _gaia is None:
        from app.services.mgs.gaia_client import GaiaClient

        _gaia = GaiaClient()
    return _gaia if _gaia.enabled else None


def _sv_id(p: Proyecto) -> int | None:
    """El id de SolarView del proyecto, o None si no está reconciliado.

    Sin fallback por nombre a propósito — ver el docstring del módulo.
    """
    if not p.project_id_solarview:
        return None
    try:
        return int(p.project_id_solarview)
    except (TypeError, ValueError):
        logger.warning("project_id_solarview inválido proyecto_id=%s valor=%r",
                       p.id, p.project_id_solarview)
        return None


def _limite_hora_kwh(capacidad_kwp: float | None) -> float | None:
    """Techo de kWh en una hora para una planta, o None si no se sabe su
    capacidad. Reusa el mismo criterio del pipeline del ASIC."""
    from apps.energia.services.reporte.utils import limite_plausible_kwh

    if not capacidad_kwp or capacidad_kwp <= 0:
        return None
    return limite_plausible_kwh(float(capacidad_kwp) / 1000)


def _es_hora_plausible(valor, limite: float | None) -> bool:
    """Si esa hora de generación es físicamente posible para la planta."""
    try:
        val = float(valor)
    except (TypeError, ValueError):
        return False
    return limite is None or abs(val) <= limite


def _suma_kwh_inversor_hoy(gen_kwh: dict, hoy_str: str,
                           capacidad_kwp: float | None = None) -> float:
    """Suma las entradas de HOY de un mapa `generation_kwh` de SolarView.

    `get_generation(ayer, hoy)` devuelve valores incrementales por franja
    horaria con claves tipo "2026-06-09 08:00"; nos quedamos con las de hoy.

    Se descartan las horas físicamente imposibles. SolarView calcula la
    generación POR DIFERENCIA DE ACUMULADOS, así que cuando el acumulador se
    reinicia o falla, la diferencia es el acumulado histórico entero. Verificado
    en vivo el 2026-09-03 con San Pedro (996 kWp): dos horas del día marcaban
    4.682.690,23 kWh cada una, junto a valores normales de 87,52 kWh. Sin este
    filtro el total del día daba 4,7 GWh y el de la flota 98 GWh.
    """
    if not gen_kwh:
        return 0.0
    limite = _limite_hora_kwh(capacidad_kwp)
    total = 0.0
    for k, v in gen_kwh.items():
        if not str(k).startswith(hoy_str):
            continue
        if not _es_hora_plausible(v, limite):
            logger.warning("hora descartada: %s = %r (techo %s)", k, v, limite)
            continue
        total += float(v)
    return total


def _kwh_medidor_de_detalle(detalle: dict | None) -> float | None:
    """Energía del día del medidor (frontera) desde /config/project-detail/.

    Reemplaza al `frontier_generation_kwh` del lote de summary de Solenium, que
    SolarView no tiene. La unidad viene DECLARADA en el propio bloque y puede
    ser kWh o MWh — verificado en vivo el 2026-09-03. Se lee, nunca se asume.
    """
    if not detalle:
        return None
    if "results" in detalle:
        detalle = detalle["results"]
    gen = (detalle or {}).get("generation") or {}
    if not gen.get("value"):
        return None
    try:
        val = float(gen["value"])
    except (ValueError, TypeError):
        return None
    unidad = (gen.get("unit") or "kWh").strip().lower()
    return val * 1000 if unidad == "mwh" else val


def _proyecto_o_404(proyecto_id: int) -> Proyecto:
    p = Proyecto.objects.filter(id=proyecto_id).first()
    if not p:
        raise NotFound("Proyecto no encontrado")
    return p


# ── Endpoints ────────────────────────────────────────────────────────────────

def historial(proyecto_id: int, fecha_inicio: str, fecha_fin: str,
              granularidad: str = "day") -> dict:
    """Generación histórica de un proyecto desde SolarView, diaria u horaria."""
    p = _proyecto_o_404(proyecto_id)

    sol_id = _sv_id(p)
    if sol_id is None:
        raise NotFound(
            "Este proyecto no tiene ID en SolarView. Se asigna con el backfill "
            "(apps/proyectos/services/backfill_solarview.py) o a mano en "
            "project_id_solarview."
        )

    cliente = _get_cliente()
    crudo = cliente.get_generation(sol_id, fecha_inicio, fecha_fin) or {}
    gen_kwh: dict[str, float] = crudo.get("generation_kwh") or {}

    if granularidad == "hour":
        puntos = [
            {"label": ts, "kwh": round(float(v), 2)}
            for ts, v in sorted(gen_kwh.items())
        ]
    else:
        # Agregar por día: sumar todas las horas del mismo día.
        diario: dict[str, float] = {}
        for ts, v in gen_kwh.items():
            dia = ts.split(" ")[0]       # "2026-05-22 08:00" → "2026-05-22"
            diario[dia] = diario.get(dia, 0.0) + float(v)
        puntos = [
            {"label": dia, "kwh": round(kwh, 1)}
            for dia, kwh in sorted(diario.items())
        ]

    return {
        "proyecto_id": p.id,
        "nombre": p.nombre_comercial,
        "sol_id": sol_id,
        "granularidad": granularidad,
        "puntos": puntos,
        "total_kwh": round(sum(pt["kwh"] for pt in puntos), 1),
    }


def _p90_del_dia(proyecto) -> float | None:
    """El P90 del mes en curso repartido entre sus días, en kWh.

    `p90_mensual_kwh` son 12 valores, uno por mes. `None` --y no 0-- cuando la
    planta no tiene curva cargada: 0 se vería como "la meta es cero" y haría que
    el porcentaje de cumplimiento saliera absurdo.
    """
    curva = proyecto.p90_mensual_kwh
    if not curva:
        return None
    hoy = hoy_col()
    try:
        mensual = float(curva[hoy.month - 1] or 0)
    except (IndexError, TypeError, ValueError):
        return None
    if mensual <= 0:
        return None
    dias = monthrange(hoy.year, hoy.month)[1]
    return round(mensual / dias, 1)


def _proyectos_en_operacion() -> list[tuple[Proyecto, int]]:
    """`(proyecto, sol_id)` de los que operan Y tienen id reconciliado.

    Los borrados quedan fuera: `Proyecto` no tiene manager que los filtre y hay
    que excluirlos en cada consulta.
    """
    proyectos = list(Proyecto.objects.filter(
        estado="en_operacion", deleted_at__isnull=True,
    ))
    emparejados = [(p, sid) for p in proyectos if (sid := _sv_id(p)) is not None]
    logger.info("proyectos con id de solarview: %d / %d",
                len(emparejados), len(proyectos))
    return emparejados


def generacion_hoy() -> dict:
    """Generación real de HOY por proyecto. Un proyecto sin id no aparece.

    Dos fuentes, en orden: los inversores (`/generation/`) y, si dan cero, el
    medidor de frontera (`/project_detail/`). El campo `fuente` dice cuál se usó.
    """
    clave = f"genhoy:{hoy_col().isoformat()}"
    if (cacheado := _cache_get(clave)) is not None:
        return cacheado

    cliente = _get_cliente()
    emparejados = _proyectos_en_operacion()
    hoy_str = hoy_col().isoformat()
    ayer_str = (hoy_col() - timedelta(days=1)).isoformat()

    def _leer(item: tuple) -> tuple:
        p, sol_id = item
        kwh = 0.0
        fuente = "sin_dato"

        # Fuente 1: get_generation(ayer, hoy) → filtramos solo entradas de hoy.
        # Con un solo día devuelve el acumulado histórico; con rango ayer→hoy
        # devuelve incrementales por franja horaria.
        try:
            gen = cliente.get_generation(sol_id, ayer_str, hoy_str) or {}
            if "results" in gen:
                gen = gen["results"]
            kwh = _suma_kwh_inversor_hoy(
                gen.get("generation_kwh") or {}, hoy_str, p.potencia_ac_kw)
            if kwh > 0:
                fuente = "inversor"
        except Exception as exc:
            logger.warning("generation fallo sol_id=%s: %s", sol_id, exc)

        # Fuente 2: el medidor de frontera.
        if kwh == 0.0:
            try:
                kwh_med = _kwh_medidor_de_detalle(cliente.get_project_detail(sol_id))
                if kwh_med and kwh_med > 0:
                    kwh, fuente = kwh_med, "medidor"
            except Exception as exc:
                logger.warning("project_detail fallo sol_id=%s: %s", sol_id, exc)

        return (p.id, p.nombre_comercial, sol_id, round(kwh, 1), fuente)

    filas = []
    if emparejados:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for pid, nombre, sol_id, kwh_real, fuente in pool.map(_leer, emparejados):
                filas.append({
                    "proyecto_id": pid, "nombre": nombre, "sol_id": sol_id,
                    "kwh_real": kwh_real, "fuente": fuente,
                })
        close_old_connections()

    filas.sort(key=lambda x: x["kwh_real"], reverse=True)
    datos = {
        "fecha": hoy_str,
        "total": round(sum(r["kwh_real"] for r in filas), 1),
        "proyectos": filas,
    }
    _cache_set(clave, CACHE_TTL_GENHOY, datos)
    return datos


def resumen_dia() -> dict:
    """Top de generación del día, por medidores y por inversores.

    Las dos lecturas van en la misma pasada paralela. Antes el medidor salía del
    lote de summary de Solenium (una llamada para toda la flota); SolarView no
    tiene ese lote, así que va por project-detail, una por proyecto — pero
    aprovechando el mismo worker que ya pedía la generación de inversores.
    """
    clave = f"resumendia:{hoy_col().isoformat()}"
    if (cacheado := _cache_get(clave)) is not None:
        return cacheado

    cliente = _get_cliente()
    hoy_str = hoy_col().isoformat()
    ayer_str = (hoy_col() - timedelta(days=1)).isoformat()
    emparejados = _proyectos_en_operacion()

    def _leer(item: tuple) -> tuple:
        p, sol_id = item
        kwh_inv = 0.0
        try:
            gen = cliente.get_generation(sol_id, ayer_str, hoy_str) or {}
            if "results" in gen:
                gen = gen["results"]
            kwh_inv = _suma_kwh_inversor_hoy(
                gen.get("generation_kwh") or {}, hoy_str, p.potencia_ac_kw)
        except Exception as exc:
            logger.warning("resumen-dia inversor sol_id=%s: %s", sol_id, exc)
        try:
            kwh_med = _kwh_medidor_de_detalle(cliente.get_project_detail(sol_id))
        except Exception as exc:
            logger.warning("resumen-dia medidor sol_id=%s: %s", sol_id, exc)
            kwh_med = None
        return (p.id, p.nombre_comercial, round(kwh_inv, 1), kwh_med)

    medidor: list[dict] = []
    inversor: list[dict] = []
    if emparejados:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for pid, nombre, kwh_inv, kwh_med in pool.map(_leer, emparejados):
                if kwh_inv > 0:
                    inversor.append({"proyecto_id": pid, "nombre": nombre, "kwh": kwh_inv})
                if kwh_med and kwh_med > 0:
                    medidor.append({"proyecto_id": pid, "nombre": nombre,
                                    "kwh": round(kwh_med, 1)})
        close_old_connections()

    medidor.sort(key=lambda x: x["kwh"], reverse=True)
    inversor.sort(key=lambda x: x["kwh"], reverse=True)

    datos = {
        "fecha": hoy_str,
        "medidor": {"total": round(sum(x["kwh"] for x in medidor), 1), "top": medidor},
        "inversor": {"total": round(sum(x["kwh"] for x in inversor), 1), "top": inversor},
    }
    _cache_set(clave, CACHE_TTL_GENHOY, datos)
    return datos


ORDEN_ESTADO = {"caido": 0, "sin_comunicacion": 1, "degradado": 2,
                "online": 3, "sin_datos": 4}


def _estado_de(categoria: str | None) -> str:
    """Categoría de disponibilidad de SolarView → estado de la tarjeta."""
    if categoria is None:
        # Ni id ni respuesta del proveedor: no es que la comunicación esté
        # caída, es que no sabemos. El frontend pinta gris lo que no reconoce.
        return "sin_datos"
    if categoria == "disconnect":
        return "sin_comunicacion"
    if categoria == "critical":
        return "caido"
    if categoria in ("low", "medium"):
        return "degradado"
    return "online"


def monitoreo_flota() -> dict:
    """Estado de la flota: minigranjas en operación con servicio de operación.

    Un proyecto SIN `project_id_solarview` igual aparece, con estado
    "sin_datos": sus medidores no dependen del proveedor y la tarjeta tiene que
    poder mostrarlos. Antes se lo saltaba y el proyecto desaparecía de la vista.
    """
    cliente = _get_cliente()

    proyectos = list(Proyecto.objects.filter(
        estado="en_operacion", tipo_proyecto="minigranja", srv_operacion=True,
        deleted_at__isnull=True,
    ))
    if not proyectos:
        return {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "fleet": {"total": 0, "online": 0, "caido": 0, "degradado": 0,
                      "sin_comunicacion": 0, "sin_datos": 0, "total_capacity_kwp": 0},
            "projects": [],
        }

    clave = f"fleet:{hoy_col().isoformat()}"
    if (cacheado := _cache_get(clave)) is not None:
        return cacheado

    # Una sola llamada para toda la flota: /kpis/availability/ devuelve el mismo
    # shape que el de Solenium a propósito, así que el mapeo de estado no cambia.
    disponibilidad = cliente.get_availability() or {}

    filas = []
    capacidad_total = 0.0
    cuenta = {"online": 0, "caido": 0, "degradado": 0, "sin_comunicacion": 0,
              "sin_datos": 0}

    for p in proyectos:
        sol_id = _sv_id(p)
        disp = disponibilidad.get(sol_id, {}) if sol_id else {}
        categoria = disp.get("category")
        capacidad = float(p.potencia_ac_kw or 0)

        estado = _estado_de(categoria)
        cuenta[estado] = cuenta.get(estado, 0) + 1
        capacidad_total += capacidad

        filas.append({
            "proyecto_id": p.id,
            "nombre": p.nombre_comercial,
            "sol_id": sol_id,
            "status": estado,
            "availability_category": categoria,
            "availability_pct": disp.get("availability"),
            "capacity_kwp": round(capacidad, 1),
            # La meta del día según el P90 del mes. Va acá porque el proyecto ya
            # está cargado en este bucle: no cuesta ni una consulta más.
            #
            # Antes lo calculaba el frontend, y para eso se traía TODO el
            # listado de proyectos (~188, con las cinco relaciones anidadas del
            # serializer de /proyectos) para leer un array de 12 números de las
            # ~47 plantas de esta vista. Era la petición más pesada de la
            # pantalla y existía para eso.
            "p90_diario_kwh": _p90_del_dia(p),
        })

    filas.sort(key=lambda x: (ORDEN_ESTADO.get(x["status"], 5), x["nombre"] or ""))

    datos = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "fleet": {
            "total": len(proyectos),
            "online": cuenta["online"],
            "caido": cuenta["caido"],
            "degradado": cuenta["degradado"],
            "sin_comunicacion": cuenta["sin_comunicacion"],
            "sin_datos": cuenta.get("sin_datos", 0),
            "total_capacity_kwp": round(capacidad_total, 1),
        },
        "projects": filas,
    }
    _cache_set(clave, CACHE_TTL_FLOTA, datos)
    return datos


def _nodos_gaia(gaia, proyecto_id: int) -> tuple:
    """`(node_principal, node_respaldo)` del proyecto, o `(None, None)`.

    Se resuelve por `fronteras.proyecto_id`, un camino que no depende de ningún
    proveedor externo — por eso funciona aunque el proyecto no tenga id de
    SolarView.
    """
    from app.services.mgs.gaia_client import (
        build_db_proyecto_frt_map, find_gaia_node_pair,
    )

    fronteras = list(
        Frontera.objects.filter(
            tipo_frontera__in=TIPOS_GENERACION, codigo_frontera__isnull=False,
        ).values_list("proyecto_id", "codigo_frontera")
    )
    return find_gaia_node_pair(
        gaia=gaia,
        proyecto_id=proyecto_id,
        db_proyecto_frt_map=build_db_proyecto_frt_map(fronteras),
    )


def _curva_potencia(datos: dict | None) -> list[dict]:
    """`[{time, kw}]` de la curva de hoy.

    Con `total_power=1` (ver `SolarViewClient.get_power`) la API ya entrega la
    potencia SUMADA entre todos los inversores, o sea un `{ts: kw}` plano. La
    API vieja de Solenium devolvía `{inversor: {ts: kw}}` y había que sumar acá;
    ese loop anidado descartaba en silencio la respuesta nueva, porque los
    valores son números y no dicts.
    """
    crudo = {}
    if isinstance(datos, dict):
        crudo = (datos.get("power")
                 or (datos.get("results") or {}).get("power")
                 or {})
    total: dict[str, float] = {}
    for ts, val in crudo.items():
        try:
            total[ts] = float(val or 0)
        except (TypeError, ValueError):
            continue
    return [{"time": ts, "kw": round(v, 2)} for ts, v in sorted(total.items())]


def _generacion_30d(crudo: dict | None) -> list[dict]:
    """`[{date, kwh}]` de los últimos 30 días, desde `get_energy(day)`.

    La unidad viene declarada en la respuesta y puede ser kWh o MWh: se lee, no
    se asume.
    """
    resultados = crudo.get("results") if isinstance(crudo, dict) else None
    puntos = resultados.get("points") if isinstance(resultados, dict) else None
    unidad = ((resultados.get("unit") or "kWh").strip().lower()
              if isinstance(resultados, dict) else "kwh")
    factor = 1000.0 if unidad == "mwh" else 1.0

    diario: dict[str, float] = {}
    if isinstance(puntos, list):
        for item in puntos:
            if not isinstance(item, dict):
                continue
            dia = item.get("time") or item.get("date") or item.get("day")
            val = item.get("kwh")
            if val is None:
                val = item.get("value") or item.get("energy")
            if dia and val is not None:
                dia = str(dia)[:10]
                diario[dia] = diario.get(dia, 0.0) + float(val) * factor
    return [{"date": d, "kwh": round(v, 1)} for d, v in sorted(diario.items())]


def monitoreo_detalle(proyecto_id: int, incluir_snapshot: bool = False,
                      incluir_30d: bool = True) -> dict:
    """Detalle de un proyecto: curva de potencia de hoy, 30 días y medidores.

    Sin id de SolarView NO se corta: los inversores quedan sin dato, pero el
    medidor se resuelve por `fronteras.proyecto_id`. Antes esto era un 422 que
    dejaba la tarjeta entera vacía, incluida la mitad que sí tenía con qué
    llenarse (2026-09-03).

    `incluir_snapshot` agrega el snapshot ELÉCTRICO del medidor (voltaje,
    corriente y potencia por fase). Es lo que necesita el diagrama fasorial, y
    lo único para lo que sigue haciendo falta el compuesto de 8 familias de
    variables del nodo. Va detrás de un flag porque cuesta una llamada por
    nodo: las ~47 tarjetas no lo usan y el fasorial se abre de a uno
    (2026-09-05 -- sin esto FasorialButton.vue quedó sin datos).

    `incluir_30d` trae la serie diaria del último mes (`generation_30d` y
    `total_30d_kwh`). Cuesta una llamada externa por tarjeta, y la vista web de
    Generación Solar NO dibuja esa serie: la usa solo como tercera opción para
    leer el kWh de HOY cuando `generation_today_kwh` viene vacío. Por eso puede
    apagarla.

    **Viene en True a propósito, al revés que `incluir_snapshot`.** Este dato ya
    lo servía el endpoint, y quién más lo consume no se puede ver desde acá --
    la app móvil está en otro repositorio. Un flag que hay que pedir para seguir
    recibiendo lo de siempre rompe callado al que no se entere; uno que hay que
    pedir para dejar de recibirlo, no rompe a nadie.
    """
    from app.services.mgs.medidor_tiempo_real import elegir_medidor, snapshot_medidor

    p = _proyecto_o_404(proyecto_id)

    clave = (f"detail:{proyecto_id}:{hoy_col().isoformat()}"
             f":{int(incluir_snapshot)}:{int(incluir_30d)}")
    if (cacheado := _cache_get(clave)) is not None:
        return cacheado

    sol_id = _sv_id(p)
    cliente = _get_cliente()
    gaia = _get_gaia()

    hoy = hoy_col()
    hoy_str = hoy.isoformat()
    desde30 = (hoy - timedelta(days=29)).isoformat()

    node_principal, node_respaldo = _nodos_gaia(gaia, p.id)
    capacidad_mw = float(p.potencia_ac_kw or 0) / 1000 or None

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_pot = pool.submit(cliente.get_power, sol_id, hoy_str, hoy_str) if sol_id else None
        # Los 30 dias solo si los piden: ver `incluir_30d` en el docstring.
        f_gen = (pool.submit(cliente.get_energy, sol_id, granularity="day",
                             date_from=desde30, date_to=hoy_str)
                 if (sol_id and incluir_30d) else None)
        f_hoy = pool.submit(cliente.get_generation, sol_id, hoy_str, hoy_str) if sol_id else None
        # Medidor: `ap` + `eae` por el mismo método que usa el pipeline del
        # ASIC, en vez del compuesto de 8 familias de variables (que para dos
        # nodos eran hasta 16 llamadas externas por tarjeta).
        f_med_p = (pool.submit(snapshot_medidor, gaia, node_principal, hoy_str, capacidad_mw)
                   if (gaia and node_principal) else None)
        f_med_r = (pool.submit(snapshot_medidor, gaia, node_respaldo, hoy_str, capacidad_mw)
                   if (gaia and node_respaldo) else None)
    close_old_connections()

    gen_hoy = (f_hoy.result() or {}) if f_hoy else {}
    # No se usa `total_generation_kwh` de la respuesta: viene con los picos
    # espurios adentro. Se recalcula sumando solo las horas plausibles.
    gen_hoy_res = gen_hoy.get("results", gen_hoy) if isinstance(gen_hoy, dict) else {}
    mapa_gen = (gen_hoy_res or {}).get("generation_kwh") or {}
    kwh_hoy = _suma_kwh_inversor_hoy(mapa_gen, hoy_str, p.potencia_ac_kw) or None

    # Hasta qué hora cubre ese total, para poder decirlo igual que el medidor:
    # son horas sumadas, no una lectura del último instante.
    limite = _limite_hora_kwh(p.potencia_ac_kw)
    horas_ok = [
        k for k, v in mapa_gen.items()
        if str(k).startswith(hoy_str) and _es_hora_plausible(v, limite)
    ]
    hasta = max(horas_ok)[11:16] if horas_ok else None

    med_p = f_med_p.result() if f_med_p else None
    med_r = f_med_r.result() if f_med_r else None

    # La elección vive SOLO acá. Antes el mismo criterio ("mayor energía")
    # estaba escrito también en SolarLiveView.vue, y podían desincronizarse en
    # silencio: la gráfica mostrando un medidor y el resto de la tarjeta otro.
    medidor, medidor_tipo = elegir_medidor(med_p, med_r)

    # Snapshot eléctrico completo, solo si lo piden (ver el docstring).
    snap_p = snap_r = None
    if incluir_snapshot and gaia:
        with ThreadPoolExecutor(max_workers=2) as pool_snap:
            f_sp = pool_snap.submit(gaia.get_node_electrical_snapshot, node_principal)                 if node_principal else None
            f_sr = pool_snap.submit(gaia.get_node_electrical_snapshot, node_respaldo)                 if node_respaldo else None
        snap_p = f_sp.result() if f_sp else None
        snap_r = f_sr.result() if f_sr else None
    mejor_nodo = medidor["node_id"] if medidor else (node_principal or node_respaldo)

    generacion_30d = _generacion_30d((f_gen.result() or {}) if f_gen else {})
    total_30d = round(sum(d["kwh"] for d in generacion_30d), 1) if incluir_30d else None

    datos = {
        "proyecto_id": p.id,
        "nombre": p.nombre_comercial,
        "sol_id": sol_id,
        "gaia_node_id": mejor_nodo,
        "gaia_node_principal": node_principal,
        "gaia_node_respaldo": node_respaldo,
        "capacity_kwp": float(p.potencia_ac_kw or 0),
        "power_curve": _curva_potencia((f_pot.result() or {}) if f_pot else {}),
        "generation_today_kwh": round(kwh_hoy, 1) if kwh_hoy is not None else None,
        "generation_today_hasta": hasta,
        "generation_30d": generacion_30d,
        "total_30d_kwh": total_30d,
        "has_strings": False,
        # Medidor ya elegido y resuelto — el frontend lo dibuja, no lo decide.
        "medidor": medidor,
        "medidor_tipo": medidor_tipo,
        "medidor_principal": med_p,
        "medidor_respaldo": med_r,
        # Solo con incluir_snapshot=True; si no, van en None.
        "gaia_snapshot": snap_p if medidor_tipo == "principal" else snap_r,
        "gaia_snapshot_principal": snap_p,
        "gaia_snapshot_respaldo": snap_r,
    }
    _cache_set(clave, CACHE_TTL_DETALLE, datos)
    return datos


def potencia_inversores(proyecto_id: int, date_from: str | None = None,
                        date_to: str | None = None) -> dict:
    """Potencia por inversor (serie temporal) en un rango de fechas.

    SolarView devuelve `power` como dict llaveado por `dev_name` **solo si se
    pide con total_power=0** -- ver SolarViewClient.get_power, donde ese
    parametro decide la FORMA de la respuesta y no solo su contenido. Acá se
    normaliza a una lista de series —una por inversor— que el front usa tanto
    para la gráfica comparativa como para la individual.

    Sin fechas → hoy (resolución 5 min). En rangos de varios días se agrupa por
    hora.
    """
    p = _proyecto_o_404(proyecto_id)
    if not p.project_id_solarview:
        raise NoProcesable("Proyecto sin ID SolarView")

    sol_id = _sv_id(p)
    # `date.today()` y no `hoy_col()`: es lo que hace FastAPI acá, y cambiarlo
    # movería el rango por defecto entre las 19:00 y medianoche de Bogotá.
    from datetime import date

    hoy = date.today().isoformat()
    desde = date_from or hoy
    hasta = date_to or hoy

    cliente = _get_cliente()
    # total_power=0 -> series POR INVERSOR ({dev_name: {ts: kw}}), que es lo que
    # esta funcion arma. Con el default (1) la API entrega la potencia YA SUMADA
    # del proyecto en un {ts: kw} plano, y el loop de abajo -- que salta lo que
    # no sea dict -- descartaba la respuesta entera: cero inversores, sin
    # ningun error. La hoja de inversores de la app movil quedaba vacia.
    crudo = cliente.get_power(sol_id, desde, hasta, total_power=0) or {}
    potencia = crudo.get("power") or (crudo.get("results") or {}).get("power") or {}

    varios_dias = desde != hasta
    inversores: list[dict] = []
    for dev_name, serie in potencia.items():
        if not isinstance(serie, dict):
            continue
        pts = sorted(serie.items())
        if varios_dias:
            # Agrupar por hora: promedio de potencia por franja "YYYY-MM-DD HH".
            baldes: dict[str, list[float]] = {}
            for ts, v in pts:
                baldes.setdefault(str(ts)[:13], []).append(float(v or 0))
            puntos = [{"time": f"{k}:00", "kw": round(sum(vs) / len(vs), 2)}
                      for k, vs in sorted(baldes.items())]
        else:
            puntos = [{"time": str(ts), "kw": round(float(v or 0), 2)} for ts, v in pts]
        pico = max((pt["kw"] for pt in puntos), default=0.0)
        inversores.append({"dev_name": dev_name, "points": puntos,
                           "peak_kw": round(pico, 2)})

    inversores.sort(key=lambda x: x["dev_name"])
    return {
        "proyecto_id": p.id,
        "sol_id": sol_id,
        "date_from": desde,
        "date_to": hasta,
        "granularidad": "hour" if varios_dias else "5min",
        "inverters": inversores,
    }
