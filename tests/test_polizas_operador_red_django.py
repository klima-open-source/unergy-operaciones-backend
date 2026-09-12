"""El operador de red de la fila de pólizas sale del FK `operador_red`.

Bug real: `GET /api/v1/polizas` devolvía 500 con

    AttributeError: 'Proyecto' object has no attribute 'operador'

El port de SQLAlchemy a Django dejó el nombre viejo de la relación (`operador`);
en Django el FK se llama `operador_red`. Debajo había una segunda falla tapada
por la primera: `build_filas` asignaba el **nombre** (un str) a `operador_red`,
que es el FK y solo acepta una instancia de `OperadorRed`. Por eso el nombre
viaja ahora en `operador_red_nombre` y el serializer lo lee con `source=`.

`tests/test_polizas.py` no lo vio porque prueba el árbol FastAPI apagado.

Sin base de datos: a modelos sin guardar (pero con `pk`) se les puede asignar y
leer el FK en memoria sin consultar nada.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _operador(nombre):
    from apps.fronteras.models import OperadorRed

    return OperadorRed(id=1, nombre_legal=nombre)


def test_toma_el_operador_propio_del_proyecto():
    from api.v1.polizas.queryset import _operador_red_legal
    from apps.proyectos.models import Proyecto

    proyecto = Proyecto(id=1, operador_red=_operador("Afinia S.A. E.S.P."))

    assert _operador_red_legal(proyecto) == "Afinia S.A. E.S.P."


def test_cae_a_la_frontera_viva_si_el_proyecto_no_tiene_propio():
    from api.v1.polizas.queryset import _operador_red_legal
    from apps.fronteras.models import Frontera
    from apps.proyectos.models import Proyecto

    proyecto = Proyecto(id=2)
    proyecto.fronteras_vivas = [
        Frontera(id=1, proyecto_id=2, operador_red=_operador("ESSA S.A. E.S.P."))
    ]

    assert _operador_red_legal(proyecto) == "ESSA S.A. E.S.P."


def test_sin_operador_en_ningun_lado_es_none():
    from api.v1.polizas.queryset import _operador_red_legal
    from apps.proyectos.models import Proyecto

    assert _operador_red_legal(Proyecto(id=3)) is None


def test_el_serializer_publica_el_nombre_en_operador_red():
    """El `source=` del serializer y el atributo que anota `build_filas` cuadran."""
    from api.v1.polizas.serializers import PolizaFilaSerializer
    from apps.proyectos.models import Proyecto

    proyecto = Proyecto(id=4, nombre_comercial="Planta Test")
    proyecto.info = None
    proyecto.poliza = None
    proyecto.operador_red_nombre = "Afinia S.A. E.S.P."

    assert PolizaFilaSerializer(proyecto).data["operador_red"] == "Afinia S.A. E.S.P."
