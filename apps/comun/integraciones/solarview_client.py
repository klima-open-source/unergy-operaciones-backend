"""Sync HTTP client for SolarView API (reemplazo de Solenium -- ver plan de
migración).

Auth: token estático por header, sin login/refresh (a diferencia de
SoleniumClient, que usa JWT con /token/ + /token/refresh/).

Cobertura: 7 de los 18 endpoints que expone la API.
  · Fase 1 -- lo que usa Reporte de Energía: get_generation, get_power,
    get_relay_historical.
  · Fase 2 (2026-09-03) -- lo que falta para portar app/api/v1/
    generacion_solar.py, hoy 100% Solenium: get_project_detail,
    get_project_inverters, get_inverter_detail, get_energy. Más
    get_availability y get_company_projects, que ya estaban.

Dos avisos sobre la paridad con SoleniumClient, porque no es 1:1:
  · `get_inverter_detail` tiene OTRA firma (solo el id del inversor, sin el
    proyecto) y no devuelve datos de string/MPPT.
  · No hay equivalente de `get_project_summary` (el lote de potencia de toda
    la flota); se compone con get_availability + get_power.

Y una limitación de la API entera, no de este cliente: **los 18 endpoints son
de solo lectura**. No existe ninguno de escritura, así que el ON/OFF del
reconectador -- que en Solenium viejo es un POST a /relay/set-status/ -- no
tiene equivalente y bloquea la migración de app/api/v1/reconectadores.py.
"""
from __future__ import annotations

import logging
import time

import httpx

from apps.comun.config import settings

logger = logging.getLogger("mgs.solarview")

RETRY_MAX = 2
TIMEOUT = 30.0
# Lo que se pide con alguien mirando la pantalla (relays, POA): un intento
# corto. Si no llega, se muestra lo ultimo conocido. Ver `_get_con_estado`.
TIMEOUT_EN_PANTALLA = 6.0
BACKOFF_SECONDS = 2.0

# La última medición del reconectador de una planta. Va por `project_id`, no
# por `recloser` como el histórico (la documentación no lo dice).
RUTA_RECLOSER = "/solarview/config/recloser/"


def _avisar_si_la_forma_no_es_la_esperada(data, total_power: int, project_id: int) -> None:
    """Grita si /power/ devolvio una forma distinta a la que se pidio.

    `total_power` decide la ESTRUCTURA: 1 da {ts: kw} y 0 da {inversor: {ts:
    kw}}. Leer la que no es no lanza ninguna excepcion -- el llamador
    simplemente descarta todo y devuelve una lista vacia o una curva sin
    puntos, que es como el proveedor no tuviera datos.

    Eso paso dos veces en la migracion de Solenium (2026-09-03/05): la curva
    del detalle quedo vacia y /inverters-power devolvio cero inversores. Los
    dos tardaron dias en verse, porque nada fallaba. Un warning aca los habria
    delatado el primer dia.
    """
    if not isinstance(data, dict):
        return
    power = (data.get("results") or {}).get("power") if isinstance(data.get("results"), dict) else None
    if not isinstance(power, dict) or not power:
        return
    primero = next(iter(power.values()))
    anidado = isinstance(primero, dict)
    if anidado is bool(total_power):
        logger.warning(
            "solarview /power/ devolvio la forma contraria a la pedida "
            "(project_id=%s total_power=%s, valores %s). El llamador la va a "
            "descartar en silencio.",
            project_id, total_power, "anidados" if anidado else "planos",
        )


