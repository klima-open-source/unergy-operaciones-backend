"""`api/v1/cumplimiento/parametros.py` — los `Query(...)` de FastAPI, traducidos.

**El modulo no tenia ni un test** hasta el 2026-09-08, y lo usan 37 llamadas en 5
recursos. Es el unico lugar donde se valida el query string: DRF no lo hace.

Lo que se vigila es que un valor invalido **falle en vez de degradar callado**.
Ese es todo el punto del modulo: sin el, un `year=1999` devolvia una lista vacia
con 200 --que parece un mes sin datos-- y un `?solo_activas=activas` devolvia el
listado completo sin filtrar, tambien con 200.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _pedir(consulta: str):
    """Una `Request` de DRF con ese query string. No toca la base."""
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    return Request(APIRequestFactory().get(f"/?{consulta}" if consulta else "/"))


# ── bandera ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "  True  ", "yes", "on", "t", "y"])
def test_bandera_verdadera(valor):
    from api.v1.cumplimiento import parametros as par

    assert par.bandera(_pedir(f"f={valor}"), "f") is True


@pytest.mark.parametrize("valor", ["0", "false", "FALSE", " false ", "no", "off", "f", "n"])
def test_bandera_falsa(valor):
    from api.v1.cumplimiento import parametros as par

    assert par.bandera(_pedir(f"f={valor}"), "f") is False


@pytest.mark.parametrize("valor", ["activas", "si", "sí", "2", "-1", "null", "undefined", "verdadero"])
def test_una_bandera_que_no_es_booleana_da_422(valor):
    """El bug: caia a `False` callado y el filtro no se aplicaba, con 200."""
    from api.exceptions import NoProcesable
    from api.v1.cumplimiento import parametros as par

    with pytest.raises(NoProcesable):
        par.bandera(_pedir(f"f={valor}"), "f")


def test_bandera_ausente_o_vacia_usa_el_defecto():
    """`?dry_run=` daba `False` y el backfill escribia en firme. Ahora respeta
    su default de `True`, que existe justamente para no escribir sin que se lo
    pidan. `ufo` --el serializador del frontend-- manda `null` asi, vacio."""
    from api.v1.cumplimiento import parametros as par

    assert par.bandera(_pedir(""), "f") is False
    assert par.bandera(_pedir(""), "f", defecto=True) is True
    assert par.bandera(_pedir("f="), "f") is False
    assert par.bandera(_pedir("f="), "f", defecto=True) is True


# ── entero ───────────────────────────────────────────────────────────────────

def test_entero_valida_rango_y_tipo():
    from api.exceptions import NoProcesable
    from api.v1.cumplimiento import parametros as par

    assert par.entero(_pedir("n=7"), "n") == 7
    assert par.entero(_pedir(""), "n", defecto=3) == 3
    assert par.entero(_pedir("n="), "n", defecto=3) == 3

    with pytest.raises(NoProcesable):
        par.entero(_pedir("n=abc"), "n")
    with pytest.raises(NoProcesable):
        par.entero(_pedir("n=0"), "n", minimo=1)
    with pytest.raises(NoProcesable):
        par.entero(_pedir("n=99999"), "n", maximo=5000)
    with pytest.raises(NoProcesable):
        par.entero(_pedir(""), "n", requerido=True)

    # Los limites son inclusivos, como el `ge=`/`le=` de FastAPI.
    assert par.entero(_pedir("n=1"), "n", minimo=1) == 1
    assert par.entero(_pedir("n=5000"), "n", maximo=5000) == 5000


# ── fecha ────────────────────────────────────────────────────────────────────

def test_fecha_valida_el_formato():
    from datetime import date

    from api.exceptions import NoProcesable
    from api.v1.cumplimiento import parametros as par

    assert par.fecha(_pedir("d=2026-09-01"), "d") == date(2026, 9, 1)
    assert par.fecha(_pedir(""), "d") is None

    # Sin esto la cadena cruda llegaba al ORM y salia un 500, no un 422.
    for malo in ("basura", "2026-13-45", "01/09/2026", "2026-9"):
        with pytest.raises(NoProcesable):
            par.fecha(_pedir(f"d={malo}"), "d")


def test_anio_y_mes_acotan_su_rango():
    from api.exceptions import NoProcesable
    from api.v1.cumplimiento import parametros as par

    assert par.anio(_pedir("year=2026")) == 2026
    assert par.mes(_pedir("month=9")) == 9

    with pytest.raises(NoProcesable):
        par.anio(_pedir("year=1999"))
    with pytest.raises(NoProcesable):
        par.mes(_pedir("month=13"))
