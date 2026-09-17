"""Parser del BalCttos: NETO DE COMPRAS EN BOLSA. Funciones puras + xlsx en memoria."""
import io

import openpyxl

from app.services.balcttos import (
    _norm,
    deficit_por_contrato,
    deficit_por_contrato_de_bytes,
    neto_compras_bolsa,
    neto_compras_bolsa_de_bytes,
    proyectar_neto_mwh,
)


def _fila(concepto, fecha, horas):
    return {"concepto": concepto, "fecha": fecha, "horas": horas}


def _fila_c(concepto, fecha, codigo, comprador, horas):
    return {"concepto": concepto, "fecha": fecha, "codigo": codigo,
            "comprador": comprador, "horas": horas}


def test_deficit_por_contrato_reparte_cada_hora_proporcional_a_la_obligacion():
    # Hora 0: solo C1 demanda (1000 kWh); deficit 500 -> todo a C1.
    # Hora 1: C1 y C2 demandan 500 c/u; deficit 200 -> 100 y 100.
    # Esperado: C1 = (500+100)/1000 = 0.6 MWh ; C2 = 100/1000 = 0.1 MWh
    def horas(v0, v1):
        return [v0, v1] + [0.0] * 22
    filas = [
        _fila_c("CONTRATO DE VENTA", "2026-08-01", "C1", "TPLC", horas(1000.0, 500.0)),
        _fila_c("CONTRATO DE VENTA", "2026-08-01", "C2", "NEUC", horas(0.0, 500.0)),
        _fila_c("NETO DE COMPRAS EN BOLSA", "2026-08-01", None, None, horas(500.0, 200.0)),
    ]
    out = deficit_por_contrato(filas)
    assert out["por_contrato"]["C1"] == 0.6
    assert out["por_contrato"]["C2"] == 0.1
    assert out["total_mwh"] == 0.7


def test_deficit_hora_sin_obligacion_no_se_reparte():
    # Deficit en una hora donde ningun contrato demanda: no se puede atribuir,
    # queda fuera del reparto (pero el total refleja solo lo atribuido).
    def horas(v):
        return [v] + [0.0] * 23
    filas = [
        _fila_c("CONTRATO DE VENTA", "2026-08-01", "C1", "TPLC", horas(0.0)),
        _fila_c("NETO DE COMPRAS EN BOLSA", "2026-08-01", None, None, horas(300.0)),
    ]
    out = deficit_por_contrato(filas)
    assert out["por_contrato"] == {}
    assert out["total_mwh"] == 0.0


def test_deficit_por_contrato_guarda_comprador():
    def horas(v):
        return [v] + [0.0] * 23
    filas = [
        _fila_c("CONTRATO DE VENTA", "2026-08-01", "C1", "TPLC", horas(1000.0)),
        _fila_c("NETO DE COMPRAS EN BOLSA", "2026-08-01", None, None, horas(1000.0)),
    ]
    out = deficit_por_contrato(filas)
    assert out["comprador"]["C1"] == "TPLC"


def test_norm_quita_acentos():
    assert _norm("  NETO de Compras ") == "neto de compras"


def test_suma_solo_neto_compras_y_convierte_a_mwh():
    filas = [
        _fila("NETO DE COMPRAS EN BOLSA", "2026-08-01", [1000.0] * 24),   # 24.000 kWh = 24 MWh
        _fila("NETO DE VENTAS EN BOLSA", "2026-08-01", [9999.0] * 24),    # ignorado
        _fila("CONTRATO DE VENTA", "2026-08-01", [5000.0] * 24),          # ignorado
    ]
    out = neto_compras_bolsa(filas)
    assert out["total_mwh"] == 24.0
    assert out["por_dia"] == {"2026-08-01": 24.0}


def test_agrupa_por_dia_y_suma_varios_dias():
    filas = [
        _fila("NETO DE COMPRAS EN BOLSA", "2026-08-01 00:00:00", [500.0] * 24),  # 12 MWh
        _fila("NETO DE COMPRAS EN BOLSA", "2026-08-02 00:00:00", [1000.0] * 24), # 24 MWh
    ]
    out = neto_compras_bolsa(filas)
    assert out["por_dia"] == {"2026-08-01": 12.0, "2026-08-02": 24.0}
    assert out["total_mwh"] == 36.0


def test_horas_no_numericas_se_ignoran_sin_romper():
    filas = [_fila("NETO DE COMPRAS EN BOLSA", "2026-08-01", [1000.0, None, "x"] + [0.0] * 21)]
    assert neto_compras_bolsa(filas)["total_mwh"] == 1.0  # solo la primera hora


def _xlsx_balcttos_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    # header con las 8 columnas fijas + 24 horas
    ws.append(["FechaDocumento", "CONCEPTO", "MERCADO", "CODIGO CONTRATO", "COMPRADOR",
               "VENDEDOR", "TIPO DE DESPACHO", "TIPO ASIGNA"] + [f"HORA {h:02d}" for h in range(1, 25)])
    ws.append(["2026-08-01", "NETO DE COMPRAS EN BOLSA", "NACIONAL", "C1", "TPLC",
               "UNGG", "x", "y"] + [1000.0] * 24)  # 24 MWh
    ws.append(["2026-08-01", "CONTRATO DE VENTA", "NO REGULADO", "C1", "TPLC",
               "UNGG", "x", "y"] + [7777.0] * 24)  # ignorado
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_lee_xlsx_en_memoria():
    out = neto_compras_bolsa_de_bytes(_xlsx_balcttos_bytes())
    assert out["total_mwh"] == 24.0


def _xlsx_balcttos_contratos_bytes():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["FechaDocumento", "CONCEPTO", "MERCADO", "CODIGO CONTRATO", "COMPRADOR",
               "VENDEDOR", "TIPO DE DESPACHO", "TIPO ASIGNA"] + [f"HORA {h:02d}" for h in range(1, 25)])
    # Hora 0: solo C1 (1000); Hora 1: C1 y C2 (500 c/u)
    ws.append(["2026-08-01", "CONTRATO DE VENTA", "NO REGULADO", "C1", "TPLC",
               "UNGG", "PC", "NB"] + [1000.0, 500.0] + [0.0] * 22)
    ws.append(["2026-08-01", "CONTRATO DE VENTA", "NO REGULADO", "C2", "NEUC",
               "UNGG", "PC", "NB"] + [0.0, 500.0] + [0.0] * 22)
    ws.append(["2026-08-01", "NETO DE COMPRAS EN BOLSA", "NACIONAL", None, None,
               "UNGG", "", ""] + [500.0, 200.0] + [0.0] * 22)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_deficit_por_contrato_de_bytes_lee_codigo_y_atribuye():
    out = deficit_por_contrato_de_bytes(_xlsx_balcttos_contratos_bytes())
    assert out["por_contrato"]["C1"] == 0.6
    assert out["por_contrato"]["C2"] == 0.1
    assert out["comprador"]["C1"] == "TPLC"


def test_proyectar_neto_extrapola_tasa_diaria_real():
    # 245.2 MWh reales en 19 días -> tasa 12.905/día
    # proyectar a 30 días (septiembre) = 12.905 * 30
    assert proyectar_neto_mwh(245.2, 19, 30) == 245.2 / 19 * 30
    # a los días que faltan del mes (ej. 12) = tasa * 12
    assert proyectar_neto_mwh(245.2, 19, 12) == 245.2 / 19 * 12


def test_proyectar_neto_sin_dato_es_cero():
    assert proyectar_neto_mwh(0.0, 0, 30) == 0.0
