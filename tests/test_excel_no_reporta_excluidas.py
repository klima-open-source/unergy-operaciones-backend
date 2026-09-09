"""El Excel del día y /enviar deciden "no reportar" con el MISMO criterio.

Bug real (encontrado 2026-09-09): el criterio estaba escrito dos veces --
`envio._reporte_ya_valido()` y dos comparaciones sueltas dentro de
`excel.generar_excel_dia()` -- y ya habían divergido. La copia del Excel
cubría el CGM pero NO 'excluida', y su consulta trae todas las filas del día
sin filtrar, así que una frontera excluida salía en el Excel con 24 horas de
0,0. Ese Excel es la matriz que se carga en Quoia: reportaba cero en una
frontera que justamente no debe reportar nada, exactamente la curva fabricada
que el chequeo de /enviar existe para evitar (ver su docstring).

Nadie lo notó porque las dos salidas se prueban por separado y ninguna
comparaba su criterio con el de la otra. El arreglo fue mover la función a
`utils.reporte_ya_valido()` -- que ya importaban los dos -- y borrar las
copias.

Por eso este archivo prueba las dos mitades: que la función cubra 'excluida'
en los dos árboles, y que ni el Excel ni el envío vuelvan a tener un criterio
propio.
"""
import re
from pathlib import Path

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

RAIZ = Path(__file__).resolve().parent.parent
REPORTE = RAIZ / "apps" / "energia" / "services" / "reporte"


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _rep(**kw):
    from types import SimpleNamespace

    base = dict(caso="Histórico", medidor_usado="historico")
    base.update(kw)
    return SimpleNamespace(**base)


# ── El criterio cubre 'excluida' en los dos árboles ──────────────────────────

def test_generacion_excluida_no_se_reporta():
    from apps.energia.services.reporte.utils import reporte_ya_valido

    # `curva_final` es None mientras dura la exclusión: sin este chequeo, el
    # Excel escribía 0,0 en las 24 horas (el `or [None] * 24` de excel.py las
    # vuelve None, y el `if valor_gen is None` las convierte en 0,0).
    rep = _rep(medidor_usado="excluida", caso=6, curva_final=None)
    assert reporte_ya_valido(rep, es_generacion=True) is True


def test_consumo_excluida_no_se_reporta():
    from apps.energia.services.reporte.utils import reporte_ya_valido

    rep = _rep(medidor_usado="excluida", caso="Revisar", curva_final=None)
    assert reporte_ya_valido(rep, es_generacion=False) is True


def test_una_fila_normal_si_se_reporta():
    """El contrapeso: sin esto, un criterio que devuelva True siempre pasaría
    los dos tests de arriba y dejaría de enviarse TODO."""
    from apps.energia.services.reporte.utils import reporte_ya_valido

    assert reporte_ya_valido(_rep(caso=3, medidor_usado="inversores"), es_generacion=True) is False
    assert reporte_ya_valido(_rep(caso="Histórico"), es_generacion=False) is False


# ── Guard: nadie vuelve a escribir su propia copia del criterio ──────────────

# Las dos comparaciones que FORMAN el criterio. Solo pueden aparecer donde el
# criterio vive (utils.py). `fuente == "cgm"` de correcciones.py es otra cosa
# -- mira la fuente que llega del front, no lo que quedó guardado -- así que
# el patrón exige el nombre del campo.
COPIA_DEL_CRITERIO = re.compile(r'medidor_usado\s*==\s*"cgm"|caso\)?\s*==\s*"CGM"')


@pytest.mark.parametrize("archivo", ["excel.py", "envio.py"])
def test_las_salidas_no_tienen_criterio_propio(archivo):
    fuente = (REPORTE / archivo).read_text(encoding="utf-8")
    hallazgos = COPIA_DEL_CRITERIO.findall(fuente)
    assert not hallazgos, (
        f"{archivo} volvió a escribir el criterio de 'no reportar' ({hallazgos}). "
        "Tiene que llamar a utils.reporte_ya_valido(): con una copia, el Excel y "
        "/enviar se separan y uno de los dos manda una matriz que el otro salta."
    )


@pytest.mark.parametrize("archivo", ["excel.py", "envio.py"])
def test_las_salidas_llaman_a_la_funcion_compartida(archivo):
    fuente = (REPORTE / archivo).read_text(encoding="utf-8")
    assert "reporte_ya_valido(" in fuente, f"{archivo} no consulta utils.reporte_ya_valido()"
