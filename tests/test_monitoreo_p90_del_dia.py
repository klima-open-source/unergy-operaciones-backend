"""`/generacion-solar/monitoring` trae el P90 del día de cada planta.

Lo trae para que la vista de Generación Solar deje de pedir el listado COMPLETO
de proyectos. Ese listado (~188 filas con las cinco relaciones anidadas del
serializer de `/proyectos`: inversionistas, info técnica, inversores, contactos
de área y contratos PPA) se traía entero para leer un array de 12 números de las
~47 plantas de esta pantalla. Era la petición más pesada de la vista y existía
solo para eso.

El cálculo ya lo hacía el frontend; lo único que cambia es dónde. Acá no cuesta
nada: el proyecto ya está cargado en el mismo bucle que arma cada fila.

`None` y no `0` cuando no hay curva: un cero se leería como "la meta del día es
cero" y el porcentaje de cumplimiento saldría absurdo.
"""
from calendar import monthrange

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _proyecto(curva):
    """Un proyecto con la curva P90 dada, sin tocar la base."""
    from types import SimpleNamespace

    return SimpleNamespace(p90_mensual_kwh=curva)


def _p90(curva):
    from apps.energia.services.solarview_monitoreo import _p90_del_dia

    return _p90_del_dia(_proyecto(curva))


def _mes_actual():
    from apps.plataforma.services.fechas import hoy_col

    return hoy_col()


def test_reparte_el_p90_del_mes_entre_sus_dias():
    hoy = _mes_actual()
    dias = monthrange(hoy.year, hoy.month)[1]
    curva = [0] * 12
    curva[hoy.month - 1] = 3000

    assert _p90(curva) == round(3000 / dias, 1)


def test_usa_el_mes_en_curso_y_no_otro():
    """Los 12 valores son distintos entre sí: si tomara el mes equivocado, la
    meta saldría de otra estación del año."""
    hoy = _mes_actual()
    curva = [(i + 1) * 1000 for i in range(12)]
    dias = monthrange(hoy.year, hoy.month)[1]

    assert _p90(curva) == round(hoy.month * 1000 / dias, 1)


def test_sin_curva_devuelve_none():
    """`None` y no 0: con 0 la vista mostraría "meta cero" y un porcentaje
    disparatado."""
    assert _p90(None) is None
    assert _p90([]) is None


def test_un_mes_en_cero_tambien_es_none():
    curva = [0] * 12

    assert _p90(curva) is None


def test_una_curva_corta_no_revienta():
    """Dato mal cargado: mejor sin meta que un 500 en toda la flota."""
    assert _p90([100, 200]) is None


def test_una_curva_con_basura_no_revienta():
    hoy = _mes_actual()
    curva = ["no es un número"] * 12
    curva[hoy.month - 1] = "tampoco"

    assert _p90(curva) is None


def test_la_fila_de_monitoreo_incluye_el_campo():
    """El contrato con el frontend: que la clave exista en cada proyecto.

    Se mira el código que arma la fila, no una llamada real: `monitoreo_flota`
    necesita SolarView y la base, y lo que importa acá es que nadie quite la
    clave sin darse cuenta de que la vista depende de ella.
    """
    import inspect

    from apps.energia.services import solarview_monitoreo

    fuente = inspect.getsource(solarview_monitoreo.monitoreo_flota)

    assert '"p90_diario_kwh"' in fuente
