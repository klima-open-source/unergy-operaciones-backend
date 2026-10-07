"""En un PPA, el lado de Unergy lo escribe `crear_ppa`, no se deja vacío.

Una de las dos partes de un PPA es siempre Unergy: en uno de compra compra, en
uno de venta vende. Hasta el 2026-09-18 nadie escribía ese lado --`firmar()`
ponía el cliente de la oferta y dejaba el otro en blanco--, y mientras ese hueco
existiera no se podía exigir que un contrato nombrara a sus dos partes.

Va en `crear_ppa` y no en `firmar()` a propósito: es la única puerta de creación,
así que lo aplican los dos caminos --la API y el CRM-- en vez de solo uno. Esa
asimetría es justo el problema que `crear_ppa` vino a resolver
(`docs/DIAGNOSTICO_PPA.md` §2).

Lo que fijan estas pruebas: que el lado correcto se llene según `tipo_contrato`
--grabarlo al revés invierte comprador y vendedor y llega hasta la facturación
sin fallar de forma visible-- y que la falta del ajuste AVISE en vez de bloquear
al CRM.
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
def entorno():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


NIT_UNERGY = "901.234.567-8"


@pytest.fixture
def unergy(entorno, settings_unergy):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(
        razon_social_nombre="Unergy S.A.S.", nit_cedula=NIT_UNERGY,
    )


@pytest.fixture
def settings_unergy():
    from django.conf import settings
    from django.test import override_settings

    with override_settings(UNERGY_NIT=NIT_UNERGY):
        yield settings


def _crear(tipo, **datos):
    from apps.ppa.services.escritura import crear_ppa

    return crear_ppa(tipo_contrato=tipo, datos={"nombre_interno": "PPA QA", **datos})


def test_en_un_ppa_de_compra_unergy_es_el_comprador(unergy):
    resultado = _crear("compra")

    assert resultado.contrato.comprador_id == unergy.id
    assert resultado.contrato.vendedor_id is None


def test_en_un_ppa_de_venta_unergy_es_el_vendedor(unergy):
    """El lado importa: grabarlo al revés llega hasta la facturación con las
    partes invertidas y sin fallar de forma visible."""
    resultado = _crear("venta")

    assert resultado.contrato.vendedor_id == unergy.id
    assert resultado.contrato.comprador_id is None


def test_no_pisa_el_lado_que_ya_viene_puesto(unergy):
    from apps.clientes.models import Cliente

    otro = Cliente.objects.create(razon_social_nombre="Comercializadora Tercera")

    resultado = _crear("compra", comprador_id=otro.id)

    assert resultado.contrato.comprador_id == otro.id


def test_copia_el_nombre_y_el_nit_de_unergy(unergy):
    """`sincronizar_partes` corre después: el contrato guarda la razón social."""
    resultado = _crear("compra")
    resultado.contrato.refresh_from_db()

    assert resultado.contrato.comprador_nombre == "Unergy S.A.S."
    assert resultado.contrato.comprador_nit == NIT_UNERGY


def test_sin_el_ajuste_avisa_pero_no_bloquea(entorno):
    """Dejar al CRM sin poder firmar por un ajuste que falta sería peor que el
    hueco que esto viene a tapar."""
    from django.test import override_settings

    with override_settings(UNERGY_NIT=""):
        resultado = _crear("compra")

    assert resultado.contrato.id is not None, "el contrato tiene que crearse igual"
    assert resultado.contrato.comprador_id is None
    assert any("sin comprador" in a for a in resultado.avisos)


def test_con_dos_clientes_con_el_mismo_nit_no_elige_ninguno(entorno, settings_unergy):
    """Un NIT duplicado es un par que hay que fusionar: elegir al azar ataría los
    contratos a la mitad equivocada."""
    from apps.clientes.models import Cliente

    Cliente.objects.create(razon_social_nombre="Unergy S.A.S.", nit_cedula=NIT_UNERGY)
    Cliente.objects.create(razon_social_nombre="Unergy SAS", nit_cedula="9012345678")

    resultado = _crear("compra")

    assert resultado.contrato.comprador_id is None
    assert any("sin comprador" in a for a in resultado.avisos)


def test_sin_configurar_nada_unergy_es_la_esp(entorno):
    """El NIT de UNERGY ENERGÍA DIGITAL S.A.S. E.S.P. viene por defecto en
    `config/settings.py`: el lado de Unergy no depende del secret ENV_FILE. Se
    reconoce como se cargó en la plataforma, con puntos y dígito de verificación."""
    from django.conf import settings

    from apps.clientes.models import Cliente

    assert settings.UNERGY_NIT == "901497656-2"
    esp = Cliente.objects.create(
        razon_social_nombre="UNERGY ENERGIA DIGITAL S.A.S E.S.P", nit_cedula="901.497.656-2",
    )

    resultado = _crear("compra")

    assert resultado.contrato.comprador_id == esp.id
