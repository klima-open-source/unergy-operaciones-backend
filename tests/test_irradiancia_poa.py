"""La irradiancia POA de la gráfica de Generación Solar.

Viene de la estación meteorológica de SolarView. Lo que se fija acá:

  · solo la POA, no la horizontal (`irradiation`), que no se compara con la
    potencia;
  · los -1 son "la estación no mide esto", no cero: se descartan, y una serie
    toda en -1 cuenta como "no disponible" (así llega Valencia Oriente);
  · sin estación (404 -> None) o sin `project_id_solarview`, tampoco hay;
  · lo "no disponible" se cachea más tiempo, porque no cambia en el día y son
    27 de las 39 plantas.

Ningún test sale a la red: el cliente es de mentira.
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


class _Cliente:
    def __init__(self, respuesta):
        self.respuesta = respuesta
        self.llamadas = []

    def get_weather(self, project_id, date_from, date_to):
        self.llamadas.append((project_id, date_from, date_to))
        return self.respuesta


@pytest.fixture
def sv(monkeypatch):
    from django.test import override_settings

    from apps.energia.services import solarview_monitoreo

    solarview_monitoreo._cache.clear()
    monkeypatch.setattr(
        solarview_monitoreo, "_proyecto_o_404",
        lambda pid: SimpleNamespace(id=pid, project_id_solarview="17"),
    )
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        from django.core.cache import cache

        cache.clear()
        yield solarview_monitoreo
    solarview_monitoreo._cache.clear()


def _con(monkeypatch, sv, respuesta):
    cliente = _Cliente(respuesta)
    monkeypatch.setattr(sv, "_get_cliente", lambda: cliente)
    return cliente


def test_devuelve_solo_la_poa_y_descarta_los_menos_uno(monkeypatch, sv):
    cliente = _con(monkeypatch, sv, {"results": {
        "irradiation": {"2026-10-01 11:00:14": 900.0},
        "irradiation_POA": {
            "2026-10-01 11:01:14": 950.26,
            "2026-10-01 11:00:14": -1.0,
            "2026-10-01 11:02:14": 0.0,
        },
    }})

    datos = sv.irradiancia_poa(5)

    assert datos["disponible"] is True
    assert datos["unidad"] == "W/m²"
    # Ordenados por hora, sin el -1, y el 0 se conserva (es un cero real).
    assert datos["puntos"] == [
        {"time": "2026-10-01 11:01:14", "w_m2": 950.3},
        {"time": "2026-10-01 11:02:14", "w_m2": 0.0},
    ]
    [(sv_id, desde, hasta)] = cliente.llamadas
    assert sv_id == 17
    assert desde.endswith("T00:00:00") and hasta.endswith("T23:59:59")


@pytest.mark.parametrize("respuesta", [
    None,  # sin estación: 404
    {"results": {"irradiation": {"t": 900.0}}},  # sin la variable POA
    {"results": {"irradiation_POA": {"t1": -1.0, "t2": -1.0}}},  # sensor ausente
    {"results": {"irradiation_POA": {"t1": 0.0, "t2": 0.0}}},  # todo en cero
])
def test_sin_poa_real_no_esta_disponible(monkeypatch, sv, respuesta):
    _con(monkeypatch, sv, respuesta)

    assert sv.irradiancia_poa(5) == {"disponible": False, "unidad": "W/m²", "puntos": []}


def test_sin_id_de_solarview_no_consulta(monkeypatch, sv):
    monkeypatch.setattr(sv, "_proyecto_o_404",
                        lambda pid: SimpleNamespace(id=pid, project_id_solarview=None))
    monkeypatch.setattr(sv, "_get_cliente", lambda: pytest.fail("no debía consultar"))

    assert sv.irradiancia_poa(5)["disponible"] is False


def test_lo_no_disponible_se_cachea_mas_tiempo(monkeypatch, sv):
    _con(monkeypatch, sv, None)
    ttls = []
    original = sv._cache_set
    monkeypatch.setattr(sv, "_cache_set", lambda k, ttl, d: ttls.append(ttl) or original(k, ttl, d))

    sv.irradiancia_poa(5)

    assert ttls == [sv.CACHE_TTL_SIN_IRRADIANCIA]
    assert sv.CACHE_TTL_SIN_IRRADIANCIA > sv.CACHE_TTL_IRRADIANCIA


def test_la_segunda_consulta_sale_del_cache(monkeypatch, sv):
    cliente = _con(monkeypatch, sv, {"results": {"irradiation_POA": {"t": 800.0}}})

    sv.irradiancia_poa(5)
    sv.irradiancia_poa(5)

    assert len(cliente.llamadas) == 1
