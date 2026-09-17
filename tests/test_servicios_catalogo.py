"""El catálogo de grupos de servicio y su endpoint.

Lo que queda del bloque de agrupación: el catálogo, que sí tiene consumidor
--`ServiciosUnificadoView.vue` lo pide para saber qué subservicios ofrece cada
pestaña-- y una guarda contra volver a publicar el listado agrupado sin
conectarlo a nada.

Las pruebas de la forma común y de la agrupación se fueron con
`services/unificado.py` y `services/consulta.py`, que no tenían consumidor.
Están en el historial de git.
"""

import os

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

# El arranque va a nivel de módulo porque los imports de abajo cargan modelos
# durante la colección, antes de que corra cualquier fixture.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.services import grupos  # noqa: E402


# ── El catálogo que consume el front ──────────────────────────────────────

def test_el_catalogo_trae_los_tres_grupos_en_orden():
    catalogo = grupos.catalogo()
    assert [g["grupo"] for g in catalogo] == ["ppa", "representacion_cgm", "operacion"]


def test_cada_grupo_expone_sus_subservicios():
    por_grupo = {g["grupo"]: g["subservicios"] for g in grupos.catalogo()}
    assert por_grupo["ppa"] == ["compra", "venta"]
    assert por_grupo["representacion_cgm"] == ["representacion", "cgm"]
    assert por_grupo["operacion"] == ["mantenimiento", "arriendo", "internet"]


def test_el_catalogo_y_el_mapa_no_pueden_divergir():
    """Si se separan, el front filtra por subservicios que no existen."""
    del_catalogo = {g["grupo"]: g["subservicios"] for g in grupos.catalogo()}
    del_mapa = {g: list(subs) for g, subs in grupos.SUBSERVICIOS.items()}
    assert del_catalogo == del_mapa


# ── La ruta ───────────────────────────────────────────────────────────────

def test_la_ruta_del_catalogo_existe():
    from django.urls import resolve

    assert resolve("/api/v1/servicios/catalogo").func is not None


def test_no_se_publica_un_listado_que_nadie_consume():
    """`GET /api/v1/servicios` se quitó antes de desplegarlo: sin consumidor.

    Con él se fueron `services/unificado.py` y `services/consulta.py`, que solo
    lo alimentaban a él. Están en el historial de git si algún día se migra la
    vista; publicarlos de nuevo sin conectarlos es lo que esta prueba impide.
    """
    from django.urls import Resolver404, resolve

    try:
        resolve("/api/v1/servicios")
    except Resolver404:
        return
    raise AssertionError("el listado agrupado volvió a publicarse sin consumidor")
