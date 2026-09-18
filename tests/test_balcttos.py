"""Parser del BalCttos: NETO DE COMPRAS EN BOLSA. Funciones puras + xlsx en memoria."""
import io

import openpyxl

from app.services.balcttos import (
    _norm,
    neto_compras_bolsa,
    neto_compras_bolsa_de_bytes,
    proyectar_neto_mwh,
)


def _fila(concepto, fecha, horas):
    return {"concepto": concepto, "fecha": fecha, "horas": horas}


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


def test_proyectar_neto_extrapola_tasa_diaria_real():
    # 245.2 MWh reales en 19 días -> tasa 12.905/día
    # proyectar a 30 días (septiembre) = 12.905 * 30
    assert proyectar_neto_mwh(245.2, 19, 30) == 245.2 / 19 * 30
    # a los días que faltan del mes (ej. 12) = tasa * 12
    assert proyectar_neto_mwh(245.2, 19, 12) == 245.2 / 19 * 12


def test_proyectar_neto_sin_dato_es_cero():
    assert proyectar_neto_mwh(0.0, 0, 30) == 0.0
