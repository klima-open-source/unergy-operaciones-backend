"""Operadores de red unidos con los de originabot por su `code`.

Importar un proyecto de TSF tiene que mapear su operador por llave, no por
nombre. `operadores_originabot.vincular()` es la puerta: el que ya tiene el code
se deja, el que coincide por nombre se vincula, el que falta se crea.
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
    from apps.fronteras.models import OperadorRed

    OperadorRed.objects.all().delete()
    yield


def _vincular(codigo, nombre):
    from apps.fronteras.services import operadores_originabot

    return operadores_originabot.vincular(codigo, nombre)


def test_vincula_por_nombre_sin_tildes_ni_mayusculas_ni_sufijo():
    from apps.fronteras.models import OperadorRed

    local = OperadorRed.objects.create(
        nombre_legal="CENTRALES ELECTRICAS DE NARIÑO S.A. E.S.P.", nombre_comercial="CEDENAR",
    )
    operador, estado = _vincular("cedenar", "Cedenar")

    assert (operador.id, estado) == (local.id, "vinculado")
    local.refresh_from_db()
    assert local.codigo_originabot == "cedenar"


def test_segunda_vez_es_el_mismo_por_llave():
    primero, _ = _vincular("afinia", "Afinia")
    segundo, estado = _vincular("AFINIA ", "Otro nombre cualquiera")
    assert (segundo.id, estado) == (primero.id, "existente")


def test_crea_el_que_falta_con_su_code():
    operador, estado = _vincular("celsia", "CELSIA")
    assert estado == "creado"
    assert (operador.codigo_originabot, operador.nombre_comercial, operador.nombre_legal) == (
        "celsia", "CELSIA", "CELSIA",
    )


def test_dos_codes_que_se_normalizan_igual_quedan_en_filas_distintas():
    """originabot tiene `ceo` y `ceo esp`: al normalizar se ven iguales."""
    a, _ = _vincular("ceo esp", "Ceo Esp")
    b, estado = _vincular("ceo", "CEO")
    assert estado == "creado"
    assert a.id != b.id


def test_nombre_legal_tomado_por_otro_code_no_se_pisa():
    from apps.fronteras.services import operadores_originabot

    _vincular("x1", "Mismo Nombre")
    with pytest.raises(operadores_originabot.VinculoImposible):
        _vincular("x2", "Mismo Nombre")


def test_sin_code_o_nombre_no_se_vincula():
    with pytest.raises(ValueError):
        _vincular("", "Afinia")


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def admin():
    from apps.plataforma.models import Usuario
    from apps.plataforma.services import cuentas

    u = Usuario.objects.create(email="admin@unergy.io", nombre="A", rol="admin", activo=True)
    return {"HTTP_AUTHORIZATION": f"Bearer {cuentas.token_de(u, movil=False)}"}


def test_endpoint_vincula_y_devuelve_el_operador(admin):
    from django.test import Client

    c = Client()
    r = c.post("/api/v1/operadores-red/originabot", {"codigo": "essa", "nombre": "ESSA"},
               content_type="application/json", **admin)
    assert r.status_code == 200
    assert r.json()["codigo_originabot"] == "essa"
    assert r.json()["estado"] == "creado"

    r2 = c.post("/api/v1/operadores-red/originabot", {"codigo": "essa", "nombre": "ESSA"},
                content_type="application/json", **admin)
    assert (r2.json()["id"], r2.json()["estado"]) == (r.json()["id"], "existente")


def test_el_code_no_se_edita_por_patch(admin):
    from django.test import Client

    operador, _ = _vincular("epm", "EPM")
    r = Client().patch(f"/api/v1/operadores-red/{operador.id}", {"codigo_originabot": "otro"},
                       content_type="application/json", **admin)
    assert r.status_code in (200, 400)
    operador.refresh_from_db()
    assert operador.codigo_originabot == "epm"
