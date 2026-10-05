"""Costo regulatorio del mes desde la hoja 'Facturas XM' del Cruce de facturas.
Cálculo puro: sin BD, sin red, sin reloj."""
from apps.garantias.services.costo_regulatorio import (
    _norm,
    costo_regulatorio_de_facturas,
)


def _factura(asic, tipo, lineas):
    return {"asic": asic, "tipo": tipo, "lineas": lineas}


def test_norm_quita_acentos_y_normaliza():
    assert _norm("  Energía en Bolsa ") == "energia en bolsa"
    assert _norm("COMERCIALIZADOR") == "comercializador"


def test_excluye_comercializador_completo():
    facturas = [
        _factura("ASIC1", "COMERCIALIZADOR", [("Servicios de administracion sic", 800000.0),
                                              ("Valor total", 800000.0)]),
        _factura("ASIC2", "GENERADOR", [("Fazni", 999626.0), ("Valor total", 999626.0)]),
    ]
    assert costo_regulatorio_de_facturas(facturas) == 999626.0


def test_excluye_energia_en_bolsa_arranque_y_subtotales():
    facturas = [_factura("ASIC3", "GENERADOR", [
        ("Arranque y parada", 9658866.0),    # excluido: XM no lo mete en el Valor Garantía
        ("Cargo por confiabilidad", 19933106.0),
        ("Energia en bolsa", 110102600.0),   # excluido: es "compras"
        ("Valor total", 139694572.0),         # subtotal: no sumar
    ])]
    # solo el cargo por confiabilidad
    assert costo_regulatorio_de_facturas(facturas) == 19933106.0


def test_iva_generador_no_entra_y_total_servicios_no():
    facturas = [_factura("ASIC4", "GENERADOR", [
        ("+ i.v.a. (19%)", 1742857.0),       # excluido: XM usa cargos base sin IVA
        ("Servicios de administracion sic", 9172932.0),
        ("Servicios despacho y coordinacion cnd", 25684211.0),
        ("Total servicios de administracion sic", 10915789.0),  # subtotal: no sumar
        ("Valor total", 36600000.0),                            # subtotal: no sumar
    ])]
    # servicios admin SIC + despacho CND, sin IVA
    assert costo_regulatorio_de_facturas(facturas) == 34857143.0


def test_total_julio_2026_reproduce_valor_referencia():
    facturas = [
        _factura("ASIC125059", "COMERCIALIZADOR", [
            ("+ i.v.a. (19%)", 151994.0), ("Servicios de administracion sic", 799970.0),
            ("Servicios despacho y coordinacion cnd", 426693.0),
            ("Total servicios de administracion sic", 951964.0), ("Valor total", 1378657.0)]),
        _factura("ASIC125064", "GENERADOR", [("Fazni", 999626.0), ("Valor total", 999626.0)]),
        _factura("ASIC125263", "GENERADOR", [
            ("Arranque y parada", 9658866.0), ("Cargo por confiabilidad", 19933106.0),
            ("Energia en bolsa", 110102600.0), ("Valor total", 139694572.0)]),
        _factura("ASIC125542", "GENERADOR", [
            ("+ i.v.a. (19%)", 1742857.0), ("Servicios de administracion sic", 9172932.0),
            ("Servicios despacho y coordinacion cnd", 25684211.0),
            ("Total servicios de administracion sic", 10915789.0), ("Valor total", 36600000.0)]),
    ]
    # Regla calibrada: GENERADOR sin energía-en-bolsa, sin arranque/parada, sin IVA.
    # FAZNI 999.626 + Confiab 19.933.106 + (admin 9.172.932 + despacho 25.684.211) = 55.789.875
    assert costo_regulatorio_de_facturas(facturas) == 55789875.0


import openpyxl
from apps.garantias.services.costo_regulatorio import extraer_facturas_xm


def _hoja_demo():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Facturas XM"
    filas = [
        ["Factura ASIC1 - COMERCIALIZADOR", None, None, None, None],
        ["campo", "cantidad", "last_value", "current_value", "total"],
        ["Servicios de administracion sic", 1, 0, 0, 800000.0],
        ["Valor total", 1, 0, 0, 800000.0],
        [None, None, None, None, None],
        ["Factura ASIC2 - GENERADOR", None, None, None, None],
        ["campo", "cantidad", "last_value", "current_value", "total"],
        ["Fazni", 1, 0, 0, 999626.0],
        ["Valor total", 1, 0, 0, 999626.0],
    ]
    for r in filas:
        ws.append(r)
    return ws


def test_extraer_facturas_separa_encabezado_tipo_y_lineas():
    facturas = extraer_facturas_xm(_hoja_demo())
    assert [f["asic"] for f in facturas] == ["ASIC1", "ASIC2"]
    assert [f["tipo"] for f in facturas] == ["COMERCIALIZADOR", "GENERADOR"]
    assert ("Fazni", 999626.0) in facturas[1]["lineas"]
    # la fila 'campo' (header) NO es una línea de concepto
    assert all(l[0] != "campo" for f in facturas for l in f["lineas"])


def test_extraer_y_calcular_da_solo_generador():
    from apps.garantias.services.costo_regulatorio import costo_regulatorio_de_facturas
    assert costo_regulatorio_de_facturas(extraer_facturas_xm(_hoja_demo())) == 999626.0


import os
import pytest
from apps.garantias.services.costo_regulatorio import costo_regulatorio_de_archivo

_ARCHIVO_JULIO = r"C:\Users\jessi\OneDrive\Documentos\Estado Resultados\2026\07_Julio\Cruce facturas 7 2026 txf.xlsx"


@pytest.mark.skipif(not os.path.exists(_ARCHIVO_JULIO),
                    reason="archivo Cruce facturas de julio no disponible (CI)")
def test_archivo_real_julio_reproduce_55_789_875():
    # Regla calibrada vs garantías XM: sin arranque/parada ni IVA (antes daba 67.191.598).
    assert costo_regulatorio_de_archivo(_ARCHIVO_JULIO) == 55789875.0


import io
import openpyxl
from apps.garantias.services.costo_regulatorio import costo_regulatorio_de_bytes


def _xlsx_bytes_demo():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Facturas XM"
    for r in [
        ["Factura ASIC2 - GENERADOR", None, None, None, None],
        ["campo", "cantidad", "last_value", "current_value", "total"],
        ["Fazni", 1, 0, 0, 999626.0],
        ["Valor total", 1, 0, 0, 999626.0],
    ]:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_costo_regulatorio_de_bytes_parsea_workbook_en_memoria():
    assert costo_regulatorio_de_bytes(_xlsx_bytes_demo()) == 999626.0
