"""Sync HTTP client for Solenium API (inverters / availability)."""
from __future__ import annotations

import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger("mgs.solenium")

RETRY_MAX = 2
TIMEOUT = 30.0
TOKEN_MARGIN_SECONDS = 60


class SoleniumClient:
    def __init__(self):
        # SOLENIUM_AUTH_URL debe ser la BASE (ej: https://auth.sole.tech/api)
        # El cliente agrega /token/ y /token/refresh/ según necesite.
        auth = settings.SOLENIUM_AUTH_URL.rstrip("/")
        # Si la URL ya incluye /token al final, quitar para quedarnos con la base
        if auth.endswith("/token"):
            auth = auth[:-len("/token")]
        self._auth_url = auth
        self._data_url = settings.SOLENIUM_DATA_URL.rstrip("/")
        self._username = settings.SOLENIUM_USER
        self._password = settings.SOLENIUM_PASS
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_time: float = 0
        self._http = httpx.Client(timeout=TIMEOUT, follow_redirects=True)

    @property
    def enabled(self) -> bool:
        return bool(self._username and self._password)

    def _authenticate(self):
        url = f"{self._auth_url}/token/"
        try:
            resp = self._http.post(url, json={
                "username": self._username,
                "password": self._password,
            })
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access"]
            self._refresh_token = data["refresh"]
            self._token_time = time.time()
        except (httpx.HTTPError, KeyError) as exc:
            logger.error("solenium auth failed: %s", exc)
            self._access_token = None
            self._refresh_token = None

    def _try_refresh(self) -> bool:
        if not self._refresh_token:
            return False
        url = f"{self._auth_url}/token/refresh/"
        try:
            resp = self._http.post(url, json={"refresh": self._refresh_token})
            resp.raise_for_status()
            self._access_token = resp.json()["access"]
            self._token_time = time.time()
            return True
        except httpx.HTTPError:
            return False

    def _ensure_token(self):
        if not self._access_token:
            self._authenticate()
            return
        age = time.time() - self._token_time
        if age > (5 * 60 - TOKEN_MARGIN_SECONDS):
            if not self._try_refresh():
                self._authenticate()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: dict | None = None) -> dict | list | None:
        for attempt in range(1, RETRY_MAX + 1):
            self._ensure_token()
            if not self._access_token:
                return None
            try:
                resp = self._http.get(url, headers=self._headers(), params=params)
                if resp.status_code == 401 and attempt == 1:
                    self._access_token = None
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                logger.warning("solenium request failed url=%s attempt=%d: %s", url, attempt, exc)
                if attempt == RETRY_MAX:
                    return None
        return None

    def get_availability(self) -> dict[int, dict]:
        url = f"{self._data_url}/project_availability/"
        data = self._get(url)
        if not data:
            return {}
        result: dict[int, dict] = {}
        for cat in data.get("results", {}).get("categories", []):
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

    def get_inverters(self, project_id: int) -> list[dict]:
        url = f"{self._data_url}/project/{project_id}/inverter/"
        data = self._get(url)
        if not data:
            return []
        return data.get("results", [])

    def get_measurements(self, project_id: int, variable: str = "cp1", time_scale: int = 0) -> dict | None:
        url = f"{self._data_url}/project/{project_id}/measurement/"
        data = self._get(url, params={"variable": variable, "time_scale": time_scale})
        if not data:
            return None
        return data.get("results") or data

    def get_projects(self) -> list[dict]:
        url = f"{self._data_url}/project/"
        all_projects = []
        first = True
        while url:
            # menu=1 en la primera llamada (igual que la interfaz web de Solenium)
            params = {"menu": 1} if first else None
            data = self._get(url, params=params)
            first = False
            if not data:
                break
            results = data.get("results", data) if isinstance(data, dict) else data
            if isinstance(results, list):
                all_projects.extend(results)
            url = data.get("next") if isinstance(data, dict) else None
        return all_projects

    def get_project_detail(self, project_id: int) -> dict | None:
        url = f"{self._data_url}/project_detail/{project_id}/"
        return self._get(url)

    def get_project_summary(self) -> list[dict]:
        url = f"{self._data_url}/project_summary/"
        data = self._get(url)
        if not data:
            return []
        return data.get("items", []) if isinstance(data, dict) else data

    def get_generation(self, project_id: int, start_date: str, end_date: str) -> dict | None:
        url = f"{self._data_url}/project/{project_id}/generation/"
        return self._get(url, params={"start_date": start_date, "end_date": end_date})

    def get_energy(self, project_id: int, granularity: str = "day",
                   date_from: str = "", date_to: str = "") -> dict | None:
        url = f"{self._data_url}/project/{project_id}/energy/"
        params: dict = {"granularity": granularity}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(url, params=params)

    def get_power(self, project_id: int, date_from: str = "", date_to: str = "") -> dict | None:
        url = f"{self._data_url}/project/{project_id}/power/"
        params: dict = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._get(url, params=params or None)

    def get_project_inverters(self, project_id: int) -> list[dict]:
        url = f"{self._data_url}/project/{project_id}/inverter/"
        data = self._get(url)
        if not data:
            return []
        return data.get("results", data) if isinstance(data, dict) else data

    def get_inverter_detail(self, project_id: int, inverter_id: int) -> dict | None:
        """Real-time detail for a single inverter (includes string/MPPT data)."""
        url = f"{self._data_url}/project/{project_id}/inverter/{inverter_id}/"
        return self._get(url)

    def get_measurements_variable(self, project_id: int, variable: str,
                                   time_scale: int = 0) -> dict | None:
        """Fetch a specific measurement variable for a project."""
        url = f"{self._data_url}/project/{project_id}/measurement/"
        data = self._get(url, params={"variable": variable, "time_scale": time_scale})
        if not data:
            return None
        return data.get("results") or data

    def get_relay(self, project_id: int) -> dict | None:
        """Estado actual del reconectador (relay) de un proyecto.

        No todos los proyectos tienen reconectador instalado -- devuelve
        None (404) si no existe, no una excepción.
        """
        url = f"{self._data_url}/project/{project_id}/relay/"
        return self._get(url)

    def get_relay_historical(self, project_id: int, start_date: str, end_date: str,
                              variables: str = "kw") -> dict | None:
        """Histórico del reconectador (relay) de un proyecto en un rango de fechas.

        start_date / end_date: "YYYY-MM-DD HH:MM:SS"
        variables: lista separada por comas de variables eléctricas, ej.
                   "i_a,i_b,i_c,u_a,u_b,u_c,u_r,u_s,u_t,f_abc,kva,kw,pf"
        """
        url = f"{self._data_url}/project/{project_id}/relay/historical/"
        return self._get(url, params={
            "start_date": start_date,
            "end_date": end_date,
            "vars": variables,
        })
