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


# ── Las horas que XM publica como PTB y no como PB_Nal ──────────────────────
#
# El 2026-09 el export salió con 18 huecos, todos en las horas pico (18, 19, 20).
# No era un fallo de la descarga: para esas horas SIMEM NO publica `PB_Nal`,
# publica `PTB` — el precio de las transacciones en bolsa, que es lo que aplica
# cuando el precio de bolsa supera el de escasez de activación. Los 720 registros
# del mes salían 702 PB_Nal + 18 PTB, y las 18 coincidían una a una con los
# huecos.
#
# Omitirlas no dejaba el número incompleto nada más: sesgaba el PNBA hacia abajo,
# porque las que faltaban eran justo las caras, y con eso la indemnización
# —faltante × (bolsa − tarifa PPA)— salía subestimada.

def test_sin_pb_nal_se_usa_el_ptb_de_esa_hora():
    horas = simem.bolsa_horaria([
        _rec("2026-09-15 17:00", 500.0),
        _rec("2026-09-15 18:00", 891.44, variable="PTB"),
    ])
    assert horas[("2026-09-15", "17")] == 500.0
    assert horas[("2026-09-15", "18")] == 891.44


def test_el_pb_nal_le_gana_al_ptb_en_la_misma_hora():
    """Cuando XM publica las dos, la de bolsa es la que manda."""
    horas = simem.bolsa_horaria([
        _rec("2026-09-15 18:00", 891.44, variable="PTB"),
        _rec("2026-09-15 18:00", 700.0),
    ])
    assert horas[("2026-09-15", "18")] == 700.0


def test_el_orden_de_los_registros_no_cambia_el_resultado():
    """El PB_Nal gana venga antes o después del PTB en la respuesta."""
    pb = _rec("2026-09-15 18:00", 700.0)
    ptb = _rec("2026-09-15 18:00", 891.44, variable="PTB")
    assert simem.bolsa_horaria([pb, ptb]) == simem.bolsa_horaria([ptb, pb])


def test_entre_dos_ptb_sigue_ganando_la_version_mas_nueva():
    horas = simem.bolsa_horaria([
        _rec("2026-09-15 18:00", 880.0, version="TX2", variable="PTB"),
        _rec("2026-09-15 18:00", 891.44, version="TXF", variable="PTB"),
    ])
    assert horas[("2026-09-15", "18")] == 891.44


def test_las_horas_de_ptb_entran_al_promedio_del_mes():
    cliente = _cliente([
        _rec("2026-09-01 00:00", 100.0),
        _rec("2026-09-01 01:00", 900.0, variable="PTB"),
    ])
    r = simem.bolsa_mensual(2026, 9, client=cliente)
    assert r["horas"] == 2
    assert r["precio_bolsa"] == 500.0


def test_se_reporta_cuantas_horas_vinieron_del_ptb():
    """Hay que poder decirlo en pantalla: son horas sobre el precio de escasez,
    no un dato cualquiera."""
    cliente = _cliente([
        _rec("2026-09-01 00:00", 100.0),
        _rec("2026-09-01 01:00", 900.0, variable="PTB"),
    ])
    assert simem.bolsa_mensual(2026, 9, client=cliente)["horas_ptb"] == 1


def test_un_mes_sin_ptb_lo_reporta_en_cero():
    cliente = _cliente([_rec("2026-09-01 00:00", 100.0)])
    assert simem.bolsa_mensual(2026, 9, client=cliente)["horas_ptb"] == 0


def test_una_variable_desconocida_se_sigue_ignorando():
    """El dataset trae dos variables; cualquier otra no es precio de bolsa."""
    horas = simem.bolsa_horaria([_rec("2026-09-15 18:00", 1.0, variable="OTRA")])
    assert horas == {}
