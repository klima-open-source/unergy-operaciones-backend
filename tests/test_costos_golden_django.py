"""GOLDEN de los costos del Panel Contable (`apps.contabilidad.services.costos`).

Red de seguridad para el corte a la tabla única. Congela:
- `_tarifa_indexada_periodo`: la tarifa $/kWh vigente según la indexación por aniversario.
- `elegir_contrato_representacion`: qué contrato manda (lee `estado == "firmado"`, así que
  este golden CAZA el desajuste de estado cuando ContratoServicio pase a proxy sobre
  `contratos`, cuyo `estado` usa otros valores).
- `internet_desde_starlink` y `aplicar_costos_modulo`: lógica pura de composición.
- `valores_facturas_modulo`: el cálculo de Representación/CGM/Administración (con BD).

Cuando el corte reescriba estos caminos, las funciones puras cambian su SETUP a los
modelos nuevos pero los valores esperados quedan congelados; si un monto se mueve, falla.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from apps.contabilidad.services import costos


# ----------------------------------------------------------------- puras

def test_golden_tarifa_indexada_periodo():
    idx = [
        {"año": 2024, "valor": 5.0, "esBase": True},
        {"año": 2025, "valor": 5.26},
        {"año": 2026, "valor": 5.52826},
    ]
    firma = date(2024, 3, 15)
    # Aniversario más reciente ya ocurrido (mes de firma = marzo).
    assert costos._tarifa_indexada_periodo(idx, 5.0, firma, "2025-06") == 5.26
    assert costos._tarifa_indexada_periodo(idx, 5.0, firma, "2026-06") == 5.52826
    # Antes del primer aniversario -> cae a la base (esBase).
    assert costos._tarifa_indexada_periodo(idx, 5.0, firma, "2024-01") == 5.0
    # Sin lista -> tarifa base.
    assert costos._tarifa_indexada_periodo([], 6.0, None, "2025-01") == 6.0
    # Esquema alterno de claves (anio / es_base), sin firma (mes 1).
    idx2 = [{"anio": 2024, "valor": 7.0, "es_base": True}]
    assert costos._tarifa_indexada_periodo(idx2, None, date(2024, 5, 1), "2023-01") == 7.0
    assert costos._tarifa_indexada_periodo(idx2, None, None, "2025-06") == 7.0


def test_golden_elegir_contrato_representacion():
    def c(**kw):
        base = dict(estado="firmado", tarifa_representacion=None, tarifa_cgm=None,
                    tarifa_admin=None, id=0)
        base.update(kw)
        return SimpleNamespace(**base)

    # Joropo: gana el que trae MÁS tarifas cargadas (108 con las tres), no el de menor id.
    c108 = c(id=108, tarifa_representacion=6.0, tarifa_cgm=5.0, tarifa_admin=0.038)
    c50 = c(id=50, tarifa_admin=0.038)
    c200 = c(id=200, tarifa_admin=0.05)
    assert costos.elegir_contrato_representacion([c50, c108, c200]).id == 108

    # La vigencia pesa MÁS que las tarifas: un firmado en ceros gana a un terminado lleno.
    c_vig = c(id=10, estado="firmado")
    c_term = c(id=20, estado="terminado", tarifa_representacion=6.0, tarifa_cgm=5.0,
               tarifa_admin=0.038)
    assert costos.elegir_contrato_representacion([c_vig, c_term]).id == 10

    assert costos.elegir_contrato_representacion([]) is None


def test_golden_internet_desde_starlink():
    lineas = [SimpleNamespace(sin_iva=100000, excluido=False),
              SimpleNamespace(sin_iva=50000, excluido=False)]
    assert costos.internet_desde_starlink(lineas) == {
        "Servicio de Internet": {"grupo": "costos", "valor": -150000.0, "fuente": "starlink"}
    }
    # Una línea excluida no cuenta.
    lineas2 = [SimpleNamespace(sin_iva=100000, excluido=True),
               SimpleNamespace(sin_iva=50000, excluido=False)]
    assert costos.internet_desde_starlink(lineas2) == {
        "Servicio de Internet": {"grupo": "costos", "valor": -50000.0, "fuente": "starlink"}
    }
    # Sin líneas vivas -> {} (no un 0 que pise el ER).
    assert costos.internet_desde_starlink([]) == {}


def test_golden_aplicar_costos_modulo():
    base = [{"grupo": "costos", "concepto": "Mantenimiento", "valor": -100.0,
             "hoja": "H", "celda": "A1"}]
    mods = {"Mantenimiento": {"valor": -200.0, "fuente": "om", "iva": True}}
    out = costos.aplicar_costos_modulo(base, mods)
    mant = next(l for l in out if l["concepto"] == "Mantenimiento")
    assert mant["valor"] == -200.0 and mant["fuente"] == "om"
    assert mant["hoja"] is None and mant["celda"] is None
    iva = next(l for l in out if l["concepto"] == "IVA Mantenimiento")
    assert iva["valor"] == -38.0  # round(-200 * 0.19, 2)


# ----------------------------------------------------------------- con BD

@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django
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


def test_golden_valores_facturas_modulo():
    from apps.contratos.models import ContratoServicio
    from apps.proyectos.models import Proyecto

    # Ids altos y propios: las pruebas golden comparten el proceso y a veces la BD
    # en memoria; con ids únicos no chocan con los de otras (p. ej. la de facturación).
    Proyecto.objects.create(id=9001, nombre_comercial="Planta X")
    ContratoServicio.objects.create(
        id=9001, proyecto_id=9001, servicio_aplica="representacion", estado="firmado",
        tarifa_representacion=6.0, tarifa_cgm=5.0, tarifa_admin=0.038,
        fecha_firma_contrato=date(2024, 1, 1),
    )

    out = costos.valores_facturas_modulo(9001, "2025-06", kwh=1000.0, ingreso=1000000.0)

    # Repr/CGM = tarifa (sin indexación -> base) × kWh; Admin = tarifa_admin(%) × ingreso.
    assert out == {
        "Representación": {"grupo": "facturas", "valor": -6000.0, "fuente": "servicios"},
        "CGM": {"grupo": "facturas", "valor": -5000.0, "fuente": "servicios"},
        "Administración": {"grupo": "facturas", "valor": -38000.0, "fuente": "operacion"},
    }
