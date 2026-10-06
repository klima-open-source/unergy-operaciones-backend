"""El IVA del arrendador sale de su cliente, sin mover las facturas de hoy.

El arrendador tenía un `responsable_iva` suelto y `calcular_iva` aplicaba 19%
fijo, mientras los clientes ya tenían tasas configurables por servicio y por
proyecto. Decidido con Sara el 2026-09-18: se deriva del cliente.

Lo que estas pruebas fijan, y que es la parte delicada --esto toca
FACTURACIÓN--: **vincular un cliente no cambia ningún número por sí solo**. El
19% sigue aplicando mientras nadie configure una tasa explícita, así que el
cambio se enciende cliente por cliente y nunca de forma inadvertida.

No se pudo verificar contra la base qué valores usa `cliente_tasa_servicio.
servicio` (sin acceso el 2026-09-18), y por eso el servicio se compara
normalizado en vez de exigir una grafía exacta.
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


def _arrendador(cliente=None, responsable=True):
    from types import SimpleNamespace

    return SimpleNamespace(
        responsable_iva=responsable,
        cliente_id=cliente.id if cliente else None,
    )


def _cliente(nombre="Dueño del lote", **kw):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre, **kw)


def _tasa(cliente, servicio, iva_pct, proyecto=None):
    from apps.clientes.models import ClienteTasaServicio

    return ClienteTasaServicio.objects.create(
        cliente=cliente, servicio=servicio, iva_pct=iva_pct, proyecto=proyecto,
    )


# ── Lo que NO debe cambiar ────────────────────────────────────────────────────

def test_sin_cliente_vinculado_sigue_siendo_el_19_por_ciento(entorno):
    """El caso de HOY: ningún arrendador tiene cliente, y nada se mueve."""
    from apps.arriendos.services import iva

    assert iva.pct_de(_arrendador()) == 19.0


def test_no_responsable_de_iva_no_paga_iva(entorno):
    from apps.arriendos.services import iva

    assert iva.pct_de(_arrendador(responsable=False)) == 0.0


def test_vincular_un_cliente_sin_tasas_no_cambia_nada(entorno):
    """Vincular es un acto de integridad de datos, no una decisión tributaria:
    por sí solo no puede mover una factura."""
    from apps.arriendos.services import iva

    cliente = _cliente()

    assert iva.pct_de(_arrendador(cliente)) == 19.0


def test_un_no_responsable_no_empieza_a_pagar_iva_por_vincularse(entorno):
    """La tasa dice CUÁNTO, no SI: quitarle esa decisión al usuario haría que
    vincular empezara a cobrarle IVA a quien no lo paga."""
    from apps.arriendos.services import iva

    cliente = _cliente()
    _tasa(cliente, "arriendo", 19)

    assert iva.pct_de(_arrendador(cliente, responsable=False)) == 0.0


# ── Lo que sí cambia, cuando alguien lo configura ─────────────────────────────

def test_la_tasa_configurada_del_cliente_manda(entorno):
    from apps.arriendos.services import iva

    cliente = _cliente()
    _tasa(cliente, "arriendo", 5)

    assert iva.pct_de(_arrendador(cliente)) == 5.0


def test_la_grafia_del_servicio_no_importa(entorno):
    """Las facturas de servicio guardan "Representación" con mayúscula y tilde;
    no se sabe qué grafía usa arriendo, así que se compara normalizado."""
    from apps.arriendos.services import iva

    cliente = _cliente()
    _tasa(cliente, "Arriendo", 8)

    assert iva.pct_de(_arrendador(cliente)) == 8.0


def test_la_excepcion_del_proyecto_gana_sobre_la_general(entorno):
    """Misma precedencia que el resto del sistema (impuestos.tasas_efectivas)."""
    from apps.arriendos.services import iva
    from apps.proyectos.models import Proyecto

    planta = Proyecto.objects.create(nombre_comercial="La Reserva")
    cliente = _cliente()
    _tasa(cliente, "arriendo", 19)
    _tasa(cliente, "arriendo", 0, proyecto=planta)

    assert iva.pct_de(_arrendador(cliente), planta.id) == 0.0


def test_la_tasa_general_del_cliente_se_usa_si_no_hay_una_de_arriendo(entorno):
    from apps.arriendos.services import iva

    cliente = _cliente(iva_pct=10)

    assert iva.pct_de(_arrendador(cliente)) == 10.0


# ── El cálculo ────────────────────────────────────────────────────────────────

def test_calcular_iva_recibe_un_porcentaje_no_un_booleano(entorno):
    """La firma cambió: pasar `True` daría 1% en vez de 19%."""
    from apps.arriendos.services.calculadora import calcular_iva

    assert calcular_iva(4_691_747, 19.0) == 891_432   # round(4_691_747 * 0.19)
    assert calcular_iva(4_691_747, 0.0) is None
    assert calcular_iva(None, 19.0) is None
