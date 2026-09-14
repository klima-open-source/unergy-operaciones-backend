"""`generacion_de_la_flota` no vuelve a pedir ~90 curvas en cada carga.

El endpoint `/monitoreo/resumen-generacion` alimenta la gráfica de "últimos 7
días" de Fallas -> Monitoreo, y cuesta **una llamada externa por proyecto**: son
~90 curvas pedidas a la API de Unergy, una por planta. No había nada que las
evitara, así que cada vez que alguien abría esa pantalla se pagaban enteras.

Medido en producción el 2026-09-14: **48 segundos para devolver 3 kB**. Que la
respuesta sea diminuta y aun así tarde tanto es la firma de este problema -- el
tiempo no se va en red ni en nuestra base, se va esperando afuera.

Lo que se fija acá:

  · que no se repita el trabajo dentro del TTL;
  · que la clave distinga rangos y conjuntos de plantas distintos -- servir la
    respuesta de otro rango es peor que ser lento;
  · que un fallo NO se cachee. Congelar un error de token 15 minutos convierte
    un problema momentáneo en uno que dura un cuarto de hora;
  · que sin Redis no se rompa nada.
"""
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def api(monkeypatch):
    """La corrida real, sustituida por un contador."""
    from django.test import override_settings

    from apps.energia.services import unergy_api

    unergy_api._cache_flota.clear()
    estado = {"corridas": 0, "respuesta": {"dates": [], "by_project": []}}

    def _falsa(proyectos, desde, hasta):
        estado["corridas"] += 1
        return dict(estado["respuesta"])

    monkeypatch.setattr(unergy_api, "_generacion_de_la_flota", _falsa)

    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        from django.core.cache import cache

        cache.clear()
        yield estado
    unergy_api._cache_flota.clear()


def _planta(pk):
    from types import SimpleNamespace

    return SimpleNamespace(id=pk)


def _pedir(plantas=None, desde=date(2026, 9, 8), hasta=date(2026, 9, 14)):
    from apps.energia.services.unergy_api import generacion_de_la_flota

    return generacion_de_la_flota(plantas or [_planta(1), _planta(2)], desde, hasta)


def test_la_segunda_vez_no_vuelve_a_correr(api):
    _pedir()
    _pedir()

    assert api["corridas"] == 1


def test_lo_guardado_lo_ve_otro_proceso(api):
    """El caché va a Redis, no solo a la memoria del worker: gunicorn levanta
    tres y cada uno pagaría los 48 segundos por su cuenta."""
    from apps.energia.services import unergy_api

    _pedir()
    unergy_api._cache_flota.clear()  # otro worker: no vio nada

    _pedir()

    assert api["corridas"] == 1


def test_otro_rango_de_fechas_es_otra_respuesta(api):
    _pedir(desde=date(2026, 9, 1), hasta=date(2026, 9, 7))
    _pedir(desde=date(2026, 9, 8), hasta=date(2026, 9, 14))

    assert api["corridas"] == 2, "sirvió la respuesta de otro rango"


def test_otro_conjunto_de_plantas_es_otra_respuesta(api):
    """Si entra una planta nueva a operación, la respuesta vieja ya no sirve."""
    _pedir(plantas=[_planta(1), _planta(2)])
    _pedir(plantas=[_planta(1), _planta(2), _planta(3)])

    assert api["corridas"] == 2


def test_el_orden_de_las_plantas_no_cambia_la_clave(api):
    """Las mismas plantas en otro orden son la misma consulta: sin ordenar, la
    clave dependería de cómo venga el queryset y el caché casi nunca acertaría."""
    _pedir(plantas=[_planta(2), _planta(1)])
    _pedir(plantas=[_planta(1), _planta(2)])

    assert api["corridas"] == 1


def test_un_fallo_no_se_cachea(api):
    """Congelar un error de token 15 minutos convierte un problema momentáneo en
    uno que dura un cuarto de hora."""
    api["respuesta"] = {"dates": [], "by_project": [], "error": "token_error"}

    _pedir()
    _pedir()

    assert api["corridas"] == 2


def test_tras_el_fallo_el_exito_si_se_cachea(api):
    """El error no envenena el caché: en cuanto vuelve a responder bien, esa
    respuesta sí se guarda y la siguiente ya no paga los 48 segundos."""
    api["respuesta"] = {"dates": [], "by_project": [], "error": "token_error"}
    _pedir()                                    # corrida 1: falla, no se guarda

    api["respuesta"] = {"dates": [{"fecha": "2026-09-14"}], "by_project": []}
    _pedir()                                    # corrida 2: sale bien, se guarda
    _pedir()                                    # del caché, sin corrida

    assert api["corridas"] == 2


def test_sin_redis_sigue_funcionando(api, monkeypatch):
    """Queda el caché por proceso, que es el comportamiento mínimo aceptable."""
    from django.core.cache import cache

    def _reventar(*a, **kw):
        raise RuntimeError("Redis no responde")

    monkeypatch.setattr(cache, "get", _reventar)
    monkeypatch.setattr(cache, "set", _reventar)

    _pedir()
    _pedir()

    assert api["corridas"] == 1


def test_devuelve_lo_mismo_que_la_corrida_real(api):
    api["respuesta"] = {"dates": [{"fecha": "2026-09-14", "kwh_real": 42}],
                        "by_project": []}

    primera = _pedir()
    segunda = _pedir()

    assert primera == segunda == api["respuesta"]
