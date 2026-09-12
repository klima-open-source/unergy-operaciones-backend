"""La simulación manda los doce meses, no solo el del inicio del rango.

`_simulacion` devolvía el P90 del mes en que ARRANCA el período consultado, y
nada más. Para `InformesMensualesPanel` alcanza, porque ahí el rango siempre es
un mes. Para el Histórico de Generación no: ahí se puede pedir enero a
diciembre, y pintar la referencia de enero sobre los doce meses es una
comparación falsa -- el P90 varía fuerte por estación, que es justo la razón por
la que se guarda mes a mes y no como un promedio.

`curva_p90_kwh` son los doce valores, para que quien dibuje un rango que cruza
meses tome el de cada uno. Los campos viejos se conservan: ya están en
producción y tienen un consumidor.
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


def _proyecto(**kw):
    """Un proyecto en memoria: `_simulacion` solo lee atributos."""
    from types import SimpleNamespace

    campos = {"id": 1, "p90_mensual_kwh": None, "p50_mensual_kwh": None,
              "p99_mensual_kwh": None}
    campos.update(kw)
    return SimpleNamespace(**campos)


def _sim(proyecto, desde=date(2026, 3, 1)):
    from api.v1.monitoreo.queryset import _simulacion

    return _simulacion(proyecto, desde)


DOCE = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]


def test_manda_los_doce_meses():
    assert _sim(_proyecto(p90_mensual_kwh=DOCE))["curva_p90_kwh"] == DOCE


def test_los_campos_viejos_siguen_ahi():
    """`InformesMensualesPanel` los lee. Romperlos dejaría ese informe sin meta."""
    sim = _sim(_proyecto(p90_mensual_kwh=DOCE, p50_mensual_kwh=DOCE), date(2026, 3, 1))

    assert sim["p90_monthly"] == 300, "marzo es el tercer valor"
    assert sim["p50_monthly"] == 300
    assert sim["p90_daily"] == round(300 / 31, 1), "marzo tiene 31 días"


def test_el_mes_suelto_y_la_curva_concuerdan():
    """El campo viejo tiene que ser el mes de la curva nueva, no otro número."""
    sim = _sim(_proyecto(p90_mensual_kwh=DOCE), date(2026, 7, 15))

    assert sim["p90_monthly"] == sim["curva_p90_kwh"][6] == 700


def test_una_curva_corta_no_se_manda_a_medias():
    """Meses sin meta mezclados con meses con meta se leen como "ese mes la
    planta no tenía que generar nada". Mejor sin curva."""
    sim = _sim(_proyecto(p90_mensual_kwh=[100, 200, 300]))

    assert sim["curva_p90_kwh"] is None


def test_sin_p90_pero_con_p50_la_curva_va_en_none():
    """La simulación existe (hay P50), pero la línea base del gráfico es P90."""
    sim = _sim(_proyecto(p50_mensual_kwh=DOCE))

    assert sim is not None
    assert sim["curva_p90_kwh"] is None
    assert sim["p50_monthly"] == 300


def test_sin_ninguna_curva_no_hay_simulacion():
    assert _sim(_proyecto()) is None


def test_sin_proyecto_no_hay_simulacion():
    assert _sim(None) is None


def test_una_curva_guardada_como_texto_tambien_sirve():
    """Dato histórico: la columna es JSONB pero hay filas viejas con la cadena."""
    import json

    sim = _sim(_proyecto(p90_mensual_kwh=json.dumps(DOCE)))

    assert sim["curva_p90_kwh"] == DOCE


def test_una_curva_ilegible_no_revienta():
    """Mejor un gráfico sin meta que un 500 en toda la consulta."""
    sim = _sim(_proyecto(p90_mensual_kwh="no es json"))

    assert sim is None or sim["curva_p90_kwh"] is None
