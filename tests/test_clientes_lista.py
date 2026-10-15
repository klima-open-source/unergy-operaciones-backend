"""GET /clientes/lista: el catálogo liviano que usan los selectores de cliente.

El listado paginado tiene tope de 100 filas y los selectores necesitan todos los
clientes; antes los pedían página a página, varias peticiones al Worker de
Cloudflare por cada vista con un selector. `lista` los da en una sola respuesta
con solo lo que un selector muestra.
"""
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


def _lista(datos_usuario):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.clientes.views import ClienteViewSet

    peticion = APIRequestFactory().get("/api/v1/clientes/lista")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ClienteViewSet.as_view({"get": "lista"})(peticion)
    respuesta.render()
    return respuesta


def test_trae_todos_sin_paginar_y_solo_lo_del_selector(datos):
    from apps.clientes.models import Cliente

    Cliente.objects.bulk_create(
        Cliente(razon_social_nombre=f"Cliente {i:03d}", nit_cedula=str(800_000_000 + i))
        for i in range(150)
    )

    respuesta = _lista(datos)

    assert respuesta.status_code == 200
    assert respuesta.data["total"] == 150
    assert len(respuesta.data["items"]) == 150  # por encima del tope de 100 del paginado
    assert set(respuesta.data["items"][0]) == {"id", "razon_social_nombre", "nit_cedula", "tipo_persona"}


def test_ordena_por_nombre_y_omite_los_borrados(datos):
    from django.utils import timezone

    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Zeta", nit_cedula="1")
    Cliente.objects.create(razon_social_nombre="Alfa", nit_cedula="2")
    Cliente.objects.create(razon_social_nombre="Borrado", nit_cedula="3", deleted_at=timezone.now())

    nombres = [c["razon_social_nombre"] for c in _lista(datos).data["items"]]

    assert nombres == ["Alfa", "Zeta"]
