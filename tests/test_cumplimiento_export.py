"""El workbook de indemnización: estructura y fórmulas (sin base ni red)."""
from apps.facturacion.services import cumplimiento_export as export


def _datos():
    lineas = [
        {"contrato": "111", "proyecto": "Planta A", "ppa": "Terpel 4", "ppa_id": 6,
         "comprador": "TPLC", "tarifa_indexada": 338.71},
        {"contrato": "222", "proyecto": "Planta B", "ppa": "Terpel 4", "ppa_id": 6,
         "comprador": "TPLC", "tarifa_indexada": 338.71},
    ]
    despacho_dia = {
        "111": {"2026-08-01": 1000.0, "2026-08-02": 1200.0},
        "222": {"2026-08-01": 500.0},
    }
    compromisos = {6: 487.2}  # MWh -> 487200 kWh
    bolsa = {"precio_bolsa": 942.32, "detalle": {
        "2026-08-01": {f"{h:02d}": 900.0 for h in range(24)},
        "2026-08-02": {f"{h:02d}": 984.64 for h in range(24)},
    }}
    return lineas, despacho_dia, compromisos, bolsa


def test_workbook_tiene_tres_hojas_y_formulas():
    lineas, dd, comp, bolsa = _datos()
    wb = export.build_workbook("2026-08", lineas, dd, comp, bolsa)
    assert wb.sheetnames == ["Resumen", "Bolsa", "Despacho"]

    wsr = wb["Resumen"]
    # fila de datos del PPA (rhdr=4 -> primera fila 5)
    # Diferencia = bolsa - tarifa ; Incumplida = max(0, min - desp) ; Valor = max(0, inc*dif)
    assert wsr["G5"].value == "=F5-E5"
    assert wsr["H5"].value == "=MAX(0,C5-D5)"
    assert wsr["I5"].value == "=MAX(0,H5*G5)"
    assert wsr["F5"].value.startswith("=Bolsa!")
    assert wsr["D5"].value.startswith("=SUMIF(Despacho!")
    assert wsr["C5"].value == 487200.0          # mínimo en kWh

    wsd = wb["Despacho"]
    # total del SIC 111 = SUM de sus columnas diarias
    assert wsd["E4"].value.startswith("=SUM(")

    wsb = wb["Bolsa"]
    # PNBA = ROUND(AVERAGE(todas las horas),2)
    pnba = [c for col in wsb.iter_cols() for c in col
            if isinstance(c.value, str) and c.value.startswith("=ROUND(AVERAGE(")]
    assert pnba, "falta la fórmula PNBA"


def test_workbook_se_guarda(tmp_path):
    lineas, dd, comp, bolsa = _datos()
    wb = export.build_workbook("2026-08", lineas, dd, comp, bolsa)
    out = tmp_path / "x.xlsx"
    wb.save(out)
    assert out.exists() and out.stat().st_size > 0
