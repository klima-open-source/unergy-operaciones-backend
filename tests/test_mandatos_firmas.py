"""Detección de firmas en los PDFs de mandato.

Las coordenadas de abajo son las REALES, medidas sobre
CMU1287-Mandato-Costos-Minigranja Solar Joropo.pdf (firmado, 2026-08-13). El
documento no se versiona -- trae valores y nombres reales -- así que lo que se
conserva es su geometría, que es lo que el detector necesita.
"""
from apps.mandatos.services.firmas import (
    lineas_firmadas,
    resumir_firmas,
    verificar_firmas,
)


# Las dos líneas de firma del PDF real. Coordenadas MEDIDAS con pdfplumber, no
# estimadas -- una versión anterior de este plan traía x1 inventados (586 y 322
# en vez de 535 y 281) y los tests habrían pasado igual, validando geometría que
# no existe.
LINEAS_REALES = [
    {"x0": 390, "x1": 535, "top": 667},   # Revisor Fiscal
    {"x0": 159, "x1": 281, "top": 671},   # Representante Legal Suplente
]

MEMBRETE = {"x0": 122, "x1": 586, "top": 38}
PIE = {"x0": -6, "x1": 714, "top": 864}
FIRMA_IZQ = {"x0": 159, "x1": 282, "top": 638}
FIRMA_DER = {"x0": 397, "x1": 481, "top": 641}


def test_pdf_real_firmado_da_dos_de_dos():
    r = lineas_firmadas(LINEAS_REALES, [MEMBRETE, FIRMA_IZQ, FIRMA_DER, PIE])
    assert r == [True, True]


def test_membrete_y_pie_no_cuentan_como_firma():
    """Ambos se solapan horizontalmente con las líneas -- el pie ocupa el 102%
    del ancho. Solo la condición vertical los excluye. Si alguien relaja esa
    condición, este test lo atrapa."""
    assert lineas_firmadas(LINEAS_REALES, [MEMBRETE, PIE]) == [False, False]


def test_sin_imagenes_no_hay_firmas():
    assert lineas_firmadas(LINEAS_REALES, []) == [False, False]


def test_una_sola_firma():
    r = lineas_firmadas(LINEAS_REALES, [MEMBRETE, FIRMA_IZQ, PIE])
    assert r == [False, True]


def test_imagen_lejos_por_encima_no_cuenta():
    """Una imagen alineada pero muy arriba es otra cosa, no la firma."""
    lejana = {"x0": 159, "x1": 282, "top": 400}
    assert lineas_firmadas(LINEAS_REALES, [lejana]) == [False, False]


def test_imagen_debajo_de_la_linea_no_cuenta():
    debajo = {"x0": 159, "x1": 282, "top": 700}
    assert lineas_firmadas(LINEAS_REALES, [debajo]) == [False, False]


def test_imagen_sin_solape_horizontal_no_cuenta():
    corrida = {"x0": 600, "x1": 700, "top": 640}
    assert lineas_firmadas(LINEAS_REALES, [corrida]) == [False, False]


# ── resumir_firmas ────────────────────────────────────────────────────────────

def test_resumen_completo():
    assert resumir_firmas([True, True]) == {
        "lineas": 2, "firmadas": 2, "estado": "firmado_completo"}


def test_resumen_parcial():
    assert resumir_firmas([True, False]) == {
        "lineas": 2, "firmadas": 1, "estado": "parcial"}


def test_resumen_sin_firmas():
    assert resumir_firmas([False, False]) == {
        "lineas": 2, "firmadas": 0, "estado": "sin_firmas"}


def test_sin_lineas_es_no_verificable_no_sin_firmas():
    """Distinción crítica: si no se encontraron líneas de firma, el documento no
    es 'sin firmar' -- es que no se pudo mirar. Confundirlos haría que un PDF con
    otra plantilla se reporte como no firmado y dispare alarmas falsas, o peor,
    que se trate como concluido algo que nadie verificó."""
    assert resumir_firmas([]) == {
        "lineas": 0, "firmadas": 0, "estado": "no_verificable"}


# ── verificar_firmas ──────────────────────────────────────────────────────────

def test_verificar_firmas_con_bytes_invalidos_es_no_verificable():
    """Un adjunto corrupto o que no es PDF no debe reventar el cron ni, peor,
    reportarse como 'sin firmas' -- que se leería como un problema del documento
    en vez de un problema al leerlo."""
    r = verificar_firmas(b"esto no es un pdf")
    assert r["estado"] == "no_verificable"


def test_verificar_firmas_con_bytes_vacios():
    assert verificar_firmas(b"")["estado"] == "no_verificable"


def test_verificar_firmas_con_none():
    assert verificar_firmas(None)["estado"] == "no_verificable"


# ── tipo por contenido ────────────────────────────────────────────────────────
# Regla del usuario (2026-08-20): el mandato de INGRESOS trae la leyenda de
# exclusión de IVA al pie del cuadro de valores; el de costos no. Hace falta
# porque un lote de autoconsumo trae ingresos y costos con la MISMA convención
# de nombre, y el tipo es parte de la identidad única del mandato.

from apps.mandatos.services.firmas import tipo_por_contenido


def test_la_leyenda_de_iva_marca_ingreso():
    assert tipo_por_contenido(
        "Concepto Valor Ingreso Bruto (COP) $ 4,912,355 ... (**) LOS INGRESOS "
        "DE VENTA DE ENERGÍA SON EXCLUIDOS DE IVA SEGÚN EL NUMERAL 11 DEL "
        "ARTÍCULO 476 DEL E.T.") == "ingreso"


def test_la_leyenda_partida_en_dos_renglones_sigue_contando():
    """pdfplumber devuelve el texto con los saltos de línea del PDF, y la
    leyenda real viene partida ('... DEL ARTÍCULO\n476 DEL E.T.')."""
    assert tipo_por_contenido(
        "(**) LOS INGRESOS DE VENTA DE ENERGÍA SON EXCLUIDOS\nDE IVA SEGÚN EL "
        "NUMERAL 11 DEL ARTÍCULO\n476 DEL E.T.") == "ingreso"


def test_sin_la_leyenda_es_costo():
    """Los mandatos de costos no mencionan la palabra 'costo' en el texto
    -- verificado sobre los lotes reales -- así que la ausencia de la leyenda
    es el único discriminador disponible."""
    assert tipo_por_contenido(
        "CERTIFICACIÓN DEL REPRESENTANTE LEGAL Y REVISOR FISCAL SOBRE CONTRATO "
        "DE MANDATO. Concepto Valor a pagar (COP) $ 1,200,000") == "costo"


def test_texto_vacio_es_costo():
    assert tipo_por_contenido("") == "costo"
    assert tipo_por_contenido(None) == "costo"
