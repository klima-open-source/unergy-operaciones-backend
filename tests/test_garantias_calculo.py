"""Cálculo puro de la garantía (apps/garantias/services/calculo.py).

Foco: una planta nueva aporta su generación MENSUAL (180.000 kWh por defecto) ×
precio de bolsa, prorrateada por la fracción de mes que cubre la ventana (mes
siguiente = 100%; resto del mes en curso = los días que faltan).
"""
from datetime import date

from apps.garantias.services.calculo import (
    KWH_PLANTA_NUEVA_DEFAULT, calcular_garantia, proyecciones,
)


def test_default_planta_nueva_es_generacion_mensual():
    assert KWH_PLANTA_NUEVA_DEFAULT == 180_000


def test_planta_nueva_mes_completo():
    # 1 planta × 180.000 kWh × 500 COP/kWh × 100% = 90.000.000
    r = calcular_garantia(0.0, precio_cop_kwh=500.0, costo_regulatorio=0.0,
                          plantas_nuevas=1, kwh_planta_nueva=180_000,
                          fraccion_periodo=1.0)
    assert r["valor_plantas_nuevas"] == 90_000_000.0
    assert r["garantia_total"] == 90_000_000.0


def test_planta_nueva_prorrateada_por_fraccion():
    # media ventana → la mitad
    r = calcular_garantia(0.0, 500.0, 0.0, plantas_nuevas=1,
                          kwh_planta_nueva=180_000, fraccion_periodo=0.5)
    assert r["valor_plantas_nuevas"] == 45_000_000.0


def _balance_cero():
    celda = {"proyectado": 0.0, "total": 0.0}
    return {"balance": {"ungg": {"venta_bolsa": dict(celda),
                                 "compra_bolsa_directa": dict(celda)}}}


def test_proyecciones_prorratea_planta_nueva_por_ventana():
    # corte 18-sep: resto de septiembre = 12/30 del mes; mes siguiente = octubre completo.
    r = proyecciones(
        date(2026, 9, 18),
        calcular_balance_fn=lambda anio, mes: _balance_cero(),
        precio_fn=lambda: 500.0,
        regulatorio_fn=lambda anio, mes: {"valor": 0.0, "anio": anio, "mes": mes},
        plantas_nuevas=1, kwh_planta_nueva=180_000,
    )
    v = {x["clave"]: x for x in r["ventanas"]}
    assert v["resto_mes_actual"]["valor_plantas_nuevas"] == 1 * 180_000 * (12 / 30) * 500
    assert v["mes_siguiente"]["valor_plantas_nuevas"] == 1 * 180_000 * 1.0 * 500
