"""Precio de bolsa mensual (SIMEM 709b84) para el valor a indemnizar.

Réplica del Excel de Compensación: versión más nueva disponible (TXF gana, TXR si
TXF aún no salió), redondeo 2 dec por hora, promedio de todas las horas.
`bolsa_mensual` se prueba con un cliente httpx inyectado (MockTransport), sin red.
"""
import os

import httpx
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.mercado_xm.services import simem


def _rec(fecha_hora, valor, version="TXF", variable="PB_Nal"):
    return {"CodigoVariable": variable, "FechaHora": fecha_hora, "Version": version,
            "UnidadMedida": "COP/kWh", "Valor": valor}


def _cliente(records):
    payload = {"result": {"records": records}}
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))


# ── bolsa_horaria (puro) ───────────────────────────────────────────────────

def test_bolsa_horaria_redondea_y_txf_gana_a_txr():
    recs = [
        _rec("2026-08-01 00:00:00", 988.9669, "TXR"),   # más preliminar
        _rec("2026-08-01 00:00:00", 988.9617, "TXF"),   # gana TXF, redondea a 988.96
        _rec("2026-08-01 01:00:00", 649.4617, "TXF"),
    ]
    h = simem.bolsa_horaria(recs)
    assert h[("2026-08-01", "00")] == 988.96
    assert h[("2026-08-01", "01")] == 649.46


def test_bolsa_horaria_usa_txr_si_no_hay_txf():
    # Mes reciente: TXF aún no salió → se usa TXR.
    recs = [_rec("2026-08-01 00:00:00", 900.005, "TXR")]
    assert simem.bolsa_horaria(recs) == {("2026-08-01", "00"): 900.0}


# ── bolsa_mensual ──────────────────────────────────────────────────────────

def test_bolsa_mensual_promedia_todas_las_horas():
    recs = [
        _rec("2026-08-01 00:00:00", 900.00),
        _rec("2026-08-01 01:00:00", 800.00),
        _rec("2026-08-02 00:00:00", 700.00),
    ]
    out = simem.bolsa_mensual(2026, 8, client=_cliente(recs))
    assert out["precio_bolsa"] == 800.0        # (900+800+700)/3
    assert out["horas"] == 3
    assert out["dias"] == 2
    assert out["horas_techadas"] == 0
    assert out["detalle"]["2026-08-01"] == {"00": 900.0, "01": 800.0}


def test_bolsa_mensual_aplica_techo_por_hora():
    recs = [
        _rec("2026-08-01 00:00:00", 1000.00),  # > techo → se recorta a 850
        _rec("2026-08-01 01:00:00", 800.00),   # < techo → queda
    ]
    out = simem.bolsa_mensual(2026, 8, techo=850.0, client=_cliente(recs))
    assert out["precio_bolsa"] == 825.0        # (850 + 800) / 2
    assert out["horas_techadas"] == 1
    assert out["detalle"]["2026-08-01"]["00"] == 850.0


def test_bolsa_mensual_sin_datos_devuelve_none():
    out = simem.bolsa_mensual(2026, 8, client=_cliente([]))
    assert out["precio_bolsa"] is None
    assert out["horas"] == 0


def test_bolsa_mensual_tolera_caida_de_simem():
    def _boom(req):
        raise httpx.ConnectError("SIMEM caído")
    out = simem.bolsa_mensual(2026, 8, client=httpx.Client(transport=httpx.MockTransport(_boom)))
    assert out["precio_bolsa"] is None
