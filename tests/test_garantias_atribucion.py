"""Atribución de la garantía por contrato.

Solo generan garantía los contratos PLC (Pague lo Contratado: si no cubren su
mínimo, compran en bolsa) o los que tienen un duplicado (energía vendida que no
existe → toda va a compra en bolsa). Los PLG sin duplicado son ruido y quedan
fuera. El peso de cada contrato sale del resumen de Cumplimiento:
`compras_bolsa_mwh` (si es PLC) + `exposicion_bolsa_duplicados_mwh` (siempre).
"""

from apps.garantias.services.atribucion import (
    garantia_por_contrato, pesos_por_contrato_desde_resumen, repartir,
)


def test_repartir_distribuye_total_por_peso_de_deficit():
    filas = repartir({"C1": 0.6, "C2": 0.4}, total_garantia=1000.0)
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


# ── Pesos desde el resumen de Cumplimiento ─────────────────────────────────────

def _contrato(num, nombre, comprador, compras, dup, n_dup=0):
    return {
        "numero_codigo_contrato": num, "nombre_interno": nombre,
        "comprador_nombre": comprador, "compras_bolsa_mwh": compras,
        "exposicion_bolsa_duplicados_mwh": dup, "n_duplicados": n_dup,
    }


def test_pesos_plc_cuenta_compras_y_excluye_plg_sin_duplicado():
    contratos = [
        _contrato("UNERGY 008-2025", "Terpel 8", "TPLC", 324.0, None),   # PLC
        _contrato("OM-UNERGY-011", "Lumina", "LMEC", 69.6, None),        # PLG, ruido
    ]
    pesos, meta = pesos_por_contrato_desde_resumen(contratos, {"UNERGY 008-2025"})
    assert pesos == {"UNERGY 008-2025": 324.0}
    assert meta["UNERGY 008-2025"]["es_plc"] is True
    assert meta["UNERGY 008-2025"]["contrato"] == "Terpel 8"
    assert "OM-UNERGY-011" not in pesos  # Lumina fuera


def test_pesos_duplicado_cuenta_siempre_aunque_no_sea_plc():
    contratos = [
        _contrato("OMCE-SFE-002", "Santa Fe 2", "SFEC", 0.0, 118.1, n_dup=1),  # PLG con dup
    ]
    pesos, meta = pesos_por_contrato_desde_resumen(contratos, plc_numeros=set())
    assert pesos == {"OMCE-SFE-002": 118.1}
    assert meta["OMCE-SFE-002"]["es_duplicado"] is True
    assert meta["OMCE-SFE-002"]["es_plc"] is False


def test_pesos_plc_con_deficit_cero_no_entra():
    # BIA es PLC pero cumplió su mínimo mensual → compras 0 → no aparece (el déficit
    # horario se afinará aparte con los mínimos).
    contratos = [_contrato("EV-ENER-003-2025", "BIA Delta 1", "BIAC", 0.0, None)]
    pesos, _ = pesos_por_contrato_desde_resumen(contratos, {"EV-ENER-003-2025"})
    assert pesos == {}


def test_garantia_por_contrato_reparte_el_total_por_los_pesos():
    contratos = [
        _contrato("UNERGY 008-2025", "Terpel 8", "TPLC", 300.0, None),
        _contrato("OMCE-SFE-002", "Santa Fe 2", "SFEC", 0.0, 100.0, n_dup=1),
    ]
    filas = garantia_por_contrato(
        2026, 9, total_garantia=4000.0,
        resumen_fn=lambda y, m, incluir_todos=True: {"contratos": contratos},
        plc_fn=lambda: {"UNERGY 008-2025"},
    )
    d = {f["codigo"]: f for f in filas}
    assert d["UNERGY 008-2025"]["monto"] == 3000.0   # 300/400
    assert d["UNERGY 008-2025"]["contrato"] == "Terpel 8"
    assert d["UNERGY 008-2025"]["es_plc"] is True
    assert d["OMCE-SFE-002"]["monto"] == 1000.0       # 100/400
    assert d["OMCE-SFE-002"]["es_duplicado"] is True
