"""GOLDEN del cálculo de facturación de energía (Django `calculo.periodo`).

Red de seguridad para el corte a la tabla única de contratos. Hoy
`apps/facturacion/services/calculo.py` NO tiene ninguna prueba propia: los tests
de facturación que existen (`test_facturacion_contrato_terminado.py`, etc.)
apuntan al árbol FastAPI apagado (`app.models`), no al código Django que corre.

Este test FIJA los números que el cálculo Django produce HOY para un escenario
conocido (SIC 89902 / GD San Pelayo → Terpel 8, con un contrato terminado a
mitad de mes). Cuando `calculo.py` se reescriba para leer de `Contrato` en vez de
`PpaContrato`/`PpaTarifa`, la SETUP de este test cambiará a los modelos nuevos,
pero los VALORES ESPERADOS de abajo tienen que quedar idénticos: si un monto se
mueve, el corte cambió la facturación y el test lo caza.
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
    teardown_test_environment()


def _setup_terpel8():
    """SIC 89902 (GD San Pelayo) en Terpel 8, terminado el 23-jul-2026."""
    from apps.facturacion import models as fa
    from apps.mercado_xm import models as mx
    from apps.ppa import models as ppa
    from apps.proyectos import models as py

    py.Proyecto.objects.create(id=1, nombre_comercial="GD San Pelayo")
    ppa.PpaContrato.objects.create(
        id=5, nombre_interno="Terpel 8 (Bancolo)",
        numero_codigo_contrato="UNERGY-T8", periodo_indexacion_base="2025-01",
        valor_indexacion_base=175.88, tipo_contrato="venta",
    )
    mx.AsicSolicitud.objects.create(
        id=212, proyecto_id=1, contrato_ppa_id=5, codigo_sic_contrato="89902",
        tipo_solicitud="registro", estado_solicitud="publicado",
        reemplaza_anterior=True, es_duplicado=False,
        fecha_solicitud=date(2026, 6, 1),
        fecha_inicio=date(2026, 6, 1), fecha_fin=date(2026, 7, 23),
    )
    mx.AsicSolicitud.objects.create(
        id=231, proyecto_id=None, contrato_ppa_id=5, codigo_sic_contrato="89902",
        tipo_solicitud="terminacion", estado_solicitud="publicado",
        reemplaza_anterior=True, es_duplicado=False,
        fecha_solicitud=date(2026, 7, 23),
        fecha_inicio=None, fecha_fin=date(2026, 7, 23),
    )
    ppa.PpaTarifa.objects.create(id=1, contrato_id=5, año=2026, mes=7, tarifa=298.8)
    ppa.IppMensual.objects.create(id=1, año=2026, mes=7, valor=186.35)
    mx.PrecioBolsaMensual.objects.create(id=1, año=2026, mes=7, valor=710.2606)
    mx.DespachoContratoMensual.objects.create(
        id=1, periodo="2026-07", codigo_sic_contrato="89902", comprador="TPLC",
        tipo="LARGO PLAZO", kwh=126479.07, dias=22,
        fecha_min=date(2026, 7, 1), fecha_max=date(2026, 7, 22),
    )


def test_golden_facturacion_terpel8():
    from apps.facturacion.services import calculo

    _setup_terpel8()
    data = calculo.periodo("2026-07")

    # --- Línea del contrato terminado: se factura a su PPA, no cae a bolsa ---
    linea = next(l for l in data["lineas"] if l["contrato"] == "89902")
    assert linea["estado"] == "ok"
    assert linea["ppa"] == "Terpel 8 (Bancolo)"
    assert linea["kwh"] == 126479.07
    assert linea["tarifa_base"] == 298.8
    assert linea["ipp_base"] == 175.88
    assert linea["ipp_mes"] == 186.35
    # GOLDEN congelado: round(298.8 * 186.35 / 175.88, 2)
    assert linea["tarifa_indexada"] == 316.59
    assert linea["facturacion"] == 40042008.77

    # --- Resumen del período (GOLDEN) ---
    r = data["resumen"]
    assert r["contratos"] == 1
    assert r["facturables"] == 1
    assert r["sin_ppa"] == 0
    assert r["kwh_total"] == 126479.07
    assert r["facturacion_total"] == 40042008.77
    assert r["ingreso_bolsa"] == 0
    assert r["ingreso_total"] == 40042008.77
    assert r["facturas"] == 1

    # --- Factura agrupada (GOLDEN) ---
    facturas = {g["factura"]: g for g in data["por_factura"]}
    assert "Terpel 8 (Bancolo)" in facturas
    t8 = facturas["Terpel 8 (Bancolo)"]
    assert t8["contratos_sic"] == ["89902"]
    assert t8["kwh"] == 126479.07
    assert t8["facturacion"] == 40042008.77
    assert t8["tarifa_indexada"] == 316.59
    assert t8["tarifa_mixta"] is False
    assert not any(g.get("sin_ppa") for g in data["por_factura"])
