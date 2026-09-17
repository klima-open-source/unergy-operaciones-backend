"""Atribución de la garantía por contrato: reparto puro + mapeo a proyecto."""
import io

import openpyxl
import pytest

from apps.garantias.services.atribucion import (
    atribuir, garantia_por_contrato_de_bytes, repartir,
)


def _xlsx_balcttos(filas):
    """filas = [(concepto, codigo, comprador, [h0, h1, ...])] -> bytes de BalCttos."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["FechaDocumento", "CONCEPTO", "MERCADO", "CODIGO CONTRATO", "COMPRADOR",
               "VENDEDOR", "TIPO DE DESPACHO", "TIPO ASIGNA"] + [f"HORA {h:02d}" for h in range(1, 25)])
    for concepto, codigo, comprador, horas in filas:
        h = list(horas) + [0.0] * (24 - len(horas))
        ws.append(["2026-08-01", concepto, "NO REGULADO", codigo, comprador, "UNGG", "PC", "NB"] + h)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_repartir_distribuye_total_por_peso_de_deficit():
    por_contrato = {"C1": 0.6, "C2": 0.4}
    filas = repartir(por_contrato, total_garantia=1000.0)
    d = {f["codigo"]: f for f in filas}
    assert d["C1"]["deficit_mwh"] == 0.6
    assert d["C1"]["pct"] == 0.6
    assert d["C1"]["monto"] == 600.0
    assert d["C2"]["monto"] == 400.0


def test_repartir_ordena_de_mayor_a_menor():
    filas = repartir({"A": 1.0, "B": 9.0}, total_garantia=100.0)
    assert [f["codigo"] for f in filas] == ["B", "A"]


def test_repartir_sin_deficit_devuelve_vacio():
    assert repartir({}, total_garantia=1000.0) == []
    assert repartir({"C1": 0.0}, total_garantia=1000.0) == []


def test_atribuir_enriquece_con_contrato_y_proyecto_del_mapa():
    por_contrato = {"86512": 0.6, "C2": 0.4}
    comprador = {"86512": "NEUC", "C2": "NEUC"}
    mapa = {"86512": {"proyecto_id": 4, "proyecto": "Minigranja Solar Baraya",
                      "contrato_interno": "NEU I (SolAyura)"}}
    filas = atribuir(por_contrato, total_garantia=1000.0, mapa=mapa, comprador=comprador)
    top = filas[0]
    assert top["codigo"] == "86512"
    assert top["monto"] == 600.0
    assert top["contrato"] == "NEU I (SolAyura)"
    assert top["proyecto"] == "Minigranja Solar Baraya"
    assert top["proyecto_id"] == 4
    assert top["comprador"] == "NEUC"


def test_atribuir_contrato_sin_mapa_queda_con_nulos_pero_no_se_pierde():
    filas = atribuir({"XX": 1.0}, total_garantia=500.0, mapa={}, comprador={"XX": "COXG"})
    assert len(filas) == 1
    assert filas[0]["contrato"] is None
    assert filas[0]["proyecto_id"] is None
    assert filas[0]["comprador"] == "COXG"
    assert filas[0]["monto"] == 500.0


def test_garantia_por_contrato_de_bytes_parsea_mapea_y_reparte():
    # Hora 0: solo C1 demanda; deficit 1000 -> todo el total va a C1.
    contenido = _xlsx_balcttos([
        ("CONTRATO DE VENTA", "86512", "NEUC", [1000.0]),
        ("NETO DE COMPRAS EN BOLSA", None, None, [1000.0]),
    ])
    mapa_fake = {"86512": {"proyecto_id": 4, "proyecto": "Baraya",
                           "contrato_interno": "NEU I"}}
    filas = garantia_por_contrato_de_bytes(
        contenido, total_garantia=2000.0, mapear_fn=lambda cods: mapa_fake,
    )
    assert len(filas) == 1
    assert filas[0]["contrato"] == "NEU I"
    assert filas[0]["monto"] == 2000.0
    assert filas[0]["comprador"] == "NEUC"
