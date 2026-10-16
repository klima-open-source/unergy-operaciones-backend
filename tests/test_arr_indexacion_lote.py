"""GET /arriendos/indexacion/{id}?arrendadores=todos: la serie del contrato y la
de cada arrendador en una sola respuesta.

La ficha de arriendo pedía la del contrato y después una por arrendador; con
tres arrendadores eran cuatro peticiones. Lo que importa es que cada serie sea
la misma que daría la petición individual.
"""
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)

    from django.conf import settings

    originales = settings.DATABASES
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    django.setup()

    from django.apps import apps as django_apps
    from django.db import connections
    from django.test.utils import setup_test_environment

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture
def datos():
    from django.db import transaction

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _indexacion(datos_usuario, contrato_id, querystring=""):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.arriendos.views import ArriendoViewSet

    peticion = APIRequestFactory().get(f"/api/v1/arriendos/indexacion/{contrato_id}{querystring}")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ArriendoViewSet.as_view({"get": "indexacion"})(peticion, contrato_id=contrato_id)
    respuesta.render()
    return respuesta


@pytest.fixture
def contrato(datos):
    from apps.arriendos.models import ArrArrendador, ArrIpcTasa
    from apps.contratos.models import ContratoServicio

    ArrIpcTasa.objects.create(año=2024, tasa=5.2)
    c = ContratoServicio.objects.create(
        servicio_aplica="arriendo", tarifa_anual=12_000_000, fecha_firma_contrato=date(2023, 3, 1)
    )
    uno = ArrArrendador.objects.create(contrato=c, nombre="Uno", valor_base=6_000_000, activo=True)
    dos = ArrArrendador.objects.create(contrato=c, nombre="Dos", valor_base=6_000_000, activo=False)
    return c, [uno, dos]


def test_trae_la_serie_de_cada_arrendador_igual_que_la_individual(datos, contrato):
    c, arrendadores = contrato

    todo = _indexacion(datos, c.id, "?arrendadores=todos").data

    assert todo["anual"] == _indexacion(datos, c.id).data["anual"]
    assert set(todo["arrendadores"]) == {str(a.id) for a in arrendadores}
    for a in arrendadores:
        individual = _indexacion(datos, c.id, f"?arrendador_id={a.id}").data
        assert todo["arrendadores"][str(a.id)] == individual


def test_sin_el_parametro_no_cambia_la_respuesta(datos, contrato):
    c, _ = contrato
    assert "arrendadores" not in _indexacion(datos, c.id).data
