"""Sync Solenium checker — inverter observations per project."""
from __future__ import annotations

import logging
import re
import unicodedata

from apps.monitoreo.services.alarmas.alarm_engine import project_name
from apps.comun.integraciones.solenium_client import SoleniumClient

logger = logging.getLogger("mgs.solenium_checker")


def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in nfkd if not unicodedata.combining(c))
    text = text.lower().strip()
    text = re.sub(r"^minigranja\s*", "", text)
    text = re.sub(r"^solar\s*", "", text)
    text = re.sub(r"^mgs\s*", "", text)
    text = re.sub(r"^\d{4}\s*-\s*", "", text)
    for suffix in ("principal", "respaldo", "repaldo"):
        text = re.sub(rf"\s*{suffix}\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_STATE_SHORT = {
    "Shutdown": "Off", "Fault": "Flt", "Standby": "Sby",
    "Disconnected": "Dsc", "Stop": "Off",
}


def _short_name(raw: str) -> str:
    m = re.search(r'[Ii]nversor\s*(\d+)', raw)
    if m:
        return f"I{m.group(1)}"
    m = re.search(r'COM(\d+)-(\d+)', raw)
    if m:
        return f"C{m.group(1)}{m.group(2)}"
    return re.sub(r'[^A-Za-z0-9]', '', raw)[:4]


def _short_state(state: str) -> str:
    return _STATE_SHORT.get(state, state[:3])


def _format_bad(bad: list[tuple[str, str]]) -> str:
    if not bad:
        return ""
    by_state: dict[str, list[str]] = {}
    for name, st in bad:
        by_state.setdefault(st, []).append(name)
    parts = []
    for st, names in by_state.items():
        if len(names) > 3:
            parts.append(f"{len(names)} {st}")
        else:
            parts.append(f"{'+'.join(names)} {st}")
    return " ".join(parts)


def build_project_map(availability: dict[int, dict]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for pid, info in availability.items():
        raw_name = info.get("name", "")
        if not raw_name:
            continue
        mapping[_normalize(raw_name)] = pid
    return mapping


def _find_solenium_id(node_name: str, project_map: dict[str, int]) -> int | None:
    base = project_name(node_name)
    norm = _normalize(base)
    if norm in project_map:
        return project_map[norm]
    for sol_norm, pid in project_map.items():
        if norm in sol_norm or sol_norm in norm:
            return pid
    return None


class SoleniumChecker:
    def __init__(self, client: SoleniumClient):
        self._client = client

    def get_inverter_observations(self, project_names: list[str]) -> dict[str, str]:
        if not self._client.enabled:
            return {}
        availability = self._client.get_availability()
        if not availability:
            return {}
        project_map = build_project_map(availability)
        observations: dict[str, str] = {}

        for proj_name in project_names:
            sol_id = _find_solenium_id(proj_name, project_map)
            if sol_id is None:
                continue
            info = availability.get(sol_id, {})
            avail = info.get("availability")
            cat = info.get("category", "unknown")

            if avail is None or cat == "disconnect":
                observations[proj_name] = "Inv. desconectados"
                continue
            if avail == 0:
                observations[proj_name] = "Sin generacion"
                continue
            if avail < 100:
                inverters = self._client.get_inverters(sol_id)
                bad: list[tuple[str, str]] = []
                for inv in inverters:
                    state = inv.get("state", "")
                    if state and state != "Grid-connected":
                        raw = inv.get("dev_name", "?")
                        bad.append((_short_name(raw), _short_state(state)))
                if bad:
                    observations[proj_name] = _format_bad(bad)

        logger.info("inverter_observations projects=%d with_obs=%d",
                     len(project_names), len(observations))
        return observations
