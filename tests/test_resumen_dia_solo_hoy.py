"""`resumen_dia()`: los inversores se piden SOLO para el día de hoy.

Antes se pedía ayer y hoy a `/measurements/generation/`. Medido el
2026-09-30: con un solo día SolarView devuelve los mismos kWh en un tercio del
tiempo, y `resumen_dia()` pasó de ~25 s a ~8 s con las mismas 78 llamadas (39
de generación y 39 de `/config/project-detail/` para el ranking de medidor, que
se sigue usando en el resumen del móvil).

Más paralelismo no mejora: 16 hilos o las 78 llamadas sueltas tardaron más
(~10-11 s) que los 8 hilos de hoy; SolarView no responde más rápido si se le
pide más a la vez.
"""
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


class _SolarView:
    def __init__(self, generacion: dict[int, dict], medidor: dict[int, float]):
        self.por_planta = generacion
        self.medidor = medidor
        self.generacion: list[tuple[int, str, str]] = []
        self.detalles: list[int] = []

    def get_generation(self, project_id, start_date, end_date):
        self.generacion.append((project_id, start_date, end_date))
        return {"generation_kwh": self.por_planta.get(project_id, {})}

    def get_project_detail(self, project_id):
        self.detalles.append(project_id)
        valor = self.medidor.get(project_id)
        if valor is None:
            return None
        return {"results": {"generation": {"value": valor, "unit": "kWh"}}}


@pytest.fixture
def sv(monkeypatch):
    from apps.energia.services import solarview_monitoreo

    monkeypatch.setattr(solarview_monitoreo, "_cache_get", lambda clave: None)
    monkeypatch.setattr(solarview_monitoreo, "_cache_set", lambda clave, ttl, datos: None)
    monkeypatch.setattr(solarview_monitoreo, "close_old_connections", lambda: None)
    return solarview_monitoreo


def _planta(pid, nombre):
    return SimpleNamespace(id=pid, nombre_comercial=nombre, potencia_ac_kw=None)


def test_inversores_solo_de_hoy_y_el_medidor_se_mantiene(sv, monkeypatch):
    hoy = sv.hoy_col().isoformat()
    cliente = _SolarView(
        generacion={
            1: {f"{hoy} 10:00": 100.0, f"{hoy} 11:00": 150.5},
            2: {f"{hoy} 10:00": 300.0},
        },
        medidor={1: 240.0, 2: 290.0},
    )
    monkeypatch.setattr(sv, "_get_cliente", lambda: cliente)
    monkeypatch.setattr(sv, "_proyectos_en_operacion", lambda: [
        (_planta(10, "Uno"), 1), (_planta(20, "Dos"), 2),
    ])

    datos = sv.resumen_dia()

    assert sorted(cliente.generacion) == [(1, hoy, hoy), (2, hoy, hoy)]
    assert sorted(cliente.detalles) == [1, 2]
    assert datos["inversor"] == {
        "total": 550.5,
        "top": [
            {"proyecto_id": 20, "nombre": "Dos", "kwh": 300.0},
            {"proyecto_id": 10, "nombre": "Uno", "kwh": 250.5},
        ],
    }
    assert datos["medidor"] == {
        "total": 530.0,
        "top": [
            {"proyecto_id": 20, "nombre": "Dos", "kwh": 290.0},
            {"proyecto_id": 10, "nombre": "Uno", "kwh": 240.0},
        ],
    }
