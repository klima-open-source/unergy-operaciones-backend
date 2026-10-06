"""La nota de inversores de una alarma sale de SolarView, y solo para las
plantas con alarma.

El checker viejo (`SoleniumChecker`) pedía disponibilidad e inversores de toda
la flota en cada ciclo, contra Solenium y emparejando por nombre. Lo que se fija
acá: las mismas notas de siempre, por `project_id_solarview`, y cero llamadas
cuando no hay nada que anotar.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


class _SolarView:
    def __init__(self, disponibilidad, inversores=None):
        self.disponibilidad = disponibilidad
        self.inversores = inversores or {}
        self.llamadas: list[str] = []

    def get_availability(self):
        self.llamadas.append("availability")
        return self.disponibilidad

    def get_project_inverters(self, sv_id):
        self.llamadas.append(f"inversores {sv_id}")
        return self.inversores.get(sv_id, [])


@pytest.fixture
def notas(monkeypatch):
    from apps.monitoreo.services.alarmas import inversores

    def montar(cliente, ids):
        monkeypatch.setattr(inversores, "_cliente_solarview", lambda: cliente)
        monkeypatch.setattr(inversores, "_ids_solarview", lambda pids: {p: ids[p] for p in pids if p in ids})
        return inversores

    return montar


def test_sin_alarmas_no_llama_a_solarview(notas):
    cliente = _SolarView({})
    modulo = notas(cliente, {})

    assert modulo.observaciones(set()) == {}
    assert cliente.llamadas == []


def test_las_notas_de_siempre(notas):
    cliente = _SolarView(
        {
            10: {"availability": None, "category": "high"},
            11: {"availability": 80, "category": "disconnect"},
            12: {"availability": 0, "category": "critical"},
            13: {"availability": 75, "category": "medium"},
            14: {"availability": 100, "category": "high"},
        },
        {13: [
            {"dev_name": "330KTL-Inversor1", "state": "Grid-connected"},
            {"dev_name": "330KTL-Inversor3", "state": "Shutdown"},
            {"dev_name": "330KTL-Inversor4", "state": "Fault"},
        ]},
    )
    modulo = notas(cliente, {1: 10, 2: 11, 3: 12, 4: 13, 5: 14})

    resultado = modulo.observaciones({1, 2, 3, 4, 5})

    assert resultado == {
        1: "Inv. desconectados",
        2: "Inv. desconectados",
        3: "Sin generacion",
        4: "I3 Off I4 Flt",
    }


def test_solo_pide_inversores_de_las_que_estan_por_debajo_de_100(notas):
    cliente = _SolarView({
        20: {"availability": 60, "category": "medium"},
        21: {"availability": 100, "category": "high"},
        22: {"availability": 0, "category": "critical"},
    })
    modulo = notas(cliente, {1: 20, 2: 21, 3: 22})

    modulo.observaciones({1, 2, 3})

    assert cliente.llamadas == ["availability", "inversores 20"]


def test_una_planta_sin_id_de_solarview_no_se_consulta(notas):
    cliente = _SolarView({30: {"availability": 0, "category": "critical"}})
    modulo = notas(cliente, {})

    assert modulo.observaciones({1}) == {}
    assert cliente.llamadas == []


def test_sin_token_no_hay_notas(notas):
    modulo = notas(None, {1: 10})

    assert modulo.observaciones({1}) == {}


def test_el_ciclo_ya_no_usa_el_checker_de_solenium():
    import inspect

    from apps.monitoreo.services.alarmas import sondeo

    fuente = inspect.getsource(sondeo)

    assert "SoleniumChecker" not in fuente
    assert "SoleniumClient" not in fuente
    assert "observaciones(con_alarma)" in fuente
