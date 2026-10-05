"""Ingesta del costo regulatorio desde el Drive de ER. La selección de período/versión
es pura; la orquestación inyecta las funciones de Drive (sin red)."""
from apps.garantias.services.costo_regulatorio_drive import _rank_version, seleccionar_cruce


def _cruce(anio, mes, version, fid):
    return {"id": fid, "anio": anio, "mes": mes, "version": version}


def test_rank_version_txf_es_la_mas_alta_y_txN_por_numero():
    assert _rank_version("txf") > _rank_version("tx8")
    assert _rank_version("tx8") > _rank_version("tx3")
    assert _rank_version("???") < _rank_version("tx3")


def test_selecciona_periodo_exacto_prefiriendo_txf():
    cruces = [
        _cruce(2026, 7, "tx3", "a"),
        _cruce(2026, 7, "txf", "b"),
        _cruce(2026, 6, "txf", "c"),
    ]
    elegido = seleccionar_cruce(cruces, 2026, 7)
    assert elegido["id"] == "b"
    assert elegido["fallback"] is False


def test_fallback_al_ultimo_periodo_no_mayor_al_pedido():
    # Se pide agosto (aún no cerrado, no hay cruce) -> cae a julio, el último <= agosto.
    cruces = [
        _cruce(2026, 6, "txf", "jun"),
        _cruce(2026, 7, "txf", "jul"),
    ]
    elegido = seleccionar_cruce(cruces, 2026, 8)
    assert elegido["id"] == "jul"
    assert elegido["fallback"] is True
    assert (elegido["anio"], elegido["mes"]) == (2026, 7)


def test_sin_cruces_devuelve_none():
    assert seleccionar_cruce([], 2026, 8) is None


import io
import openpyxl
from apps.garantias.services.costo_regulatorio_drive import costo_regulatorio_del_mes


def _xlsx_generador_bytes(valor):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Facturas XM"
    for r in [
        ["Factura ASIC9 - GENERADOR", None, None, None, None],
        ["campo", "cantidad", "last_value", "current_value", "total"],
        ["Fazni", 1, 0, 0, float(valor)],
        ["Valor total", 1, 0, 0, float(valor)],
    ]:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def _listar_fake():
    # Simula lo que devuelve listar_carpeta ya "crudo": dicts con name/id/mimeType.
    return [
        {"id": "jul", "name": "Cruce facturas 7 2026 txf.xlsx", "mimeType": "x"},
        {"id": "jun", "name": "Cruce facturas 6 2026 txf.xlsx", "mimeType": "x"},
        {"id": "er1", "name": "Estado resultados Cliente Proyecto 7 2026.xlsx", "mimeType": "x"},
    ]


def test_del_mes_exacto_baja_y_parsea():
    bajados = {"jul": _xlsx_generador_bytes(500000), "jun": _xlsx_generador_bytes(111)}
    r = costo_regulatorio_del_mes(2026, 7, listar=_listar_fake, descargar=bajados.get)
    assert r["valor"] == 500000.0
    assert r["fallback"] is False
    assert (r["anio"], r["mes"]) == (2026, 7)


def test_del_mes_con_fallback_usa_ultimo_disponible():
    bajados = {"jul": _xlsx_generador_bytes(500000), "jun": _xlsx_generador_bytes(111)}
    r = costo_regulatorio_del_mes(2026, 8, listar=_listar_fake, descargar=bajados.get)
    assert r["valor"] == 500000.0    # julio, el último <= agosto
    assert r["fallback"] is True
    assert (r["anio"], r["mes"]) == (2026, 7)


def test_del_mes_sin_cruces_devuelve_none_valor():
    r = costo_regulatorio_del_mes(2026, 8, listar=lambda: [], descargar=lambda i: b"")
    assert r["valor"] is None
    assert r["cruce"] is None
