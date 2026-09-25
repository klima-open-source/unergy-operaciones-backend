"""Regresión: el serializer de lectura de PPA expone las tarifas MENSUALES.

Tras el corte a la tabla única, `PpaContrato` es un proxy de `Contrato`, y `.tarifas`
en el proxy es `ContratoTarifa` (el modelo nuevo versionado), NO la serie mensual
`PpaTarifa`. El `ContratoSerializer` debe leer `tarifas_ppa` para seguir devolviendo
{año, mes, tarifa}. Sin esto, `GET /ppa` reventaba con:
    AttributeError: 'ContratoTarifa' object has no attribute 'año'
"""
from datetime import date

import pytest

django = pytest.importorskip("django")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    from django.conf import settings

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
    assert connections["default"].vendor == "sqlite"
    yield
    teardown_test_environment()


def test_contrato_serializer_expone_tarifas_mensuales():
    from apps.ppa.models import PpaContrato, PpaTarifa
    from api.v1.ppa.serializers import ContratoSerializer

    c = PpaContrato.objects.create(
        id=9101, numero_codigo_contrato="UNERGY-TEST", nombre_interno="Test",
        tipo_contrato="venta", fecha_inicio=date(2024, 1, 1),
    )
    PpaTarifa.objects.create(id=9101, contrato=c, año=2024, mes=1, tarifa=5.0)
    PpaTarifa.objects.create(id=9102, contrato=c, año=2024, mes=2, tarifa=5.26)

    data = ContratoSerializer(c).data
    assert data["tipo_contrato"] == "venta"
    assert len(data["tarifas"]) == 2
    assert {t["año"] for t in data["tarifas"]} == {2024}
    assert {t["mes"] for t in data["tarifas"]} == {1, 2}
    # No debe traer ContratoTarifa (el modelo versionado nuevo) en este campo.
    assert all("tarifa" in t and "vigencia" not in t for t in data["tarifas"])
