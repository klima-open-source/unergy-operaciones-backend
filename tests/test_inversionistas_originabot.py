"""Clientes unidos con los inversionistas de originabot (TSF) por su id.

«Importar desde TSF» trae los inversionistas de la minigranja; cada uno se mapea
a un cliente de Operaciones: por llave, por nombre, o se crea (con confirmación
en la pantalla, que usa `?simular=true` antes).
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


@pytest.fixture(autouse=True)
def _limpio():
    from apps.clientes.models import Cliente

    Cliente.objects.all().delete()
    yield
    Cliente.objects.all().delete()


def _svc():
    from apps.clientes.services import inversionistas_originabot

    return inversionistas_originabot


def test_vincula_por_nombre_normalizado():
    from apps.clientes.models import Cliente

    local = Cliente.objects.create(razon_social_nombre="SOLENIUM S.A.S.")
    cliente, estado = _svc().vincular(21, "Solenium")
    assert (cliente.id, estado) == (local.id, "vinculado")
    local.refresh_from_db()
    assert local.originabot_investment_id == 21


def test_segunda_vez_es_por_llave():
    a, _ = _svc().vincular(95, "Unergy Interno")
    b, estado = _svc().vincular(95, "Otro nombre")
    assert (b.id, estado) == (a.id, "existente")


def test_crea_el_que_falta_marcando_el_origen():
    cliente, estado = _svc().vincular(7, "Fondo Nuevo")
    assert estado == "creado"
    assert (cliente.razon_social_nombre, cliente.originabot_investment_id, cliente.origen_tipo) == (
        "Fondo Nuevo", 7, "originabot",
    )


def test_dos_clientes_con_el_mismo_nombre_no_se_adivina():
    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Inversiones X")
    Cliente.objects.create(razon_social_nombre="INVERSIONES X S.A.S.")
    _, estado = _svc().que_pasaria(3, "Inversiones X")
    assert estado == "creado"


def test_simular_no_escribe():
    from apps.clientes.models import Cliente

    local = Cliente.objects.create(razon_social_nombre="Solenium")
    _, estado = _svc().que_pasaria(21, "Solenium")
    assert estado == "vinculado"
    local.refresh_from_db()
    assert local.originabot_investment_id is None
    _, estado = _svc().que_pasaria(99, "Nadie")
    assert estado == "creado"
    assert Cliente.objects.count() == 1


def test_endpoint_simula_y_vincula():
    from django.test import Client

    from apps.clientes.models import Cliente
    from apps.plataforma.models import Usuario
    from apps.plataforma.services import cuentas

    u, _ = Usuario.objects.get_or_create(
        email="admin-inv@unergy.io", defaults={"nombre": "A", "rol": "admin", "activo": True},
    )
    h = {"HTTP_AUTHORIZATION": f"Bearer {cuentas.token_de(u, movil=False)}"}
    c = Client()
    r = c.post("/api/v1/clientes/originabot?simular=true", {"investment_id": 5, "nombre": "Fondo Sur"},
               content_type="application/json", **h)
    assert r.json() == {"estado": "creado", "id": None, "razon_social_nombre": "Fondo Sur"}
    assert not Cliente.objects.exists()

    r = c.post("/api/v1/clientes/originabot", {"investment_id": 5, "nombre": "Fondo Sur"},
               content_type="application/json", **h)
    assert r.json()["estado"] == "creado"
    assert Cliente.objects.get(pk=r.json()["id"]).originabot_investment_id == 5
