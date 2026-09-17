"""Sync HTTP client for Gaia (Quoia CGM) API — JWT auth.

Provides access to:
  - /api/cgm/v1/border/          → grid connection borders
  - /api/node/{id}/measurements/ → electrical node measurements (V, I, P, Q, PF, energy)

Authentication uses username/password JWT (Bearer token), different from the
legacy QuoiaClient which uses a static API Token.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger("mgs.gaia")

TIMEOUT = 30.0
TOKEN_REFRESH_SEC = 4 * 60   # refresh access token every 4 min (they typically expire in 5)

# Nodos cuyo firmware reporta eaepd en Wh en lugar de kWh.
# Para todos los demás se asume kWh directamente.
_EAE_WH_NODES: frozenset[int] = frozenset()  # no hay nodos con quirk Wh conocidos actualmente

# ── Hardcoded node map: frt_code → (principal_node_id, respaldo_node_id) ──────
# Source: nodos.json from the Plataforma Central de Monitoreo.
# Key = frontera SIC code, Value = (principal, respaldo) — None if not registered.
FRONTERA_NODE_MAP: dict[str, tuple[int | None, int | None]] = {
    "frt55044": (603,  604),   # MINIGRANJA SOLAR BARAYA
    "frt55090": (609,  610),   # MINIGRANJA SOLAR GANDALF
    "frt55093": (606,  607),   # MINIGRANJA SOLAR CAÑAHUATE
    "frt58839": (845,  846),   # Minigranja 005 - La Paz Vallenata
    "frt60629": (848,  849),   # Minigranja Solar 0006 - Perijá
    "frt63879": (1022, 1020),  # Minigranja 0009 La Paz Verso
    "frt65205": (1283, 1282),  # Minigranja 0018 - La Paz Leyenda
    "frt66597": (1459, 1460),  # Minigranja 0017 - La Paz Esmeralda
    "frt_olimpo14": (1579, 1580), # MGS 0014 - El Olimpo
    "frt67475": (1481, 1482),  # MGS 0015 - El Son
    "frt67496": (1489, 1490),  # MGS 0019 - El Merengue
    "frt68269": (1514, 1515),  # MGS 0016 - La Puya
    "frt73414": (1590, 1591),  # MGS 0021 - Ibirico
    "frt74080": (1584, 1585),  # MGS 0022 - La Cumbia
    "frt76578": (1656, 1657),  # MGS 0024 - San Diego Sur
    "frt76581": (1654, 1655),  # MGS 0020 - El Mapalé
    "frt76586": (1658, 1659),  # MGS 0023 - El Joropo
    "frt82546": (1664, 1665),  # MGS 0027 - Valencia Oriente 2
    "frt82576": (1662, 1663),  # MGS 0025 - El Copey Occidente
    "frt82846": (1692, 1693),  # Sol&Cielo 7 Los Bongos
    "frt84587": (1712, 1713),  # GD San Pelayo
    "frt86234": (1660, 1661),  # MGS 0026 - Valencia Oriente
    "frt87017": (1722, 1723),  # MGS 0040 - La Cacica
    "frt87018": (1724, 1725),  # MGS 0041 - Las Piloneras
    "frt87336": (1716, 1717),  # La Catedral
    "frt89202": (1719, 1718),  # Sol&Cielo 9 - Ciénaga Generación
    "frt92219": (1730, 1731),  # MGS 0075 - Chiriguaná Norte 2
    "frt92221": (1739, 1740),  # MGS 0077 - Chiriguaná Norte 4
    "frt_reserva": (1578, 1577),  # La Reserva
    "frt_paso_norte": (1750, 1751),  # MGS 0032 - El Paso Norte
}

# ── Dynamic node map (built from live Quoia API, cached 1 h) ─────────────────
_dynamic_cache: dict | None = None
_dynamic_cache_ts: float = 0.0
_DYNAMIC_CACHE_TTL = 3600  # seconds


def _get_dynamic_maps(gaia: "GaiaClient") -> dict | None:
    """Return {frt, node_meter} maps built from live Quoia API, cached for 1 hour.

    frt: frt_code → (node_principal, node_respaldo)
    node_meter: node_id → {marca, modelo, serie}

    Returns stale cache on fetch failure rather than None so callers can
    still serve the last known data while the API is temporarily down.
    """
    global _dynamic_cache, _dynamic_cache_ts
    now = time.monotonic()
    if _dynamic_cache is not None and (now - _dynamic_cache_ts) < _DYNAMIC_CACHE_TTL:
        return _dynamic_cache
    try:
        borders = gaia.get_all_borders()
        # get_all_borders() y get_all_nodes() nunca lanzan (_get() ya traga
        # sus propias excepciones) -- pero cada una resetea
        # `gaia.ultima_llamada_fallo` al empezar, así que si la primera
        # falla y la segunda tiene éxito, el estado final del atributo
        # ocultaría la falla real. Se guarda aparte para no perderla.
        fallo_borders = gaia.ultima_llamada_fallo
        nodes = gaia.get_all_nodes()
        if fallo_borders:
            gaia.ultima_llamada_fallo = True
    except Exception as exc:
        logger.warning("Dynamic node map fetch failed, using stale cache: %s", exc)
        gaia.ultima_llamada_fallo = True
        return _dynamic_cache
    if gaia.ultima_llamada_fallo:
        logger.warning("Dynamic node map fetch failed, using stale cache")
        return _dynamic_cache

    # meter_id → node_id, node_id → {marca, modelo, serie}
    meter_to_node: dict[int, int] = {}
    node_meter: dict[int, dict] = {}
    for node in nodes:
        meter = node.get("meter") or {}
        nid = node.get("id")
        if isinstance(meter, dict):
            mid = meter.get("id")
            if mid is not None and nid is not None:
                meter_to_node[int(mid)] = int(nid)
            info = _meter_info(meter)
            if info and nid is not None:
                node_meter[int(nid)] = info

    frt_map: dict[str, tuple[int | None, int | None]] = {}
    for border in borders:
        frt_gen = border.get("frt_generation") or {}
        if not frt_gen:
            continue
        frt_code = (frt_gen.get("frt_code") or "").lower()
        if not frt_code:
            continue
        main_m = frt_gen.get("main_meter")
        back_m = frt_gen.get("backup_meter")
        node_p = meter_to_node.get(int(main_m)) if main_m else None
        node_r = meter_to_node.get(int(back_m)) if back_m else None
        frt_map[frt_code] = (node_p, node_r)

    _dynamic_cache = {"frt": frt_map, "node_meter": node_meter}
    _dynamic_cache_ts = now
    logger.info("Dynamic node maps built: %d borders, %d nodes", len(frt_map), len(meter_to_node))
    return _dynamic_cache


def _meter_info(meter: dict) -> dict | None:
    """Extrae {marca, modelo, serie} de un `node["meter"]` de /api/node/retailer/."""
    if not isinstance(meter, dict):
        return None
    model = meter.get("model") or {}
    marca = model.get("brand") if isinstance(model, dict) else None
    modelo = model.get("model") if isinstance(model, dict) else None
    serie = meter.get("serial")
    if not (marca or modelo or serie):
        return None
    return {"marca": marca, "modelo": modelo, "serie": serie}


def get_frt_meter_info(gaia: "GaiaClient", frt_code: str) -> tuple[dict | None, dict | None]:
    """(info_principal, info_respaldo) para un frt_code, desde el mapa dinámico de nodos.

    Cada info es {marca, modelo, serie} o None si el nodo no tiene medidor
    con esos datos o el frt_code no tiene border en Quoia."""
    maps = _get_dynamic_maps(gaia)
    if not maps:
        return (None, None)
    node_p, node_r = maps["frt"].get(frt_code.lower(), (None, None))
    node_meter = maps.get("node_meter") or {}
    info_p = node_meter.get(node_p) if node_p is not None else None
    info_r = node_meter.get(node_r) if node_r is not None else None
    return (info_p, info_r)


def _mgs_number(name: str) -> int | None:
    m = re.search(r"(?:minigranja|mgs|mgr)\s+0*(\d+)", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


# Override directo proyecto_id -> (node_principal, node_respaldo).
# Para medidores que existen como nodo en Gaia pero cuyo proyecto no está
# registrado como border/frontera en Gaia, por lo que la resolución dinámica
# (que arma su mapa desde los borders) no los encuentra. Mismo espíritu que el
# fallback hardcodeado FRONTERA_NODE_MAP, pero indexado por proyecto_id.
_PROYECTO_NODE_OVERRIDE: dict[int, tuple[int | None, int | None]] = {
    45: (616, None),  # Minigranja Solar San Pedro — medidor de generación (nodo Gaia 616)
    # Autoconsumo Nestlé DPA (Valledupar). Un autoconsumo no entrega energía al
    # SIC, así que no tiene frontera de generación y Gaia no lo lista como
    # border: el mapa dinámico nunca lo encuentra por más que el nodo exista.
    # Medidor serie 88865813 → nodo 1670. Sin respaldo: es un solo medidor.
    59: (1670, None),  # Autoconsumo Nestlé DPA — medidor 88865813 (nodo Gaia 1670)
}


def _resolve_frt_and_pair(
    gaia: "GaiaClient | None" = None,
    proyecto_id: int | None = None,
    db_proyecto_frt_map: "dict[int, str] | None" = None,
) -> tuple[str | None, tuple[int | None, int | None]]:
    """Core lookup: (frt_code, (node_principal, node_respaldo)).

    Resolution order:
      0. Override directo por proyecto_id (medidores sin border en Gaia,
         ver _PROYECTO_NODE_OVERRIDE).
      1. Vínculo directo en BD: fronteras.proyecto_id -> codigo_frontera. Desde
         que se reconcilió esa columna (ETL 2026-07-02, ver
         scripts/etl_fronteras_proyectos.py) es la ÚNICA fuente de verdad --
         cubre el 100% de los proyectos con frontera de generación registrada
         (verificado 2026-07-08 contra la BD real). Si un proyecto no tiene
         este vínculo, no se adivina por nombre/número: se retorna sin match.
      2. Node pair desde el mapa dinámico de Quoia (frt_map).
      3. Hardcoded FRONTERA_NODE_MAP como fallback final -- solo para cuando
         la API de Quoia no está disponible ni siquiera para el mapa
         dinámico (un problema de disponibilidad, no de vínculos mal hechos).

    Se quitó la adivinanza por nombre/número (num_map dinámico + _find_frt
    estático) que quedaba como fallback secundario tras el vínculo directo:
    con este último cubriendo el 100% de los casos reales, esos pasos nunca
    se ejecutaban y eran la fuente de los bugs de mapeo original (Cañahuate,
    El Molino, prefijos "GD" vs "Minigranja Solar"). Si el vínculo directo de
    un proyecto llega a romperse, el fix es corregir fronteras.proyecto_id
    (correr scripts/etl_fronteras_proyectos.py), no volver a adivinar.
    """
    if proyecto_id is not None and proyecto_id in _PROYECTO_NODE_OVERRIDE:
        return (None, _PROYECTO_NODE_OVERRIDE[proyecto_id])

    frt = db_proyecto_frt_map.get(proyecto_id) if (proyecto_id is not None and db_proyecto_frt_map) else None
    if frt is None:
        return (None, (None, None))

    maps = _get_dynamic_maps(gaia) if gaia is not None else None
    if maps and frt in maps["frt"]:
        return (frt, maps["frt"][frt])

    pair = FRONTERA_NODE_MAP.get(frt)
    return (frt, pair if pair else (None, None))


def build_db_proyecto_frt_map(fronteras: list[tuple[int, str]]) -> dict[int, str]:
    """Build a proyecto_id -> frt_code map from DB fronteras (proyecto_id, codigo_frontera) pairs.

    Esta es la fuente de verdad directa desde que fronteras.proyecto_id se
    reconcilió (ver scripts/etl_fronteras_proyectos.py, 2026-07-02): reemplaza
    la necesidad de adivinar por nombre/número en la mayoría de los casos.
    Pásalo como db_proyecto_frt_map a find_gaia_node_pair junto con el
    proyecto_id del proyecto que se está resolviendo.
    """
    result: dict[int, str] = {}
    for proyecto_id, codigo in fronteras:
        if proyecto_id is not None and codigo:
            result.setdefault(int(proyecto_id), codigo.lower())
    return result


def find_gaia_node_id(
    gaia: "GaiaClient | None" = None,
    proyecto_id: int | None = None,
    db_proyecto_frt_map: "dict[int, str] | None" = None,
) -> int | None:
    """Find the principal Gaia node_id for a project. Returns None if not found."""
    _, (node_p, _) = _resolve_frt_and_pair(
        gaia=gaia, proyecto_id=proyecto_id, db_proyecto_frt_map=db_proyecto_frt_map,
    )
    return node_p


def find_gaia_node_pair(
    gaia: "GaiaClient | None" = None,
    proyecto_id: int | None = None,
    db_proyecto_frt_map: "dict[int, str] | None" = None,
) -> tuple[int | None, int | None]:
    """Find both (principal, respaldo) Gaia node IDs for a project.

    Requiere proyecto_id + db_proyecto_frt_map (construido con
    build_db_proyecto_frt_map desde fronteras.proyecto_id) -- es la única
    fuente de verdad, no se adivina por nombre. Si gaia se pasa, resuelve el
    par de nodos desde el mapa dinámico de Quoia (cacheado 1h); si no, cae al
    hardcodeado FRONTERA_NODE_MAP. Retorna (None, None) si no hay vínculo.
    """
    _, pair = _resolve_frt_and_pair(
        gaia=gaia, proyecto_id=proyecto_id, db_proyecto_frt_map=db_proyecto_frt_map,
    )
    return pair


def _col_today() -> str:
    """Current date in Colombia time (UTC-5), formatted YYYY-MM-DD."""
    col = datetime.now(timezone.utc) - timedelta(hours=5)
    return col.strftime("%Y-%m-%d")


class GaiaClient:
    """JWT-authenticated client for the Gaia CGM API.

    Handles token refresh automatically. _authenticate()/_try_refresh() (las
    únicas dos funciones que escriben _access_token/_refresh_token) están
    protegidas por _token_lock -- sin eso, dos hilos usando el mismo cliente
    en paralelo (ver curva_medidor_en_vivo, principal+respaldo a la vez)
    podían renovar el token cada uno por su cuenta y pisarse el resultado si
    uno de los dos fallaba.
    """

    def __init__(self):
        self._base = settings.GAIA_BASE_URL.rstrip("/")
        self._username = settings.GAIA_USER
        self._password = settings.GAIA_PASS
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_time: float = 0
        self._token_lock = threading.Lock()
        self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=True)
        # `_get()` traga cualquier excepción y devuelve None -- indistinguible
        # de "no hay dato" para quien llama. Esta bandera se resetea a False
        # al INICIO de cada _get() y solo queda en True si esa llamada
        # específica falló (red, timeout, error HTTP, o no se pudo
        # autenticar) -- nunca por un 404 real. Pensada para los pocos
        # llamadores que necesitan distinguir "Quoia dice que no hay nada" de
        # "no se pudo preguntar" (ver /fronteras/quoia/pendientes y los
        # backfills, que antes reportaban huecos falsos durante una caída de
        # Quoia -- diagnóstico de Fronteras, 2026-08-24).
        self.ultima_llamada_fallo: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self._username and self._password)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _authenticate(self) -> None:
        with self._token_lock:
            try:
                resp = self._http.post(f"{self._base}/api/auth/token/", json={
                    "username": self._username,
                    "password": self._password,
                })
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data["access"]
                self._refresh_token = data["refresh"]
                self._token_time = time.time()
            except Exception as exc:
                logger.error("gaia auth failed: %s", exc)
                self._access_token = None
                self._refresh_token = None

    def _try_refresh(self) -> bool:
        with self._token_lock:
            if not self._refresh_token:
                return False
            try:
                resp = self._http.post(f"{self._base}/api/auth/token/refresh/",
                                       json={"refresh": self._refresh_token})
                resp.raise_for_status()
                self._access_token = resp.json()["access"]
                self._token_time = time.time()
                return True
            except Exception:
                return False

    def _ensure_token(self) -> None:
        if not self._access_token:
            self._authenticate()
            return
        if time.time() - self._token_time > TOKEN_REFRESH_SEC:
            if not self._try_refresh():
                self._authenticate()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        """Wrapper histórico -- guarda el resultado en self.ultima_llamada_fallo
        (bandera compartida de la instancia) para los llamadores existentes,
        todos secuenciales hoy. Insegura bajo concurrencia -- ver
        _get_con_estado()/get_border_report_status_con_estado() para el caso
        de varias llamadas en paralelo sobre el mismo GaiaClient (ej.
        reporte_cgm.py, ThreadPoolExecutor reusando un solo cliente)."""
        data, fallo = self._get_con_estado(url, params)
        self.ultima_llamada_fallo = fallo
        return data

    def _get_con_estado(self, url: str, params: dict | None = None) -> tuple[dict | list | None, bool]:
        """Núcleo real de _get() -- retorna (data, fallo) como tupla LOCAL a
        esta llamada, sin tocar self.ultima_llamada_fallo. Thread-safe: no
        depende de ningún estado compartido de la instancia salvo el token
        (ya protegido por _token_lock)."""
        self._ensure_token()
        if not self._access_token:
            return None, True
        try:
            resp = self._http.get(url, headers=self._headers(), params=params)
            if resp.status_code == 401:
                # One retry after re-auth
                self._access_token = None
                self._authenticate()
                if not self._access_token:
                    return None, True
                resp = self._http.get(url, headers=self._headers(), params=params)
            if resp.status_code == 404:
                return None, False
            resp.raise_for_status()
            return resp.json(), False
        except Exception as exc:
            logger.warning("gaia request failed url=%s: %s", url, exc)
            return None, True

    # ── Public methods ─────────────────────────────────────────────────────────

    def get_node_eae_today(self, node_id: int) -> float:
        """Return total exported energy [Wh] for node today (sum of eaepd1+2+3 across all rows)."""
        rows = self.get_node_measurements(node_id, _col_today(), "eae")
        total = 0.0
        for r in rows:
            for f in ("eaepd1", "eaepd2", "eaepd3"):
                v = r.get(f)
                if v is not None:
                    total += float(v)
        return total

    def get_node_measurements(self, node_id: int, date_str: str, var_name: str) -> list[dict]:
        """Fetch measurement rows for one variable from a node for a given date.

        var_name: one of  v, c, ap, rp, pf, eae, iae, ere, ire
        Returns list of dicts, each containing a 'time' key and per-phase fields.
        """
        url = f"{self._base}/api/node/{node_id}/measurements/"
        params = {
            "init_date": f"{date_str}T00:00:00-05:00",
            "end_date":  f"{date_str}T23:59:59-05:00",
            "vars":      var_name,
        }
        data = self._get(url, params=params)
        return data if isinstance(data, list) else []

    def get_border_report_status(self, border_id: int, date_str: str) -> dict | None:
        """Fetch the ASIC report status for a border on a specific date.

        Uses /api/cgm/v1/report_/historic/{border_id}/ (paginated, most recent
        first) and returns the entry matching report_date == date_str, or None
        if that date has no report yet.

        Returns a dict with 'status' ('OK'/'WARNING'/'ERROR'), and the hourly
        curves 'reported_data_main' / 'reported_data_backup' (24 floats each).
        """
        reporte, _ = self.get_border_report_status_con_estado(border_id, date_str)
        return reporte

    def get_border_report_status_con_estado(self, border_id: int, date_str: str) -> tuple[dict | None, bool]:
        """Igual que get_border_report_status(), pero retorna (reporte, fallo)
        -- 'fallo' es local a ESTA llamada (usa _get_con_estado(), no
        self.ultima_llamada_fallo, compartida entre hilos e insegura cuando
        varias llamadas corren en paralelo sobre el mismo GaiaClient -- ver
        reporte_cgm.py: ThreadPoolExecutor(max_workers=12) reusando un solo
        cliente). Pensada para distinguir "Quoia dice que no hay reporte"
        (fallo=False) de "no se pudo preguntar" (fallo=True) sin la carrera
        de la bandera compartida."""
        encontrados, fallo = self.get_border_reports_status_con_estado(border_id, {date_str})
        return encontrados.get(date_str), fallo

    def get_border_reports_status_con_estado(
        self, border_id: int, date_strs: set[str],
    ) -> tuple[dict[str, dict], bool]:
        """Igual que get_border_report_status_con_estado(), pero para VARIAS
        fechas del mismo border en una sola pasada paginada -- evita una
        llamada HTTP por cada (frontera, día) cuando se piden varios días de
        la misma frontera (ej. reporte mensual de Cliente, hasta 31 llamadas
        por frontera antes -- todas leyendo básicamente las mismas páginas,
        ya que /report_/historic/ trae 100 reportes por página, "más
        reciente primero" -- auditoría CGM 2026-08-26, finding #4).

        Retorna ({fecha: reporte} solo las encontradas, fallo) -- una fecha
        ausente del dict significa "sin reporte esa fecha", no error; fallo
        solo es True si una llamada de red real falló antes de terminar de
        recorrer las páginas necesarias."""
        url = f"{self._base}/api/cgm/v1/report_/historic/{border_id}/"
        params: dict | None = {"page_size": 100}
        pendientes = set(date_strs)
        encontrados: dict[str, dict] = {}
        for _ in range(30):
            if not pendientes:
                break
            data, fallo = self._get_con_estado(url, params=params)
            if not isinstance(data, dict):
                return encontrados, fallo
            for reporte in data.get("results", []):
                rd = reporte.get("report_date")
                if rd in pendientes:
                    encontrados[rd] = reporte
                    pendientes.discard(rd)
            nxt = data.get("next")
            if not nxt:
                break
            url, params = nxt, None
        return encontrados, False

    def get_all_borders(self) -> list[dict]:
        """Fetch all borders registered in Quoia (paginated). Returns flat list of project dicts.

        Each dict has: name, frt_generation, frt_consumption
        where frt_* = {frt_code, status, last_report_date} or None.
        """
        results = []
        url = f"{self._base}/api/cgm/v1/border/"
        while url:
            data = self._get(url)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                results.extend(data.get("results", []))
                url = data.get("next")
            else:
                break
        return results

    def post_report(self, border_id: int, main_readings: list[float], backup_readings: list[float]) -> bool:
        """Envía el reporte de 24 horas (CGM) de una frontera a Quoia/ASIC.

        POST /api/cgm/v1/report_/{border_id}/ -- mismo endpoint que
        get_border_report_status()/report_historic lee, ahora escribiendo.
        main_readings/backup_readings: listas de 24 floats (kWh por hora).

        Envía datos reales al reporte regulatorio ASIC -- verificado en
        producción desde 2026-08-21 (91/91 envíos exitosos, 0 fallos)."""
        self._ensure_token()
        if not self._access_token:
            return False
        url = f"{self._base}/api/cgm/v1/report_/{border_id}/"
        payload = {"main_readings": main_readings, "backup_readings": backup_readings}
        try:
            resp = self._http.post(url, headers=self._headers(), json=payload)
            if resp.status_code == 401:
                self._access_token = None
                self._authenticate()
                if not self._access_token:
                    return False
                resp = self._http.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("gaia post_report failed border_id=%s: %s", border_id, exc)
            return False

    def get_all_nodes(self) -> list[dict]:
        """Fetch all monitoring nodes from /api/node/retailer/ (paginated).

        Each dict has: id, name, category, meter {id, serial}, eae, iae, status.
        Used to build the meter_id → node_id mapping for dynamic border resolution.
        """
        results = []
        url = f"{self._base}/api/node/retailer/"
        while url:
            data = self._get(url)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                results.extend(data.get("results", []))
                url = data.get("next")
            else:
                break
        return results

    def get_node_electrical_snapshot(self, node_id: int) -> dict | None:
        """Comprehensive electrical snapshot for a node (today).

        Fetches all 8 variable families in parallel and returns:
          - Instantaneous: voltage per phase (vp1/2/3), current (cp1/2/3),
            active power (ap1/2/3 + ap_total), reactive power (rp1/2/3 + rp_total),
            power factor (pf1/2/3 + pf_avg)
          - Cumulative today: energy exported (eae_wh), imported (iae_wh),
            reactive exported (ere_wh), all with per-phase breakdown
          - last_time: ISO timestamp of the most recent datapoint
        Returns None if no data is available.
        """
        date_str = _col_today()
        VARS = ["v", "c", "ap", "rp", "pf", "eae", "iae", "ere"]

        results: dict[str, list] = {}
        # Each variable requires its own request (API restriction)
        with ThreadPoolExecutor(max_workers=len(VARS)) as ex:
            futs = {v: ex.submit(self.get_node_measurements, node_id, date_str, v)
                    for v in VARS}
            for var, fut in futs.items():
                try:
                    results[var] = fut.result() or []
                except Exception:
                    results[var] = []

        def _last(lst: list) -> dict:
            return lst[-1] if lst else {}

        def _sum(*fields: str, data: list) -> float | None:
            total: float | None = None
            for row in data:
                for f in fields:
                    v = row.get(f)
                    if v is not None:
                        total = (total or 0.0) + float(v)
            return total

        lv  = _last(results["v"])
        lc  = _last(results["c"])
        lap = _last(results["ap"])
        lrp = _last(results["rp"])
        lpf = _last(results["pf"])
        eae = results["eae"]
        iae = results["iae"]
        ere = results["ere"]

        # Retorna None solo si no hay absolutamente ningún dato útil
        if not lv and not lc and not lap and not eae and not iae:
            return None

        # Most recent timestamp across all vars
        all_times = [r[-1].get("time") for r in results.values() if r]
        last_time = max((t for t in all_times if t), default=None)

        # ── Helpers ────────────────────────────────────────────────────────────
        def _phase_sum(row: dict, *fields: str) -> float | None:
            """Sum several phase fields from a single row; None if all absent."""
            vals = [float(row[f]) for f in fields if row.get(f) is not None]
            return sum(vals) if vals else None

        def _phase_avg(row: dict, *fields: str) -> float | None:
            vals = [float(row[f]) for f in fields if row.get(f) is not None]
            return sum(vals) / len(vals) if vals else None

        def _running_sum_series(data: list, *fields: str) -> list:
            """Cumulative sum of one or more fields across all rows → kWh timeline."""
            total = 0.0
            out = []
            for row in data:
                t = row.get("time")
                v = sum(float(row[f]) for f in fields if row.get(f) is not None)
                if t and v:
                    total += v
                    out.append({"time": t, "kwh": round(total / 1000, 3)})
            return out

        # ── Instantaneous values (last measurement row) ────────────────────────
        # Active power: API returns app1/app2/app3 (not ap1/ap2/ap3)
        ap1 = lap.get("app1")
        ap2 = lap.get("app2")
        ap3 = lap.get("app3")
        ap_total = _phase_sum(lap, "app1", "app2", "app3")

        # Reactive power: rpp1/rpp2/rpp3
        rp1 = lrp.get("rpp1")
        rp2 = lrp.get("rpp2")
        rp3 = lrp.get("rpp3")
        rp_total = _phase_sum(lrp, "rpp1", "rpp2", "rpp3")

        # Power factor: pfp1/pfp2/pfp3
        pf1 = lpf.get("pfp1")
        pf2 = lpf.get("pfp2")
        pf3 = lpf.get("pfp3")
        pf_avg = _phase_avg(lpf, "pfp1", "pfp2", "pfp3")

        # ── Detect AP unit: if any |app| value > 5000, values are in W ──────────
        _max_ap = max(
            (abs(float(r.get(f) or 0))
             for r in results["ap"]
             for f in ("app1", "app2", "app3")
             if r.get(f) is not None),
            default=0
        )
        _ap_divisor = 1000.0 if _max_ap > 5000 else 1.0

        # ── Time series for power chart → always kW ───────────────────────────
        # abs(): algunos medidores reportan AP en negativo todo el día (polaridad
        # del CT invertida en sitio) mientras eae sigue acumulando bien -- ver
        # caso MGS 0032 El Paso Norte, 2026-07-02. Esta función solo se usa para
        # monitorear generación (nunca consumo puro), así que una potencia
        # negativa nunca es un dato real a preservar, siempre es el defecto del
        # medidor. Se deja un warning para poder rastrear qué medidores lo
        # tienen y mandarlos a revisar físicamente -- no se corrige solo en
        # silencio para siempre.
        _ap_negative_count = 0
        ap_series = []
        for r in results["ap"]:
            if not (any(r.get(k) is not None for k in ("app1", "app2", "app3")) and r.get("time")):
                continue
            raw_kw = sum(float(r.get(k) or 0) for k in ("app1", "app2", "app3")) / _ap_divisor
            if raw_kw < 0:
                _ap_negative_count += 1
            ap_series.append({"time": r["time"], "kw": round(abs(raw_kw), 3)})

        if _ap_negative_count:
            logger.warning(
                "Medidor %s reportó AP negativo en %d de %d lecturas hoy -- "
                "probable polaridad de CT invertida en sitio; se corrigió el signo para mostrar.",
                node_id, _ap_negative_count, len(ap_series),
            )

        # Potencia derivada de los deltas de eae -- se calcula siempre (no solo
        # cuando AP está vacío del todo) para poder rellenar los huecos de
        # tiempo donde el medidor dejó de reportar potencia instantánea (AP)
        # pero siguió acumulando energía exportada (eae). Antes el fallback
        # solo se activaba si AP no tenía NINGÚN dato en todo el día; si el
        # medidor reportaba AP solo hasta cierta hora (ej. se cortó a media
        # tarde) la curva quedaba truncada ahí aunque el total de energía sí
        # incluyera esas horas -- ver caso Perijá, 2026-07-02.
        def _parse_t(s: str):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        eae_derived_series = []
        _eae_factor = 1000.0 if node_id in _EAE_WH_NODES else 1.0  # Wh→kWh si aplica
        _prev_t: str | None = None
        for r in eae:
            t = r.get("time")
            if not t:
                continue
            delta_kwh = sum(float(r.get(k) or 0) for k in ("eaepd1", "eaepd2", "eaepd3")) / _eae_factor
            if delta_kwh <= 0 or _prev_t is None:
                _prev_t = t
                continue
            try:
                dt_h = (_parse_t(t) - _parse_t(_prev_t)).total_seconds() / 3600.0
                # Piso mínimo de 6 min: si dos lecturas de eae caen casi pegadas
                # (a veces pasa, un registro extra a un segundo del anterior),
                # dividir por un intervalo casi nulo dispara una potencia
                # absurda (ej. 500,000 kW) -- mejor descartar ese punto que
                # inventar un pico sin sentido.
                if dt_h >= 0.1:
                    eae_derived_series.append({"time": t, "kw": round(delta_kwh / dt_h, 3)})
            except Exception:
                pass
            _prev_t = t

        # AP tiene prioridad (es la medición real de potencia instantánea);
        # eae rellena huecos LARGOS de tiempo sin AP -- al inicio, en medio
        # (ej. se cae la conexión y se recupera más tarde) o al final. No basta
        # con rellenar solo la cola: si AP se cae y luego se recupera, el hueco
        # queda en medio del día, no al final. La comparación es por cercanía
        # en el tiempo, no por texto exacto: ap y eae no siempre comparten el
        # mismo segundo exacto de lectura aunque sean del mismo intervalo
        # (ej. 16:00:00 vs 16:00:01), y comparar el string tal cual dejaba
        # pasar "huecos" falsos que no eran huecos reales.
        #
        # El umbral era 10 min, pensado para el caso real que motivó esto
        # (Perijá, 2026-07-02: AP caído por horas completas). Pero con un
        # umbral tan chico, un medidor con huecos de AP cortos y frecuentes
        # (típico del medidor de Respaldo, que reporta más intermitente) queda
        # relleno de puntos derivados de eae por todos lados -- y como eae se
        # reporta en intervalos más largos que AP, cada relleno promedia
        # potencia sobre una ventana de tiempo distinta a la de sus vecinos
        # reales, produciendo un diente de sierra en la curva (caso real: MGS
        # 0007 La Paz Vallenata, 2026-07-14). Subir el umbral a 30 min sigue
        # cubriendo caídas largas tipo Perijá sin intervenir en huecos cortos,
        # que `spanGaps` en el frontend ya conecta con una línea recta.
        _GAP_TOLERANCE_SEC = 1800  # 30 min
        try:
            _ap_dt = sorted(_parse_t(pt["time"]) for pt in ap_series)
        except Exception:
            _ap_dt = []

        def _tiene_ap_cerca(t_str: str) -> bool:
            try:
                t = _parse_t(t_str)
            except Exception:
                return True  # si no se puede parsear, no lo agregamos (más cauto)
            return any(abs((t - apt).total_seconds()) <= _GAP_TOLERANCE_SEC for apt in _ap_dt)

        power_series = sorted(
            ap_series + [pt for pt in eae_derived_series if not _tiene_ap_cerca(pt["time"])],
            key=lambda pt: pt["time"],
        )

        # ── eae unit: nodos en _EAE_WH_NODES retornan Wh; el resto ya es kWh ──
        _eae_raw = _sum("eaepd1", "eaepd2", "eaepd3", data=eae)
        _eae_kwh = (round(_eae_raw / 1000.0, 3)
                    if (_eae_raw and node_id in _EAE_WH_NODES)
                    else _eae_raw)

        return {
            # Voltage per phase [V]  — vp1/vp2/vp3 correct
            "vp1": lv.get("vp1"), "vp2": lv.get("vp2"), "vp3": lv.get("vp3"),
            # Current per phase [A] — cp1/cp2/cp3 correct
            "cp1": lc.get("cp1"), "cp2": lc.get("cp2"), "cp3": lc.get("cp3"),
            # Active power [W]      — API: app1/app2/app3
            "ap1": ap1, "ap2": ap2, "ap3": ap3, "ap_total": ap_total,
            # Reactive power [VAR]  — API: rpp1/rpp2/rpp3
            "rp1": rp1, "rp2": rp2, "rp3": rp3, "rp_total": rp_total,
            # Power factor          — API: pfp1/pfp2/pfp3
            "pf1": pf1, "pf2": pf2, "pf3": pf3, "pf_avg": pf_avg,
            # Cumulative energy today [kWh] — unit-normalized
            "eae_wh":  _eae_kwh,
            "eae1_wh": _sum("eaepd1", data=eae),
            "eae2_wh": _sum("eaepd2", data=eae),
            "eae3_wh": _sum("eaepd3", data=eae),
            # Imported energy [Wh]        — API: iaepd1/iaepd2/iaepd3
            "iae_wh":  _sum("iaepd1", "iaepd2", "iaepd3", data=iae),
            "iae1_wh": _sum("iaepd1", data=iae),
            "iae2_wh": _sum("iaepd2", data=iae),
            "iae3_wh": _sum("iaepd3", data=iae),
            # Reactive exported [VARh]    — API: erepd1/erepd2/erepd3
            "ere_wh":  _sum("erepd1", "erepd2", "erepd3", data=ere),
            "ere1_wh": _sum("erepd1", data=ere),
            "ere2_wh": _sum("erepd2", data=ere),
            "ere3_wh": _sum("erepd3", data=ere),
            "last_time": last_time,
            # Time series for frontend charts
            "time_series": {
                "power":      power_series,
                "energy_exp": _running_sum_series(eae, "eaepd1", "eaepd2", "eaepd3"),
                "energy_imp": _running_sum_series(iae, "iaepd1", "iaepd2", "iaepd3"),
            },
        }
