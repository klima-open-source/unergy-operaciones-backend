"""GET /generacion-solar/monitoring/tarjetas?ids=: varias tarjetas en una petición.

Generación Solar pedía detalle e irradiancia por cada tarjeta visible (diez
tarjetas, veinte peticiones al Worker) cada 5 minutos. Lo que se prueba: que una
tarjeta que falla no tumba a las demás, y que una estación lenta no retiene la
respuesta —la razón por la que la irradiancia iba aparte—.
"""
import time

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def sv(monkeypatch):
    from apps.energia.services import solarview_monitoreo as sv

    def detalle(pid, incluir_30d=True):
        assert incluir_30d is False, "las tarjetas no dibujan la serie de 30 días"
        if pid == 2:
            raise RuntimeError("SolarView caído")
        return {"proyecto": pid}

    def irradiancia(pid):
        if pid == 3:
            time.sleep(1)  # estación lenta
        return {"disponible": True, "proyecto": pid}

    monkeypatch.setattr(sv, "monitoreo_detalle", detalle)
    monkeypatch.setattr(sv, "irradiancia_poa", irradiancia)
    monkeypatch.setattr(sv, "GRACIA_IRRADIANCIA_S", 0.2)
    monkeypatch.setattr(sv.connection, "close", lambda: None)
    return sv


def test_una_tarjeta_que_falla_no_tumba_a_las_demas(sv):
    r = sv.tarjetas([1, 2])

    assert r["1"] == {"detalle": {"proyecto": 1}, "irradiancia": {"disponible": True, "proyecto": 1}}
    assert r["2"]["detalle"] == {}


def test_una_estacion_lenta_no_retiene_la_respuesta(sv):
    inicio = time.monotonic()
    r = sv.tarjetas([1, 3])

    assert time.monotonic() - inicio < 0.8
    assert r["3"] == {"detalle": {"proyecto": 3}, "irradiancia": None}
    assert r["1"]["irradiancia"] == {"disponible": True, "proyecto": 1}


def _pedir(querystring):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.v1.generacion_solar.views import GeneracionSolarViewSet

    peticion = APIRequestFactory().get(f"/api/v1/generacion-solar/monitoring/tarjetas{querystring}")
    force_authenticate(peticion, user=type("U", (), {"is_authenticated": True})())
    # Lo que se prueba es la validación, no los permisos.
    vista = GeneracionSolarViewSet.as_view({"get": "monitoring_tarjetas"}, permission_classes=[])
    respuesta = vista(peticion)
    respuesta.render()
    return respuesta


@pytest.mark.parametrize("ids", ["", "?ids=a,b", "?ids=" + ",".join(str(i) for i in range(13))])
def test_valida_los_ids(sv, ids):
    assert _pedir(ids).status_code == 400
