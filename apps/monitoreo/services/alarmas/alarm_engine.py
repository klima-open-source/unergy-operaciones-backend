"""
Motor de alarmas para minigranjas solares Unergy.

Adapted from mgs_alarms/alarm_engine.py for sync operation inside
the operations backend (Railway).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pytz

from apps.comun.config import settings

tz = pytz.timezone(settings.TIMEZONE)

SOLAR_START_HOUR = 6
SOLAR_END_HOUR = 18

DEBOUNCE_POLLS = 4

_NODE_CATEGORIAS = ("ELECTRICAL_GENERATION", "BORDER")


def project_name(node_name: str) -> str:
    """Limpia el nombre crudo de un nodo Quoia/Gaia a un nombre legible.

    Usado hoy solo por solenium_checker.py (fuzzy match contra la API legacy
    de Solenium, un sistema aparte) -- el agrupamiento por proyecto de este
    motor ya NO pasa por esta función. Auditoría alarmas_monitoreo 2026-08-31:
    agrupar/resolver proyectos por nombre (esta función + is_minigranja(),
    ambas removidas de ese camino) causaba fallas silenciosas de vinculación
    con `proyectos` -- ahora se resuelve por FK real vía
    fronteras.proyecto_id, ver scheduler._resolver_mapa_proyectos."""
    name = node_name
    for prefix in ("Minigranja Solar ", "Minigranja ", "MGS "):
        name = name.replace(prefix, "")
    name = re.sub(r"^\d{4}\s*-\s*", "", name)
    for suffix in (" Principal", " Respaldo", " principal", " respaldo", " Repaldo"):
        name = name.replace(suffix, "")
    name = name.strip()
    if name and name[0].islower():
        name = name[0].upper() + name[1:]
    return name


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class AlarmType(str, Enum):
    PLANTA_CAIDA = "PLANTA_CAIDA"
    SIN_GENERACION = "SIN_GENERACION"
    CORTE_ZONA = "CORTE_ZONA"
    INVERSORES_DEGRADADOS = "INVERSORES_DEGRADADOS"
    RECUPERACION = "RECUPERACION"


@dataclass
class Alarm:
    severity: Severity
    alarm_type: AlarmType
    proyecto_id: int | None  # None solo en CORTE_ZONA (evento derivado de varios proyectos)
    proyecto_nombre: str
    category: str
    details: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz))


_STATUS_PRIORITY = {"OK": 0, "WARNING": 1, "ERROR": 2, "NO_DATA": 3}


def _group_by_project(
    nodes: list[dict],
    node_to_proyecto: dict[int, int],
    proyecto_nombres: dict[int, str],
) -> list[dict]:
    """Agrupa nodos Quoia/Gaia por proyecto_id real, resuelto externamente
    (ver scheduler._resolver_mapa_proyectos) vía fronteras.proyecto_id -- no
    por texto. Un nodo sin proyecto_id resuelto se ignora (antes se adivinaba
    por nombre; ahora un vínculo faltante se reporta explícitamente en el log
    del scheduler en vez de fallar en silencio)."""
    groups: dict[int, list[dict]] = {}
    for node in nodes:
        if node.get("category") not in _NODE_CATEGORIAS:
            continue
        pid = node_to_proyecto.get(node.get("id"))
        if pid is None:
            continue
        groups.setdefault(pid, []).append(node)

    virtual: list[dict] = []
    for pid, members in groups.items():
        best_status = "UNKNOWN"
        best_priority = 999
        max_eae = 0
        for m in members:
            s = m.get("status", "UNKNOWN")
            p = _STATUS_PRIORITY.get(s, 999)
            if p < best_priority:
                best_priority = p
                best_status = s
            eae = m.get("eae") or 0
            if eae > max_eae:
                max_eae = eae
        virtual.append({
            "proyecto_id": pid,
            "name": proyecto_nombres.get(pid, f"Proyecto {pid}"),
            "status": best_status,
            "category": "ELECTRICAL_GENERATION",
            "eae": max_eae,
        })
    return virtual


class AlarmEngine:
    def __init__(self):
        self.previous_states: dict[int, str] = {}
        self.bad_streak: dict[int, int] = {}
        self.active_alarms: dict[int, set[AlarmType]] = {}
        self.daily_stats: dict[str, int] = {
            "critical": 0, "warning": 0, "recoveries": 0,
        }
        self.no_gen_nodes: set[str] = set()

    def reset_daily_stats(self):
        self.daily_stats = {"critical": 0, "warning": 0, "recoveries": 0}
        self.no_gen_nodes.clear()

    def evaluate(
        self,
        nodes: list[dict],
        node_to_proyecto: dict[int, int],
        proyecto_nombres: dict[int, str],
    ) -> list[Alarm]:
        alarms: list[Alarm] = []
        now = datetime.now(tz)
        is_solar = SOLAR_START_HOUR <= now.hour < SOLAR_END_HOUR

        projects = _group_by_project(nodes, node_to_proyecto, proyecto_nombres)

        if not is_solar:
            for proj in projects:
                pid = proj["proyecto_id"]
                self.previous_states[pid] = proj.get("status", "UNKNOWN")
                self.bad_streak.pop(pid, None)
            return alarms

        fell_this_poll: list[dict] = []

        for proj in projects:
            pid = proj["proyecto_id"]
            name = proj["name"]
            status = proj.get("status", "UNKNOWN")
            category = proj.get("category", "")
            prev = self.previous_states.get(pid)
            proj_alarms = self.active_alarms.setdefault(pid, set())
            is_bad = status in ("NO_DATA", "ERROR")
            # Capturado ANTES de que el bloque de abajo descarte PLANTA_CAIDA
            # de proj_alarms en el mismo poll en que se recupera -- si no, el
            # chequeo de recuperación más abajo nunca lo encontraría.
            habia_planta_caida = AlarmType.PLANTA_CAIDA in proj_alarms

            if is_bad:
                self.bad_streak[pid] = self.bad_streak.get(pid, 0) + 1
            else:
                self.bad_streak.pop(pid, None)

            # >= (no ==): si un sondeo se salta y el contador pasa de DEBOUNCE_POLLS
            # sin caer justo en el valor exacto, con == la alarma NUNCA dispararía
            # para una planta realmente caída. El guard `not in proj_alarms` de abajo
            # ya evita disparos duplicados, así que >= es seguro.
            if is_bad and self.bad_streak.get(pid, 0) >= DEBOUNCE_POLLS:
                if AlarmType.PLANTA_CAIDA not in proj_alarms:
                    alarms.append(Alarm(
                        severity=Severity.CRITICAL,
                        alarm_type=AlarmType.PLANTA_CAIDA,
                        proyecto_id=pid, proyecto_nombre=name, category=category,
                        details=f"Proyecto sin datos hace ~30 min (estado: {status})",
                    ))
                    proj_alarms.add(AlarmType.PLANTA_CAIDA)
                    self.daily_stats["critical"] += 1
                    fell_this_poll.append(proj)
            elif not is_bad:
                proj_alarms.discard(AlarmType.PLANTA_CAIDA)

            if status == "OK" and (proj.get("eae") or 0) == 0:
                if AlarmType.SIN_GENERACION not in proj_alarms:
                    alarms.append(Alarm(
                        severity=Severity.WARNING,
                        alarm_type=AlarmType.SIN_GENERACION,
                        proyecto_id=pid, proyecto_nombre=name, category=category,
                        details=f"Medidor conectado pero sin generacion a las {now.strftime('%I:%M %p')}",
                    ))
                    proj_alarms.add(AlarmType.SIN_GENERACION)
                    self.daily_stats["warning"] += 1
                    self.no_gen_nodes.add(name)
            else:
                proj_alarms.discard(AlarmType.SIN_GENERACION)

            # Solo cuenta como recuperación si de verdad hubo una caída
            # confirmada (PLANTA_CAIDA, tras superar el debounce) -- sin este
            # guard, `AlarmType.RECUPERACION not in proj_alarms` era siempre
            # True (RECUPERACION nunca se agrega a proj_alarms), así que
            # CUALQUIER fluctuación NO_DATA/ERROR -> OK/WARNING generaba una
            # alarma de recuperación, incluso cuando el debounce nunca llegó
            # a disparar PLANTA_CAIDA (bug encontrado en auditoría 2026-09-01).
            if prev in ("NO_DATA", "ERROR") and status in ("OK", "WARNING"):
                if habia_planta_caida:
                    alarms.append(Alarm(
                        severity=Severity.INFO,
                        alarm_type=AlarmType.RECUPERACION,
                        proyecto_id=pid, proyecto_nombre=name, category=category,
                        details=f"Nuevamente operativo ({prev} -> {status})",
                    ))
                    proj_alarms.discard(AlarmType.PLANTA_CAIDA)
                    self.daily_stats["recoveries"] += 1

            self.previous_states[pid] = status

        if len(fell_this_poll) >= 2:
            self._detect_zone_outage(alarms, fell_this_poll, projects)

        return alarms

    def _detect_zone_outage(
        self, alarms: list[Alarm], fell_nodes: list[dict], all_projects: list[dict],
    ):
        from apps.monitoreo.services.alarmas.grid_map import group_by_grid

        fell_proj_names = sorted({n["name"] for n in fell_nodes})
        all_proj_names = [p["name"] for p in all_projects]
        ok_proj_names = {
            p["name"] for p in all_projects
            if p.get("status") in ("OK", "WARNING")
        }
        already_grouped: set[str] = set()

        for level in ("circuito", "subestacion", "or"):
            all_groups = group_by_grid(all_proj_names, level)
            fell_groups = group_by_grid(fell_proj_names, level)

            for key, fell_members in fell_groups.items():
                if key == "?" or len(fell_members) < 2:
                    continue
                remaining = [m for m in fell_members if m not in already_grouped]
                if len(remaining) < 2:
                    continue
                neighbors = set(all_groups.get(key, []))
                if neighbors & ok_proj_names:
                    continue

                level_label = {
                    "circuito": "circuito", "subestacion": "subestacion",
                    "or": "operador de red",
                }[level]

                # CORTE_ZONA abarca varios proyectos a la vez -- no tiene un
                # proyecto_id único, se identifica por nombre (ver
                # _resolver_alarmas_superadas en scheduler.py).
                alarms[:] = [
                    a for a in alarms
                    if a.alarm_type != AlarmType.PLANTA_CAIDA or a.proyecto_nombre not in remaining
                ]
                alarms.append(Alarm(
                    severity=Severity.CRITICAL,
                    alarm_type=AlarmType.CORTE_ZONA,
                    proyecto_id=None, proyecto_nombre=", ".join(remaining),
                    category="ELECTRICAL_GENERATION",
                    details=f"Posible corte de {level_label} '{key}': {len(remaining)} proyectos fuera de operacion",
                ))
                already_grouped.update(remaining)

    def get_summary(
        self,
        nodes: list[dict],
        node_to_proyecto: dict[int, int],
        proyecto_nombres: dict[int, str],
    ) -> dict:
        projects = _group_by_project(nodes, node_to_proyecto, proyecto_nombres)
        counts = {"OK": 0, "WARNING": 0, "NO_DATA": 0, "ERROR": 0}
        project_list: list[dict] = []
        for proj in projects:
            s = proj.get("status", "UNKNOWN")
            if s in counts:
                counts[s] += 1
            project_list.append({
                "name": proj["name"], "status": s,
                "kwh": round(proj.get("eae") or 0),
            })
        return {
            "date": datetime.now(tz).strftime("%Y-%m-%d"),
            "time": datetime.now(tz).strftime("%I:%M %p"),
            "status_counts": counts,
            "projects": project_list,
            "total_projects": len(projects),
            "daily_critical": self.daily_stats["critical"],
            "daily_warning": self.daily_stats["warning"],
            "daily_recoveries": self.daily_stats["recoveries"],
        }
