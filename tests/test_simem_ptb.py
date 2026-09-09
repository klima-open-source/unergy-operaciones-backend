"""Techo de bolsa (PTB) de SIMEM (dataset 709b84) y el capping día a día.

`ptb_diario` es puro; `precio_bolsa_techado` combina el techo con nuestro precio de
bolsa diario — se le inyecta un cliente httpx (MockTransport) y se sustituye la
consulta a `precios_bolsa_diario`, así no toca ni red ni base.
"""
import json
import os

import httpx
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.mercado_xm.services import simem


def _rec(dia_hora, valor, version="TX1", variable="PB_Nal"):
    return {"CodigoVariable": variable, "FechaHora": dia_hora, "Version": version,
            "UnidadMedida": "COP/kWh", "Valor": valor}


# ── ptb_diario (puro) ──────────────────────────────────────────────────────

def test_ptb_diario_promedia_horas_de_la_mejor_version():
    recs = [
        _rec("2026-07-01 00:00:00", 100, "TX1"),
        _rec("2026-07-01 01:00:00", 200, "TX1"),
        # TX2 es más definitiva: gana y descarta las TX1 de ese día
        _rec("2026-07-01 00:00:00", 500, "TX2"),
        _rec("2026-07-01 01:00:00", 700, "TX2"),
    ]
    assert simem.ptb_diario(recs) == {"2026-07-01": 600.0}   # (500+700)/2


def test_ptb_diario_ignora_otras_variables():
    recs = [_rec("2026-07-01 00:00:00", 100), _rec("2026-07-01 01:00:00", 999, variable="OTRA")]
    assert simem.ptb_diario(recs) == {"2026-07-01": 100.0}


# ── precio_bolsa_techado (con cliente y DB inyectados) ─────────────────────

def _cliente_simem(records):
    payload = {"result": {"records": records}}
    return httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=payload)))


def test_techado_recorta_solo_los_dias_donde_el_ptb_es_menor(monkeypatch):
    monkeypatch.setattr(simem, "_nuestro_bolsa_diario",
                        lambda a, m: {"2026-07-01": 600.0, "2026-07-02": 800.0})
    # PTB: día 1 = 500 (techa 600→500); día 2 = 900 (no techa, 800<900)
    recs = [_rec("2026-07-01 00:00:00", 500), _rec("2026-07-02 00:00:00", 900)]
    out = simem.precio_bolsa_techado(2026, 7, client=_cliente_simem(recs))

    assert out["dias"] == 2
    assert out["dias_techados"] == 1
    assert out["ptb_disponible"] is True
    assert out["precio_bolsa"] == 650.0          # (500 + 800) / 2


def test_techado_sin_datos_nuestros_devuelve_none(monkeypatch):
    monkeypatch.setattr(simem, "_nuestro_bolsa_diario", lambda a, m: {})
    out = simem.precio_bolsa_techado(2026, 7, client=_cliente_simem([]))
    assert out == {"precio_bolsa": None, "dias": 0, "dias_techados": 0,
                   "ptb_disponible": False, "ptb_promedio": None}


def test_techado_sin_ptb_no_recorta_y_marca_no_disponible(monkeypatch):
    monkeypatch.setattr(simem, "_nuestro_bolsa_diario",
                        lambda a, m: {"2026-07-01": 600.0, "2026-07-02": 800.0})
    # SIMEM sin registros → no hay techo → promedio sin recortar
    out = simem.precio_bolsa_techado(2026, 7, client=_cliente_simem([]))
    assert out["precio_bolsa"] == 700.0          # (600 + 800) / 2
    assert out["dias_techados"] == 0
    assert out["ptb_disponible"] is False


def test_techado_tolera_caida_de_simem(monkeypatch):
    monkeypatch.setattr(simem, "_nuestro_bolsa_diario", lambda a, m: {"2026-07-01": 600.0})

    def _boom(req):
        raise httpx.ConnectError("SIMEM caído")

    cli = httpx.Client(transport=httpx.MockTransport(_boom))
    out = simem.precio_bolsa_techado(2026, 7, client=cli)
    assert out["precio_bolsa"] == 600.0          # sin techo, no rompe
    assert out["ptb_disponible"] is False
