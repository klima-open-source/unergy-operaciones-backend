"""Clientes unidos con los propietarios (arrendadores) de originabot (TSF).

«Sincronizar con TSF» trae los contratos de arrendamiento con sus arrendadores;
cada uno se mapea a un cliente: por llave, por documento completo, por nombre, o
se crea (con confirmación en la pantalla, que usa `?simular=true` antes).
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
    from apps.clientes.services import arrendadores_originabot

    return arrendadores_originabot


def test_vincula_por_documento_aunque_el_nombre_difiera():
    from apps.clientes.models import Cliente

    local = Cliente.objects.create(razon_social_nombre="Charlene V.", nit_cedula="19.456.753")
    cliente, estado = _svc().vincular(596, "Charlene Valentine", "19456753")
    assert (cliente.id, estado) == (local.id, "vinculado")
    local.refresh_from_db()
    assert local.originabot_landlord_id == 596


def test_documento_enmascarado_no_sirve_y_cae_al_nombre():
    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Otra", nit_cedula="19000053")
    local = Cliente.objects.create(razon_social_nombre="MANDY CLAYTON")
    cliente, estado = _svc().vincular(1997, "Mandy Clayton", "19****53")
    assert (cliente.id, estado) == (local.id, "vinculado")


def test_crea_con_documento_y_tipo_de_persona():
    cliente, estado = _svc().vincular(7, "Finca La Esperanza S.A.S.", "900.123.456-7", "juridica")
    assert estado == "creado"
    assert (cliente.nit_cedula, cliente.tipo_persona, cliente.originabot_landlord_id) == (
        "9001234567", "juridica", 7,
    )


def test_crea_sin_nit_si_otro_cliente_ya_lo_tiene():
    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Ya vinculado", nit_cedula="123", originabot_landlord_id=1)
    cliente, estado = _svc().vincular(2, "Homónimo", "123")
    assert estado == "creado" and cliente.nit_cedula is None


def test_simular_no_escribe_y_la_segunda_vez_es_por_llave():
    from apps.clientes.models import Cliente

    assert _svc().que_pasaria(5, "Nuevo") == (None, "creado")
    assert not Cliente.objects.exists()
    a, _ = _svc().vincular(5, "Nuevo")
    b, estado = _svc().vincular(5, "Renombrado")
    assert (b.id, estado) == (a.id, "existente")


def test_nombre_ambiguo_no_vincula():
    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Juan Pérez")
    Cliente.objects.create(razon_social_nombre="JUAN PEREZ", nit_cedula="1")
    assert _svc().que_pasaria(9, "Juan Perez")[1] == "creado"


def test_pide_id_y_nombre():
    with pytest.raises(ValueError):
        _svc().vincular(0, "x")