class SolarViewClient:
    def __init__(self):
        self._base_url = settings.SOLARVIEW_BASE_URL.rstrip("/")
        self._token = settings.SOLARVIEW_TOKEN
        self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=True)

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._token}"}

    def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        return self._get_con_estado(url, params)[1]

    def _get_con_estado(
        self, url: str, params: dict | None = None, *,
        timeout: float | None = None, intentos: int = RETRY_MAX,
    ) -> tuple[str, dict | list | None]:
        """Como `_get`, pero dice POR QUÉ no hay datos.

        `_get` devuelve None tanto si SolarView respondió 404 (el recurso no
        existe: la planta no tiene estación o relay) como si la llamada falló
        (timeout, 5xx). Quien cachea necesita distinguirlos: lo primero no
        cambia en el día, lo segundo se debe reintentar pronto. Estados:
        `"ok"`, `"no_existe"` (404) y `"error"` (sin token, timeout o 5xx).

        `timeout` e `intentos` acortan la espera para lo que se pide mientras
        alguien mira la pantalla (reconectadores, POA). Con los valores por
        defecto (30 s x 2 intentos) un endpoint colgado de SolarView retiene un
        proceso web ~60 s, y con pocos procesos la plataforma entera queda en
        fila: el 2026-10-02 SolarView dejo de responder a ratos (40 s sin
        respuesta en /recloser/ y /weather/) y hasta la campana tardaba 28 s.
        """
        for attempt in range(1, intentos + 1):
            if not self._token:
                return "error", None
            try:
                # Sin `timeout` explicito, la llamada es identica a la de siempre.
                extra = {"timeout": timeout} if timeout is not None else {}
                resp = self._http.get(url, headers=self._headers(), params=params, **extra)
                if resp.status_code == 404:
                    return "no_existe", None
                if resp.status_code in (429, 503) and attempt < intentos:
                    # Reintentar de inmediato ante rate limiting no sirve de
                    # nada -- con ~37 fronteras y hasta 2 llamadas cada una en
                    # la corrida diaria, un 429 sin espera probablemente
                    # vuelva a chocar con el mismo límite. Se respeta
                    # Retry-After si la API lo manda, si no una pausa fija.
                    espera = BACKOFF_SECONDS
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            espera = max(espera, float(retry_after))
                        except ValueError:
                            pass
                    time.sleep(espera)
                    continue
                resp.raise_for_status()
                return "ok", resp.json()
            except (httpx.HTTPError, httpx.TimeoutException, ValueError) as exc:
                logger.warning("solarview request failed url=%s attempt=%d: %s", url, attempt, exc)
                if attempt >= intentos:
                    return "error", None
        return "error", None

    def get_availability(self) -> dict[int, dict]:
        """Disponibilidad de toda la flota en una sola llamada, agrupada por
        categoria (`high`/`medium`/`disconnect`) -- GET /solarview/kpis/availability/.

        Mismo shape de salida que SoleniumClient.get_availability()
        ({project_id: {name, availability, category}}) a proposito: permite
        que un llamador (ej. alarmas de desconexion) trate ambas fuentes
        igual sin ramas por proveedor."""
        url = f"{self._base_url}/solarview/kpis/availability/"
        data = self._get(url)
        if not isinstance(data, dict):
            return {}
        result: dict[int, dict] = {}
        for cat in (data.get("results") or {}).get("categories", []):
            cat_id = cat.get("id", "unknown")
            for item in cat.get("items", []):
                pid = item.get("project")
                if pid is not None:
                    result[pid] = {
                        "name": (item.get("name") or "").strip(),
                        "availability": item.get("availability"),
                        "category": cat_id,
                    }
        return result

    def get_company_projects(self) -> list[dict]:
        """Proyectos en operación disponibles para este cliente -- cada item
        trae {id, name, lon, lat, plant_code, is_minifarm}. Es el listado que
        se usa para emparejar por nombre y asignar project_id_solarview (ver
        app/services/proyectos_backfill_solarview.py), igual que
        SoleniumClient.get_projects() para project_id_solenium -- los dos
        esquemas de id NO coinciden entre sí, no se puede derivar uno del otro.
        """
        url = f"{self._base_url}/solarview/config/company-projects/"
        data = self._get(url)
        if not isinstance(data, dict):
            return []
        results = data.get("results")
        return results if isinstance(results, list) else []

    def get_project_detail(self, project_id: int) -> dict | None:
        """Info general del proyecto + generación acumulada del día --
        GET /solarview/config/project-detail/{id}/.

        Equivalente a `SoleniumClient.get_project_detail()`. Devuelve la
        envoltura cruda `{message, error, results, success}` y el llamador
        desenvuelve, igual que get_generation/get_power acá.

        Contrato verificado en vivo el 2026-09-03 (project_id=12):
          · `results.generation` = {time, value, unit, complete} -- trae su
            PROPIA unidad, que puede ser kWh o MWh. Hay que leerla, no
            asumirla (mismo patrón que ya normaliza /generacion-hoy).
          · `results.installed_capacity` puede venir como el string
            "Desconocida" en vez de un número -- no hacerle float() a ciegas.
        """
        url = f"{self._base_url}/solarview/config/project-detail/{project_id}/"
        return self._get(url)

    def get_generation(self, project_id: int, start_date: str, end_date: str) -> dict | None:
        url = f"{self._base_url}/solarview/measurements/generation/"
        return self._get(url, params={
            "project_id": project_id,
            "start_date": start_date,
            "end_date": end_date,
        })

    def get_project_inverters(self, project_id: int) -> list[dict]:
        """Inversores activos del proyecto con su última lectura --
        GET /solarview/measurements/inverters-list/?project_id=X.

        Equivalente a `SoleniumClient.get_project_inverters()`, misma firma y
        mismo tipo de retorno (lista, vacía si no hay dato).

        Contrato verificado en vivo el 2026-09-03 (project_id=12, 11
        inversores): cada item trae {id, dev_name, state, power, efficiency,
        temperature, time}. NO trae datos de strings/MPPT -- para eso están
        /measurements/dc/ (por string) y /kpis/availability/detail/.
        """
        url = f"{self._base_url}/solarview/measurements/inverters-list/"
        data = self._get(url, params={"project_id": project_id})
        if not isinstance(data, dict):
            return []
        results = data.get("results")
        return results if isinstance(results, list) else []

    def get_inverter_detail(self, inverter_id: int) -> dict | None:
        """Estado y última lectura de UN inversor --
        GET /solarview/measurements/inverter-detail/?id=X.

        OJO con la firma: `SoleniumClient.get_inverter_detail()` recibe
        (project_id, inverter_id); acá el inversor se pide solo por su id, sin
        el proyecto. No son intercambiables por posición.

        Y la forma tampoco es equivalente: el de Solenium incluye datos de
        string/MPPT, este devuelve lo mismo que un item de
        get_project_inverters() ({id, dev_name, state, power, efficiency,
        temperature, time}) -- verificado en vivo el 2026-09-03 con el
        inversor 722. Quien necesite strings tiene que ir a
        /measurements/dc/ o a /kpis/availability/detail/.
        """
        url = f"{self._base_url}/solarview/measurements/inverter-detail/"
        return self._get(url, params={"id": inverter_id})

    def get_energy(self, project_id: int, granularity: str = "day",
                   date_from: str = "", date_to: str = "") -> dict | None:
        """Energía por granularidad (hour|day|month) --
        GET /solarview/measurements/energy/?project_id=X&granularity=Y.

        Equivalente a `SoleniumClient.get_energy()`, misma firma.

        **`date_from` y `date_to` son obligatorios en la práctica**: sin ellos
        la respuesta trae `points: []` aunque igual eche un rango en
        date_from/date_to (verificado en vivo el 2026-09-03 con tres
        proyectos distintos).

        Este método NO es un pasa-manos: normaliza la unidad a kWh. El
        endpoint devuelve los puntos con la clave literalmente llamada `kwh`
        pero declara `unit: "MWh"` en el mismo `results` -- o sea que el
        nombre de la clave miente por un factor de 1000. Verificado en vivo
        el 2026-09-03 (project_id=11, Baraya): `unit: "MWh"` con puntos como
        {"time": "2026-08-01", "kwh": 7.7266}, que son 7.726,6 kWh reales
        (coherente con su P90 de ~7.291 kWh/día).

        Se absorbe acá a propósito, en vez de dejar que cada llamador lo
        resuelva: esa clase de error ya costó caro dos veces en este
        repositorio -- ver `eae_wh` en gaia_client.py, que contiene kWh y que
        informe_om.py divide entre 1000 creyendo el nombre. Al salir de este
        método `unit` siempre dice "kWh" y los valores están en kWh.
        """
        url = f"{self._base_url}/solarview/measurements/energy/"
        params: dict = {"project_id": project_id, "granularity": granularity}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        data = self._get(url, params=params)
        if not isinstance(data, dict):
            return data
        resultados = data.get("results")
        if isinstance(resultados, dict) and str(resultados.get("unit") or "").strip().lower() == "mwh":
            for punto in resultados.get("points") or []:
                if isinstance(punto, dict) and punto.get("kwh") is not None:
                    try:
                        punto["kwh"] = round(float(punto["kwh"]) * 1000, 3)
                    except (TypeError, ValueError):
                        pass
            resultados["unit"] = "kWh"
        return data

    def get_power(self, project_id: int, date_from: str = "", date_to: str = "",
                  total_power: int = 1) -> dict | None:
        """Potencia activa del proyecto -- GET /solarview/measurements/power/.

        `total_power` cambia la FORMA de la respuesta, no solo su contenido
        (verificado en vivo el 2026-09-03 con project_id=11):

          · 1 (por defecto) -> `results.power` es un {timestamp: kw} plano, ya
            sumado entre todos los inversores. Es lo que quiere una curva del
            proyecto, y evita el loop de suma manual que hacia falta con la API
            vieja de Solenium.
          · 0 -> `results.power` es un {nombre_inversor: {timestamp: kw}}, o
            sea la serie de CADA inversor. Es lo que necesita la vista de
            inversores (movil) y es la forma que devolvia Solenium siempre.

        Pedir el default cuando se querian series por inversor devuelve numeros
        donde el llamador espera diccionarios, y el resultado es una lista
        vacia sin ningun error.
        """
        url = f"{self._base_url}/solarview/measurements/power/"
        params: dict = {"project_id": project_id, "power": "active_power",
                        "total_power": total_power}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        data = self._get(url, params=params)
        _avisar_si_la_forma_no_es_la_esperada(data, total_power, project_id)
        return data

    def get_power_con_estado(self, project_id: int, date_from: str,
                             date_to: str) -> tuple[str, dict | None]:
        """`get_power` (potencia ya sumada) diciendo si fue 404 o error.

        `get_power` devuelve None tanto si la planta no tiene datos como si la
        llamada falló; para decir "sin comunicación" hay que distinguirlos (ver
        `_get_con_estado` y `apps/monitoreo/services/comunicacion.py`).
        """
        url = f"{self._base_url}/solarview/measurements/power/"
        estado, data = self._get_con_estado(url, params={
            "project_id": project_id, "power": "active_power", "total_power": 1,
            "date_from": date_from, "date_to": date_to,
        })
        if estado == "ok":
            _avisar_si_la_forma_no_es_la_esperada(data, 1, project_id)
        return estado, data

    def get_weather_con_estado(
        self, project_id: int, date_from: str, date_to: str,
    ) -> tuple[str, dict | None]:
        """Estación meteorológica -- GET /solarview/measurements/weather/.

        `results` trae una serie {timestamp: valor} por variable
        (`irradiation`, `irradiation_POA`, `temperature`, `temperature_POA`,
        `wind_speed`, `wind_direction`) y sus unidades en `unit`. Una variable
        que la estación no mide llega en -1 en todos los puntos, no ausente
        (verificado el 2026-10-01: Valencia Oriente no tiene POA). Sin
        estación: 404 -> `("no_existe", None)` (ver `_get_con_estado`).
        """
        url = f"{self._base_url}/solarview/measurements/weather/"
        return self._get_con_estado(url, params={"project_id": project_id,
                                                 "date_from": date_from,
                                                 "date_to": date_to},
                                    timeout=TIMEOUT_EN_PANTALLA, intentos=1)

    def get_recloser_con_estado(self, project_id: int) -> tuple[str, dict | None]:
        """Última medición del reconectador -- GET /solarview/config/recloser/.

        `results` trae las mismas claves que traía Solenium (`active`, `time`,
        `i_a`… `pf`). `"no_existe"` (404) es que la planta no tiene
        reconectador; `"error"`, que no se pudo leer. Un intento corto: se
        pide con alguien mirando la pantalla (ver `TIMEOUT_EN_PANTALLA`).

        Solo LEE. El ON/OFF no está en este cliente a propósito: vive en
        `apps/monitoreo/services/reconectadores.py::enviar_comando`, detrás
        de sus candados, para que no haya otro camino que se los salte.
        """
        return self._get_con_estado(f"{self._base_url}{RUTA_RECLOSER}",
                                    params={"project_id": project_id},
                                    timeout=TIMEOUT_EN_PANTALLA, intentos=1)

    def get_relay_historical(self, project_id: int, start_date: str, end_date: str,
                              variables: str = "kw") -> dict | None:
        """Histórico del reconectador de un proyecto en un rango de fechas.

        start_date / end_date: "YYYY-MM-DD HH:MM:SS" (verificado en vivo --
        pese a lo que dice la documentación de SolarView sobre ISO 8601 con
        offset, el endpoint real sigue esperando este formato viejo y
        responde 400 con el otro).
        """
        url = f"{self._base_url}/solarview/config/recloser/historical/"
        return self._get(url, params={
            "recloser": project_id,
            "start_date": start_date,
            "end_date": end_date,
            "vars": variables,
        })
