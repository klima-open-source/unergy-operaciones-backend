"""Los inversores en vivo salen de SolarView, por `project_id_solarview`.

Una sola función alimenta el informe de puesta en marcha y el informe mensual
de O&M. Hasta el 2026-09-29 eran dos copias contra Solenium, que ya no
responde.
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


@pytest.fixture
def modulo(monkeypatch):
    from apps.monitoreo.services import inversores_en_vivo

    monkeypatch.setattr(inversores_en_vivo, "_cache", {})
    return inversores_en_vivo


class _Cliente:
    def __init__(self, crudos):
        self.crudos = crudos
        self.pedidos: list[int] = []

    def get_project_inverters(self, project_id):
        self.pedidos.append(project_id)
        return self.crudos


def _proyecto(sv_id="12"):
    return SimpleNamespace(id=1, project_id_solarview=sv_id)


def test_sin_id_de_solarview_no_se_adivina(modulo, monkeypatch):
    cliente = _Cliente([{"id": 1}])
    monkeypatch.setattr(modulo, "_cliente_solarview", lambda: cliente)

    lista, error = modulo.inversores(_proyecto(sv_id=None))

    assert lista == []
    assert "project_id_solarview" in error
    assert cliente.pedidos == []


def test_traduce_los_campos_de_solarview(modulo, monkeypatch):
    """`inverters-list` trae {id, dev_name, state, power, ...}: la salida es la
    forma que leen las dos pantallas."""
    cliente = _Cliente([
        {"id": 7, "dev_name": "330KTL-Inversor1", "state": "Grid-connected", "power": 212.4},
        {"id": 8, "dev_name": None, "state": "Shutdown", "power": 0},
        {"dev_name": "sin id, se descarta"},
    ])
    monkeypatch.setattr(modulo, "_cliente_solarview", lambda: cliente)

    lista, error = modulo.inversores(_proyecto("12"))

    assert error is None
    assert cliente.pedidos == [12]
    assert lista == [
        {"id": 7, "nombre": "330KTL-Inversor1", "potencia_nominal_kw": 330.0,
         "power_kw": 212.4, "state": "Grid-connected"},
        {"id": 8, "nombre": "Inversor 8", "potencia_nominal_kw": None,
         "power_kw": 0, "state": "Shutdown"},
    ]


def test_la_segunda_consulta_sale_del_cache(modulo, monkeypatch):
    cliente = _Cliente([{"id": 7, "dev_name": "X"}])
    monkeypatch.setattr(modulo, "_cliente_solarview", lambda: cliente)

    modulo.inversores(_proyecto("12"))
    modulo.inversores(_proyecto("12"))

    assert cliente.pedidos == [12]


def test_una_respuesta_vacia_avisa_y_no_se_guarda(modulo, monkeypatch):
    """[] puede ser "no tiene" o "falló": no se cachea, para que el siguiente
    intento vuelva a preguntar."""
    cliente = _Cliente([])
    monkeypatch.setattr(modulo, "_cliente_solarview", lambda: cliente)

    lista, error = modulo.inversores(_proyecto("12"))
    modulo.inversores(_proyecto("12"))

    assert lista == []
    assert error
    assert cliente.pedidos == [12, 12]


def test_sin_token_lo_dice(modulo, monkeypatch):
    monkeypatch.setattr(modulo, "_cliente_solarview", lambda: None)

    lista, error = modulo.inversores(_proyecto("12"))

    assert lista == []
    assert "SOLARVIEW_TOKEN" in error


def test_la_puesta_en_marcha_usa_la_misma_funcion(monkeypatch):
    from apps.monitoreo.services import inversores_en_vivo
    from apps.om.services import vivo

    monkeypatch.setattr(inversores_en_vivo, "inversores", lambda p: ([{"id": 1}], None))

    assert vivo.inversores(_proyecto("12")) == [{"id": 1}]


def test_ya_nadie_llama_a_solenium_para_inversores():
    import inspect

    from apps.monitoreo.services import inversores_en_vivo
    from apps.om.services import vivo

    for modulo in (inversores_en_vivo, vivo):
        fuente = inspect.getsource(modulo)
        assert "SoleniumClient" not in fuente
