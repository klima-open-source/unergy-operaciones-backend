"""Facturación trae sola el IPP del mes si falta (`ipp.asegurar_mes`).

Pedido por Jessica el 2026-10-08: el botón «Guardar IPP en facturación»
funcionaba, pero había que acordarse de pulsarlo. Lo que se prueba:

  * con el mes ya guardado no se llama a la API;
  * si falta, se crean los meses que faltan y **nunca se pisa** uno existente
    (moverlo solo cambiaría un mes quizá ya facturado);
  * si la API no lo tiene o se cae, no revienta y no se reintenta en cada carga.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    """SQLite en memoria aislada (mismo patrón que `test_facturacion_golden_django`)."""
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django
    from django.conf import settings

    originales = settings.DATABASES
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    django.setup()

    from django.apps import apps as django_apps
    from django.db import connections
    from django.test.utils import setup_test_environment, teardown_test_environment

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()
    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture(autouse=True)
def _deshacer_datos():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


class _CacheFalsa:
    def __init__(self):
        self.datos = {}

    def get(self, clave):
        return self.datos.get(clave)

    def set(self, clave, valor, segundos):
        self.datos[clave] = valor


@pytest.fixture
def entorno(monkeypatch):
    """La API y la caché, sustituidas: cuenta las llamadas a la API."""
    import django.core.cache

    from apps.ppa.services import ipp

    estado = {"llamadas": 0, "filas": [], "falla": False}

    def _api():
        estado["llamadas"] += 1
        if estado["falla"]:
            raise RuntimeError("API caída")
        return estado["filas"]

    monkeypatch.setattr(ipp, "_traer_de_la_api", _api)
    monkeypatch.setattr(django.core.cache, "cache", _CacheFalsa())
    return estado


def _valor(anio, mes):
    from apps.ppa.models import IppMensual

    fila = IppMensual.objects.filter(año=anio, mes=mes).first()
    return float(fila.valor) if fila else None


def test_con_el_mes_guardado_no_llama_a_la_api(entorno):
    from apps.ppa.models import IppMensual
    from apps.ppa.services.ipp import asegurar_mes

    IppMensual.objects.create(año=2026, mes=9, valor=187.47)
    assert asegurar_mes(2026, 9) is True
    assert entorno["llamadas"] == 0


def test_trae_los_que_faltan_sin_pisar_los_existentes(entorno):
    from apps.ppa.models import IppMensual
    from apps.ppa.services.ipp import asegurar_mes

    IppMensual.objects.create(año=2026, mes=8, valor=180.0)  # cargado a mano
    entorno["filas"] = [
        {"year": 2026, "month": 8, "ipp": 185.33, "date": "2026-09-10"},
        {"year": 2026, "month": 9, "ipp": 187.47, "date": "2026-10-08"},
    ]
    assert asegurar_mes(2026, 9) is True
    assert _valor(2026, 9) == 187.47
    assert _valor(2026, 8) == 180.0  # no se tocó


def test_si_la_api_no_lo_tiene_no_reintenta_en_cada_carga(entorno):
    from apps.ppa.services.ipp import asegurar_mes

    entorno["filas"] = [{"year": 2026, "month": 9, "ipp": 187.47, "date": "2026-10-08"}]
    assert asegurar_mes(2026, 10) is False
    assert asegurar_mes(2026, 10) is False
    assert entorno["llamadas"] == 1
    assert _valor(2026, 9) == 187.47  # de paso guardó el que sí había


def test_si_la_api_se_cae_no_revienta(entorno):
    from apps.ppa.services.ipp import asegurar_mes

    entorno["falla"] = True
    assert asegurar_mes(2026, 10) is False
    assert _valor(2026, 10) is None
