"""Curva horaria de medidor (nodo de monitoreo en Quoia) -- generación (eae)
y consumo (iae) del mismo medidor físico.

Puerto de process/src/internals/medidores.py (repo Reporte-Energia), usando
el GaiaClient ya existente del backend en vez de un cliente propio.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

# `ponytail: los clientes de Quoia y SolarView siguen en app/services/mgs/`.
# Son HTTP puro, sin sesión de base: se mueven cuando se retire FastAPI.
from app.services.mgs.gaia_client import GaiaClient
from apps.energia.services.reporte import recuperacion
from apps.energia.services.reporte.utils import limite_plausible_kwh

logger = logging.getLogger("reporte_energia.curvas")

HORAS = list(range(24))

# Cachés cortas (mismo patrón que gaia_client._get_dynamic_maps, TTL más
# corto) para el mapa medidor->nodo y borders->frt_code -- sin esto, cada
# clic en el detalle de una frontera (solo para mostrar la curva de
# referencia) volvía a traer el catálogo COMPLETO de Quoia (todos los nodos,
# todos los borders), no solo el de esa frontera. El reporte real
# (orquestador.ejecutar_dia) corre una vez al día y no necesita el TTL --
# esto es sobre todo para la vista de detalle, que se abre repetidas veces
# en la misma sesión. Subido de 300 a 1800 (30 min) -- este catálogo (qué
# medidor/nodo tiene cada frontera) casi no cambia día a día, así que un
# TTL más largo reduce cuánto se paga el costo de refrescarlo (~5-9s en
# frío) sin perder nada de precisión real (los VALORES de medición se
# siguen consultando frescos siempre, solo el mapeo de IDs se reusa más).
_CACHE_TTL = 1800  # segundos

# **El cache vive en Redis, no en el proceso.** Estos catalogos se guardaban en
# variables globales del modulo, y eso alcanzaba cuando `WORKERS` tenia que ser
# 1 (el BackgroundScheduler vivia dentro del proceso web). Al levantar esa
# restriccion, gunicorn corre con `--workers 3` y cada worker quedo con su
# propia copia: abrir tres fronteras seguidas caia tipicamente en tres procesos
# distintos y cada uno pagaba el fetch completo (~5-9s) por separado. El TTL de
# 30 minutos daba una falsa sensacion de barato.
#
# Redis ya estaba puesto para esto: `CACHES` en config/settings.py apunta al
# mismo Redis del compose y su comentario dice, textualmente, que es para
# "estado efimero (que es lo que era antes: memoria de un proceso)".
#
# Se conserva un primer nivel EN EL PROCESO porque no es redundante: evita
# deserializar el catalogo completo de fronteras en cada apertura del panel.
# Redis es el segundo nivel, el que comparten los tres workers.
_CLAVE_MAPA_NODO = "reporte_energia:mapa_medidor_nodo"
_CLAVE_BORDERS = "reporte_energia:borders_crudos"

_local: dict[str, tuple[float, object]] = {}


def _cacheado(clave: str, construir, usar_cache: bool = True):
    """Devuelve el valor de `clave`, construyendolo solo si hace falta.

    Tres capas, de la mas barata a la mas cara: memoria del proceso, Redis, y
    la llamada real a Quoia. `usar_cache=False` las saltea las dos primeras y
    refresca ambas -- lo usa la corrida diaria, que quiere el catalogo fresco.

    **Si Redis no responde, no se rompe nada**: se cae al comportamiento
    anterior (cache por proceso y, en el peor caso, el fetch). Un catalogo de
    IDs no vale una caida del panel.
    """
    from django.core.cache import cache

    now = time.monotonic()
    if usar_cache:
        guardado = _local.get(clave)
        if guardado is not None and (now - guardado[0]) < _CACHE_TTL:
            return guardado[1]
        try:
            de_redis = cache.get(clave)
        except Exception:
            de_redis = None
        if de_redis is not None:
            _local[clave] = (now, de_redis)
            return de_redis

    valor = construir()
    _local[clave] = (now, valor)
    try:
        cache.set(clave, valor, _CACHE_TTL)
    except Exception:
        pass  # sin Redis el cache queda por proceso, como antes
    return valor

UMBRAL_GENERACION_KWH = 0.5   # kWh por hora mínimo para considerar la hora "generando"
HORA_MINIMA_CIERRE    = 18    # el medidor debe seguir reportando al menos hasta esta hora
HORA_MAXIMA_APERTURA  = 6     # el medidor debe empezar a reportar a más tardar a esta hora

# Nodos cuyo firmware reporta eaepd/iaepd en Wh en vez de kWh -- vacío hasta
# encontrar un caso real (mismo criterio ya usado en Reporte-Energia).
EAE_WH_NODES: frozenset[int] = frozenset()

_CAMPOS_POR_VAR = {
    "eae": ("eaepd1", "eaepd2", "eaepd3"),
    "iae": ("iaepd1", "iaepd2", "iaepd3"),
}


def construir_mapa_medidor_nodo(gaia: GaiaClient, usar_cache: bool = True) -> dict[int, int]:
    """meter_id -> node_id, desde /api/node/retailer/ (gaia.get_all_nodes()).

    main_meter/backup_meter de un border de Quoia son IDs administrativos de
    medidor; el endpoint de mediciones usa node_id, un espacio de IDs
    distinto -- este mapa es la conversión, igual que
    process/src/internals/nodos_quoia.py en Reporte-Energia.

    Cacheado _CACHE_TTL segundos (usar_cache=False fuerza traerlo de nuevo --
    la corrida real del día usa esto una sola vez, así que no le hace falta,
    pero no le molesta tampoco).
    """
    def construir() -> dict[int, int]:
        mapa: dict[int, int] = {}
        for node in gaia.get_all_nodes():
            meter = node.get("meter") or {}
            nid = node.get("id")
            mid = meter.get("id") if isinstance(meter, dict) else None
            if mid is not None and nid is not None:
                mapa[int(mid)] = int(nid)
        return mapa

    mapa = _cacheado(_CLAVE_MAPA_NODO, construir, usar_cache)
    return mapa


def obtener_borders_crudos(gaia: GaiaClient, usar_cache: bool = True) -> list[dict]:
    """Catálogo completo de fronteras de Quoia (gaia.get_all_borders()), sin
    transformar -- cacheado _CACHE_TTL segundos, mismo criterio que
    construir_mapa_medidor_nodo()/construir_mapa_borders().

    Punto único de acceso al catálogo completo para evitar traerlo dos veces
    en la misma request: antes construir_mapa_borders() (este módulo) y
    resolver_borders() (reporte_cgm.py, servicio de envío del reporte CGM a
    clientes/operadores) cada uno llamaba a gaia.get_all_borders() por su
    cuenta -- con caches independientes, ambos podían fallar en frío en la
    misma request (ej. "Cliente" que además dispara el resumen mensual) y
    pagar el fetch completo dos veces (~5-9s cada uno, auditoría CGM
    2026-08-26, finding #2)."""
    return _cacheado(_CLAVE_BORDERS, gaia.get_all_borders, usar_cache)


def construir_mapa_borders(gaia: GaiaClient, usar_cache: bool = True) -> dict[str, dict]:
    """frt_code (lowercase) -> {border_id, main_meter, backup_meter} desde
    obtener_borders_crudos() (cacheado 30 min)."""
    mapa: dict[str, dict] = {}
    for proyecto in obtener_borders_crudos(gaia, usar_cache):
        for key in ("frt_generation", "frt_consumption"):
            frt = proyecto.get(key)
            if not frt:
                continue
            frt_code = (frt.get("frt_code") or "").strip().lower()
            if not frt_code:
                continue
            mapa[frt_code] = {
                "border_id": frt.get("id"),
                "main_meter": frt.get("main_meter"),
                "backup_meter": frt.get("backup_meter"),
            }

    return mapa


def _horas_reportadas(filas: list[dict]) -> set[int]:
    """Horas (0-23) para las que llegó al menos una fila en la respuesta de la API,
    sin importar su valor — permite distinguir 'no generó' de 'no reportó'."""
    horas = set()
    for fila in filas:
        ts = fila.get("time", "")
        try:
            hora = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        if 0 <= hora < 24:
            horas.add(hora)
    return horas


def dia_completo(curva: pd.Series, horas_con_dato: set[int]) -> bool:
    """True si no hay huecos de reporte dentro de la ventana real de generación.

    Revisa huecos en ambos extremos del día -- que el medidor haya empezado a
    reportar a más tardar a HORA_MAXIMA_APERTURA y que haya seguido hasta al
    menos HORA_MINIMA_CIERRE -- antes de solo mirar gaps DENTRO de la ventana
    de generación detectada (una hora sin sol nunca es un hueco, así que solo
    mirar gaps internos no detecta un medidor que nunca empezó a reportar o
    que dejó de reportar antes de tiempo).
    """
    horas_generando = [h for h, v in curva.items() if pd.notna(v) and v > UMBRAL_GENERACION_KWH]
    if not horas_generando:
        return False  # no generó nada ese día -- no es este chequeo el que decide (ver Caso 6)

    if max(horas_con_dato, default=-1) < HORA_MINIMA_CIERRE:
        return False  # el medidor dejó de reportar temprano

    if min(horas_con_dato, default=24) > HORA_MAXIMA_APERTURA:
        return False  # el medidor empezó a reportar demasiado tarde

    inicio, fin = min(horas_generando), max(horas_generando)
    return all(h in horas_con_dato for h in range(inicio, fin + 1))


def _curva_de_mediciones(filas: list[dict], node_id: int, campos: tuple[str, str, str]) -> pd.Series:
    """Convierte lista de mediciones de nodo -> pd.Series[0..23] kWh por hora.

    Las horas sin NINGUNA fila real quedan en NaN, no 0.0 -- un hueco de
    telemetría (el medidor dejó de reportar) es indistinguible de "generó/
    consumió cero" si se inicializa todo en 0.0 de entrada.
    """
    en_wh = node_id in EAE_WH_NODES
    acum = {h: 0.0 for h in HORAS}
    horas_con_dato = set()
    for fila in filas:
        ts = fila.get("time", "")
        try:
            hora = int(ts[11:13])
        except (ValueError, IndexError):
            continue
        if not (0 <= hora < 24):
            continue
        valor = sum(float(fila.get(f, 0) or 0) for f in campos)
        acum[hora] += valor / 1000 if en_wh else valor
        horas_con_dato.add(hora)
    curva = pd.Series(acum, dtype=float)
    for h in HORAS:
        if h not in horas_con_dato:
            curva[h] = None
    return curva


def _curva_nodo(
    gaia: GaiaClient, node_id: int | None, fecha_str: str, label: str, var_name: str = "eae",
    capacidad_efectiva_mw: float | None = None,
) -> tuple[pd.Series, bool]:
    """Retorna (curva horaria kWh, dia_completo) para un node_id.

    var_name: 'eae' (generación, por defecto) o 'iae' (consumo).
    Serie de None×24 y completo=False si no hay nodo o no hay datos.

    Si se pasa capacidad_efectiva_mw, las horas físicamente implausibles
    (mismo criterio que ya usan reconectador.get_curva_reconectador() y
    solarview.curva_generacion() -- ver limite_plausible_kwh() en utils.py)
    se tratan como huecos (None), no como lectura real: un glitch de
    telemetría del medidor de nodo puede colarse igual que uno de
    SolarView, y acá el riesgo es mayor -- esta curva se reporta DIRECTO
    (sin FP de por medio) en Casos 2/4/5.
    """
    vacia = pd.Series([None] * 24, index=HORAS, dtype=float)
    if node_id is None:
        return vacia, False
    filas = gaia.get_node_measurements(node_id, fecha_str, var_name)
    if not filas:
        return vacia, False
    curva = _curva_de_mediciones(filas, node_id, _CAMPOS_POR_VAR[var_name])
    horas_reportadas = _horas_reportadas(filas)

    limite = limite_plausible_kwh(capacidad_efectiva_mw)
    if limite is not None:
        implausibles = curva.abs() > limite
        if implausibles.any():
            curva[implausibles] = None
            horas_reportadas -= set(curva.index[implausibles])

    completo = dia_completo(curva, horas_reportadas)
    return curva, completo


def curva_medidor_en_vivo(
    gaia: GaiaClient,
    mapa_medidor_nodo: dict[int, int],
    main_meter_id: int | None,
    backup_meter_id: int | None,
    fecha_str: str,
    frt_code: str,
    var_name: str = "eae",
    capacidad_efectiva_mw: float | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Curva EN VIVO (principal, respaldo) de UNA sola variable (eae o iae),
    sin recuperación activa -- pensado para 'Detalle de las fuentes' del
    front, que solo necesita esto para detectar si Quoia ya cambió desde
    que se clasificó (medidor_actualizado_en_quoia). curvas_de_frontera()
    trae las 4 curvas (eae+iae x principal+respaldo) de forma secuencial
    porque el clasificador SÍ necesita las 4 -- acá solo hacían falta 2, y
    en paralelo, no en secuencia (era el 4x de más peso en la demora que
    reportó el usuario al abrir el panel, 2026-08-12).

    Si se pasa capacidad_efectiva_mw, mismo criterio de plausibilidad que
    ya usa el resto de la clasificación (ver limite_plausible_kwh() en
    utils.py) -- un glitch de telemetría también puede colarse acá,
    inflando la escala del gráfico o disparando un falso aviso de
    "el medidor cambió" contra un valor corrupto (ver MGS 0010 Villanueva
    2026-08-26)."""
    node_p = mapa_medidor_nodo.get(int(main_meter_id)) if main_meter_id else None
    node_r = mapa_medidor_nodo.get(int(backup_meter_id)) if backup_meter_id else None
    with ThreadPoolExecutor(max_workers=2) as executor:
        fut_p = executor.submit(_curva_nodo, gaia, node_p, fecha_str, f"{frt_code}/principal", var_name, capacidad_efectiva_mw)
        fut_r = executor.submit(_curva_nodo, gaia, node_r, fecha_str, f"{frt_code}/respaldo", var_name, capacidad_efectiva_mw)
        curva_p, _ = fut_p.result()
        curva_r, _ = fut_r.result()
    return curva_p, curva_r


def recuperar_y_releer(
    gaia: GaiaClient, node_id: int, meter_id: int, fecha_str: str, label: str, var_name: str = "eae",
    capacidad_efectiva_mw: float | None = None,
) -> tuple[pd.Series, bool, bool]:
    """Interroga el medidor (recuperación activa vía WebSocket) y vuelve a
    leer su curva desde Quoia.

    Solo tiene sentido llamarla cuando la lectura pasiva ya salió
    incompleta -- recuperar un medidor ya completo no cambia nada.

    Retorna (curva, completo, exito) -- 'exito' es si Quoia confirmó
    'status': 'success' en la interrogación (no si el día quedó completo
    después, eso ya lo dice 'completo')."""
    resultado = recuperacion.recuperar_datos_medidor(meter_id, fecha_str, fecha_str)
    exito = recuperacion.fue_exitosa(resultado)
    if not exito:
        logger.info("recuperacion %s meter_id=%s no confirmó éxito: %s", label, meter_id, resultado)
    curva, completo = _curva_nodo(
        gaia, node_id, fecha_str, f"{label} (post-recuperacion)", var_name, capacidad_efectiva_mw,
    )
    return curva, completo, exito


TOLERANCIA_VALOR_SOSPECHOSO = 0.50  # %: qué tan lejos de mediana_referencia antes de forzar recuperación


def curvas_de_frontera(
    gaia: GaiaClient,
    mapa_medidor_nodo: dict[int, int],
    main_meter_id: int | None,
    backup_meter_id: int | None,
    fecha_str: str,
    frt_code: str,
    recuperar: bool = True,
    mediana_referencia: float | None = None,
    capacidad_efectiva_mw: float | None = None,
) -> dict:
    """Curvas horarias (kWh) de generación (eae) y consumo propio (iae) del
    medidor principal y de respaldo de una frontera, para una fecha.

    'Consumo propio' acá es el autoconsumo del medidor de GENERACIÓN (equipos
    tomando de red en horas de bajo sol) -- no es una frontera de consumo
    aparte, es el mismo medidor, otra variable (iae en vez de eae).

    Si `recuperar` es True (default) y la lectura pasiva de un medidor sale
    incompleta en CUALQUIERA de sus dos variables (eae o iae), se interroga
    ese medidor UNA sola vez -- la interrogación reenvía todas las lecturas
    del dispositivo físico sin importar la variable, así que no hace falta
    interrogar dos veces el mismo medidor -- y se vuelven a leer ambas
    curvas (eae e iae) después.

    `mediana_referencia` (opcional, default None -- no cambia nada para
    quien no lo pasa) agrega un segundo motivo para recuperar: aunque la
    lectura pasiva venga "completa", si su total se aleja de esta mediana
    más de TOLERANCIA_VALOR_SOSPECHOSO, se interroga igual -- la
    completitud no detecta un glitch de telemetría que reporta un valor
    doblado o partido a la mitad, solo huecos (ver MGS 0032 El Paso Norte /
    Sol&Cielo 7 Los Bongos: el medidor venía "completo" pero exactamente 2x
    su valor normal).

    `capacidad_efectiva_mw` (opcional) se pasa a _curva_nodo() para
    descartar horas físicamente implausibles -- ver MGS 0010 Villanueva
    2026-08-26: el mismo tipo de glitch que ya se filtraba en SolarView/
    reconectador puede venir del medidor de nodo también, y acá el riesgo
    es mayor -- estas curvas se reportan DIRECTO (Casos 2/4/5), sin FP de
    por medio que amortigüe un valor absurdo.
    """
    node_p = mapa_medidor_nodo.get(int(main_meter_id)) if main_meter_id else None
    node_r = mapa_medidor_nodo.get(int(backup_meter_id)) if backup_meter_id else None

    curva_p, comp_p = _curva_nodo(gaia, node_p, fecha_str, f"{frt_code}/principal", "eae", capacidad_efectiva_mw)
    curva_r, comp_r = _curva_nodo(gaia, node_r, fecha_str, f"{frt_code}/respaldo", "eae", capacidad_efectiva_mw)
    cons_p, cons_comp_p = _curva_nodo(gaia, node_p, fecha_str, f"{frt_code}/principal/consumo", "iae", capacidad_efectiva_mw)
    cons_r, cons_comp_r = _curva_nodo(gaia, node_r, fecha_str, f"{frt_code}/respaldo/consumo", "iae", capacidad_efectiva_mw)

    def _sospechoso(curva: pd.Series) -> bool:
        if not mediana_referencia or mediana_referencia <= 0:
            return False
        total = float(curva.fillna(0).sum())
        if total == 0:
            return False
        return abs(total - mediana_referencia) / mediana_referencia > TOLERANCIA_VALOR_SOSPECHOSO

    intentos: list[str] = []
    if recuperar:
        if node_p is not None and main_meter_id and (not (comp_p and cons_comp_p) or _sospechoso(curva_p)):
            curva_p, comp_p, exito = recuperar_y_releer(gaia, node_p, int(main_meter_id), fecha_str, f"{frt_code}/principal", "eae", capacidad_efectiva_mw)
            cons_p, cons_comp_p, _ = recuperar_y_releer(gaia, node_p, int(main_meter_id), fecha_str, f"{frt_code}/principal/consumo", "iae", capacidad_efectiva_mw)
            intentos.append(f"principal: {'éxito' if exito else 'falló'}")
        if node_r is not None and backup_meter_id and (not (comp_r and cons_comp_r) or _sospechoso(curva_r)):
            curva_r, comp_r, exito = recuperar_y_releer(gaia, node_r, int(backup_meter_id), fecha_str, f"{frt_code}/respaldo", "eae", capacidad_efectiva_mw)
            cons_r, cons_comp_r, _ = recuperar_y_releer(gaia, node_r, int(backup_meter_id), fecha_str, f"{frt_code}/respaldo/consumo", "iae", capacidad_efectiva_mw)
            intentos.append(f"respaldo: {'éxito' if exito else 'falló'}")

    return {
        "node_ppal": node_p, "node_resp": node_r,
        "curva_ppal": curva_p, "curva_resp": curva_r,
        "ppal_completo": comp_p, "resp_completo": comp_r,
        "consumo_ppal": cons_p, "consumo_resp": cons_r,
        "consumo_ppal_completo": cons_comp_p, "consumo_resp_completo": cons_comp_r,
        "recuperacion_datos": ", ".join(intentos) or None,
    }
