"""`resumen_dia()`: una llamada por planta, pidiendo SOLO el día de hoy.

Antes pedía ayer y hoy a `/measurements/generation/` y además
`/config/project-detail/` para un "top por medidor". Medido el 2026-09-30: con
un solo día SolarView devuelve los mismos kWh en un tercio del tiempo, y el
ranking de medidor se quitó (dato poco confiable, 11 de 39 plantas en 0). De 78
llamadas y ~25 s pasó a 39 llamadas y ~6 s.
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
    def __init__(self, por_planta: dict[int, dict]):
        self.por_planta = por_planta
        self.generacion: list[tuple[int, str, str]] = []
        self.detalles: list[int] = []

    def get_generation(self, project_id, start_date, end_date):
        self.generacion.append((project_id, start_date, end_date))
        return {"generation_kwh": self.por_planta.get(project_id, {})}

    def get_project_detail(self, project_id):
        self.detalles.append(project_id)
        return None


@pytest.fixture
def sv(monkeypatch):
    from apps.energia.services import solarview_monitoreo

    monkeypatch.setattr(solarview_monitoreo, "_cache_get", lambda clave: None)
    monkeypatch.setattr(solarview_monitoreo, "_cache_set", lambda clave, ttl, datos: None)
    monkeypatch.setattr(solarview_monitoreo, "close_old_connections", lambda: None)
    return solarview_monitoreo


def _planta(pid, nombre):
    return SimpleNamespace(id=pid, nombre_comercial=nombre, potencia_ac_kw=None)


def test_pide_solo_hoy_y_una_llamada_por_planta(sv, monkeypatch):
    hoy = sv.hoy_col().isoformat()
    cliente = _SolarView({
        1: {f"{hoy} 10:00": 100.0, f"{hoy} 11:00": 150.5},
        2: {f"{hoy} 10:00": 300.0},
        3: {f"{hoy} 10:00": None},
    })
    monkeypatch.setattr(sv, "_get_cliente", lambda: cliente)
    monkeypatch.setattr(sv, "_proyectos_en_operacion", lambda: [
        (_planta(10, "Uno"), 1), (_planta(20, "Dos"), 2), (_planta(30, "Tres"), 3),
    ])

    datos = sv.resumen_dia()

    assert sorted(cliente.generacion) == [(1, hoy, hoy), (2, hoy, hoy), (3, hoy, hoy)]
    assert cliente.detalles == [], "el ranking de medidor ya no existe"
    assert datos == {
        "fecha": hoy,
        "inversor": {
            "total": 550.5,
            "top": [
                {"proyecto_id": 20, "nombre": "Dos", "kwh": 300.0},
                {"proyecto_id": 10, "nombre": "Uno", "kwh": 250.5},
            ],
        },
    }
