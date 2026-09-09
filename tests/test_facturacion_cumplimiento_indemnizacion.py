"""Valor a indemnizar cuando un PPA queda BAJO EL MÍNIMO.

La energía que faltó por entregar el comprador la compra en bolsa al precio
promedio del mes, más cara que el PPA. La indemnización es ese sobrecosto:
`faltante_kwh × (precio_bolsa − tarifa_ppa)`, en COP, pisado en 0 (si la bolsa
estuvo más barata que el PPA el comprador no se perjudicó).
"""
import os
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

# El build es una función pura (el query de compromisos se sustituye en cada
# test), pero importar el módulo carga modelos de Django: hay que configurarlo
# antes. No se toca ninguna base.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.facturacion.services import cumplimiento
from apps.facturacion.services.cumplimiento import _valor_indemnizar


# ── helper puro ───────────────────────────────────────────────────────────

def test_indemnizar_bolsa_mas_cara_que_ppa():
    # 100.000 kWh faltantes, bolsa 700 vs PPA 300 → 100.000 × 400 = 40.000.000
    piso, bruto = _valor_indemnizar(100_000.0, 700.0, 300.0)
    assert bruto == 40_000_000.0
    assert piso == 40_000_000.0


def test_indemnizar_bolsa_mas_barata_se_pisa_en_cero():
    # bolsa 250 < PPA 300 → bruto negativo, el valor a indemnizar es 0
    piso, bruto = _valor_indemnizar(100_000.0, 250.0, 300.0)
    assert bruto == -5_000_000.0     # el bruto conserva el signo
    assert piso == 0.0               # pero no se cobra indemnización


@pytest.mark.parametrize("faltante,bolsa,tarifa", [
    (0.0, 700.0, 300.0),        # no faltó energía
    (-10.0, 700.0, 300.0),      # faltante no positivo
    (100.0, None, 300.0),       # sin precio de bolsa cargado
    (100.0, 700.0, None),       # sin tarifa del PPA
])
def test_indemnizar_sin_datos_devuelve_none(faltante, bolsa, tarifa):
    assert _valor_indemnizar(faltante, bolsa, tarifa) == (None, None)


# ── build completo (sin DB: se sustituye el query de compromisos) ──────────

def _fake_compromisos(monkeypatch, filas):
    """Sustituye PpaCompromisoEnergia.objects.filter(...) por `filas`."""
    class _Objs:
        def filter(self, **_kw):
            return filas
    monkeypatch.setattr(
        cumplimiento.ppa_models.PpaCompromisoEnergia, "objects", _Objs()
    )


def _linea(ppa_id, kwh, tarifa):
    return {
        "ppa_id": ppa_id, "ppa": f"PPA {ppa_id}", "numero_contrato": f"C{ppa_id}",
        "comprador": "TPLC", "proyecto": "Planta X", "kwh": kwh,
        "tarifa_indexada": tarifa,
    }


def test_build_calcula_indemnizacion_para_bajo_minimo(monkeypatch):
    # mínimo 100 MWh, despachado 60 MWh → faltan 40 MWh = 40.000 kWh.
    # bolsa 700, tarifa 300 → 40.000 × 400 = 16.000.000 COP.
    _fake_compromisos(monkeypatch, [
        SimpleNamespace(contrato_id=1, energia_minima=100, energia_maxima=200),
    ])
    datos = {"periodo": "2026-07", "lineas": [_linea(1, 60_000.0, 300.0)]}

    out = cumplimiento.build(datos, 2026, 7, precio_bolsa=700.0)

    fila = out["filas"][0]
    assert fila["estado"] == "bajo_minimo"
    assert fila["faltante_kwh"] == 40_000.0
    assert fila["tarifa_ppa_cop_kwh"] == 300.0
    assert fila["precio_bolsa_cop_kwh"] == 700.0
    assert fila["valor_indemnizar_cop"] == 16_000_000.0
    assert fila["valor_indemnizar_bruto_cop"] == 16_000_000.0
    assert out["resumen"]["valor_indemnizar_total_cop"] == 16_000_000.0
    assert out["resumen"]["precio_bolsa_cop_kwh"] == 700.0


def test_build_no_indemniza_a_quien_cumple(monkeypatch):
    # despachado 120 MWh ≥ mínimo 100 → cumple, sin indemnización.
    _fake_compromisos(monkeypatch, [
        SimpleNamespace(contrato_id=1, energia_minima=100, energia_maxima=200),
    ])
    datos = {"periodo": "2026-07", "lineas": [_linea(1, 120_000.0, 300.0)]}

    out = cumplimiento.build(datos, 2026, 7, precio_bolsa=700.0)

    fila = out["filas"][0]
    assert fila["estado"] == "cumple"
    assert fila["valor_indemnizar_cop"] is None
    assert out["resumen"]["valor_indemnizar_total_cop"] == 0.0


def test_build_sin_precio_bolsa_no_calcula(monkeypatch):
    # bajo mínimo pero sin precio de bolsa cargado → no se puede valorar.
    _fake_compromisos(monkeypatch, [
        SimpleNamespace(contrato_id=1, energia_minima=100, energia_maxima=200),
    ])
    datos = {"periodo": "2026-07", "lineas": [_linea(1, 60_000.0, 300.0)]}

    out = cumplimiento.build(datos, 2026, 7, precio_bolsa=None)

    fila = out["filas"][0]
    assert fila["estado"] == "bajo_minimo"
    assert fila["valor_indemnizar_cop"] is None
    assert out["resumen"]["valor_indemnizar_total_cop"] == 0.0
