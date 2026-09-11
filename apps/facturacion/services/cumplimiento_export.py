"""Excel del cálculo de indemnización por incumplimiento, calcado del formato de
Compensación de la usuaria y **todo formulado** (auditable).

Tres hojas:
  - **Bolsa**: precio horario del mes (24 h/día) + PNBA = promedio de todas las
    horas (con techo opcional por hora), como fórmula.
  - **Despacho**: una fila por contrato SIC (proyecto) con su despacho diario y el
    total del mes (SUM).
  - **Resumen**: una fila por PPA bajo el mínimo con mínimo, despachado (SUMIF sobre
    Despacho), tarifa, precio de bolsa (=Bolsa!PNBA), diferencia, energía incumplida
    y valor a indemnizar — todas fórmulas que referencian las otras hojas.

`build_workbook` es puro (recibe los datos ya consultados) para poder testearlo sin
base ni red; la vista arma los datos y lo sirve como xlsx.
"""

from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FONT = "Arial"
_HDR = Font(name=FONT, bold=True, color="FFFFFF", size=10)
_HDR_FILL = PatternFill("solid", fgColor="1F4E78")
_BOLD = Font(name=FONT, bold=True, size=10)
_BASE = Font(name=FONT, size=10)
_TITLE = Font(name=FONT, bold=True, size=13, color="1F4E78")
_MONEY = '#,##0'
_PRICE = '#,##0.00'
_KWH = '#,##0.00'


def _hcell(ws, row, col, txt):
    c = ws.cell(row, col, txt)
    c.font = _HDR
    c.fill = _HDR_FILL
    c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    return c


def build_workbook(periodo, lineas, despacho_dia, compromisos, bolsa, *, techo=None):
    """Arma el workbook.

    - `periodo`: "YYYY-MM".
    - `lineas`: filas de `calculo.periodo` (por contrato SIC): contrato, proyecto,
      ppa, ppa_id, comprador, tarifa_indexada.
    - `despacho_dia`: {codigo_sic: {"YYYY-MM-DD": kwh}}.
    - `compromisos`: {ppa_id: energia_minima_mwh}.
    - `bolsa`: dict de `simem.bolsa_mensual` (precio_bolsa + detalle horario).
    """
    anio, mes = int(periodo[:4]), int(periodo[5:7])
    import calendar
    ndias = calendar.monthrange(anio, mes)[1]
    dias = [f"{anio}-{mes:02d}-{d:02d}" for d in range(1, ndias + 1)]

    wb = Workbook()

    # ═══ Hoja Bolsa ═══
    wsb = wb.active
    wsb.title = "Bolsa"
    wsb["A1"] = f"Precio de bolsa horario (SIMEM 709b84) · {periodo}"
    wsb["A1"].font = _TITLE
    r_techo = 2
    wsb.cell(r_techo, 1, "Techo ($/kWh):").font = _BOLD
    techo_cell = wsb.cell(r_techo, 2, techo if techo is not None else "")
    techo_cell.font = _BOLD
    techo_ref = f"$B${r_techo}"
    hdr = 4
    _hcell(wsb, hdr, 1, "Fecha")
    for h in range(24):
        _hcell(wsb, hdr, 2 + h, f"{h:02d}")
    _hcell(wsb, hdr, 26, "Prom. día")
    detalle = bolsa.get("detalle", {})
    first_data = hdr + 1
    for i, d in enumerate(dias):
        r = first_data + i
        wsb.cell(r, 1, d).font = _BASE
        horas = detalle.get(d, {})
        for h in range(24):
            v = horas.get(f"{h:02d}")
            cell = wsb.cell(r, 2 + h, v)
            cell.font = _BASE
            cell.number_format = _PRICE
        # promedio del día, techado si aplica: AVERAGE(min(hora,techo))
        rng = f"B{r}:Y{r}"
        if techo is not None:
            # no array formula: promedio simple; el techo real se aplica en PNBA abajo
            pass
        pc = wsb.cell(r, 26, f"=IFERROR(ROUND(AVERAGE({rng}),2),\"\")")
        pc.font = _BASE
        pc.number_format = _PRICE
    last_data = first_data + len(dias) - 1
    # PNBA del mes = promedio de TODAS las horas (techadas por hora si hay techo)
    pnba_row = last_data + 2
    wsb.cell(pnba_row, 1, "PNBA Promedio del mes").font = _BOLD
    all_rng = f"B{first_data}:Y{last_data}"
    if techo is not None and techo != "":
        # promedio de min(hora, techo) sin array: SUMPRODUCT
        n = f"COUNT({all_rng})"
        capped_sum = f"(SUMPRODUCT(({all_rng}<={techo_ref})*{all_rng})+{techo_ref}*SUMPRODUCT(--({all_rng}>{techo_ref})))"
        formula = f"=ROUND({capped_sum}/{n},2)"
    else:
        formula = f"=ROUND(AVERAGE({all_rng}),2)"
    pnba = wsb.cell(pnba_row, 2, formula)
    pnba.font = _BOLD
    pnba.number_format = _PRICE
    pnba_ref = f"Bolsa!$B${pnba_row}"
    wsb.column_dimensions["A"].width = 12
    for h in range(24):
        wsb.column_dimensions[get_column_letter(2 + h)].width = 8
    wsb.column_dimensions["Z"].width = 10
    wsb.freeze_panes = "B5"

    # ═══ Hoja Despacho (por proyecto/SIC) ═══
    wsd = wb.create_sheet("Despacho")
    wsd["A1"] = f"Despacho diario por contrato (proyecto) · {periodo} · kWh"
    wsd["A1"].font = _TITLE
    dhdr = 3
    cabeceras = ["Cód. SIC", "Proyecto", "Contrato (PPA)", "Comprador", "Total mes (kWh)"]
    for j, t in enumerate(cabeceras, 1):
        _hcell(wsd, dhdr, j, t)
    for i, d in enumerate(dias):
        _hcell(wsd, dhdr, 6 + i, d[8:10])  # día del mes
    # una fila por SIC (línea con ppa)
    sic_rows = {}
    r = dhdr + 1
    for ln in lineas:
        sic = ln.get("contrato")
        if not sic:
            continue
        wsd.cell(r, 1, sic).font = _BASE
        wsd.cell(r, 2, ln.get("proyecto") or "").font = _BASE
        wsd.cell(r, 3, ln.get("ppa") or "").font = _BASE
        wsd.cell(r, 4, ln.get("comprador") or "").font = _BASE
        dd = despacho_dia.get(sic, {})
        for i, d in enumerate(dias):
            v = dd.get(d)
            c = wsd.cell(r, 6 + i, v)
            c.font = _BASE
            c.number_format = _KWH
        first_col = get_column_letter(6)
        last_col = get_column_letter(6 + len(dias) - 1)
        tc = wsd.cell(r, 5, f"=SUM({first_col}{r}:{last_col}{r})")
        tc.font = _BOLD
        tc.number_format = _KWH
        sic_rows.setdefault(ln.get("ppa_id"), []).append(r)
        r += 1
    last_sic_row = r - 1
    wsd.column_dimensions["A"].width = 10
    wsd.column_dimensions["B"].width = 26
    wsd.column_dimensions["C"].width = 20
    wsd.column_dimensions["D"].width = 10
    wsd.column_dimensions["E"].width = 15
    wsd.freeze_panes = "F4"

    # ═══ Hoja Resumen (formulada) ═══
    wsr = wb.create_sheet("Resumen", 0)  # primera
    wsr["A1"] = f"Valor a indemnizar por incumplimiento · {periodo}"
    wsr["A1"].font = _TITLE
    wsr["A2"] = ("Descuento = Energía incumplida × (Precio de bolsa − Tarifa PPA), "
                 "piso en 0. Bolsa = SIMEM 709b84 (TXF).")
    wsr["A2"].font = Font(name=FONT, italic=True, size=9, color="808080")
    rhdr = 4
    cols_r = ["Contrato (PPA)", "Proyectos", "Mínimo (kWh)", "Despachado (kWh)",
              "Tarifa PPA ($/kWh)", "Precio bolsa ($/kWh)", "Diferencia ($/kWh)",
              "Energía incumplida (kWh)", "Valor a indemnizar (COP)"]
    for j, t in enumerate(cols_r, 1):
        _hcell(wsr, rhdr, j, t)

    # agrupar líneas por PPA
    por_ppa = {}
    for ln in lineas:
        pid = ln.get("ppa_id")
        if not pid:
            continue
        g = por_ppa.setdefault(pid, {"ppa": ln.get("ppa"), "tarifa": None, "proyectos": set()})
        if g["tarifa"] is None and ln.get("tarifa_indexada") is not None:
            g["tarifa"] = ln["tarifa_indexada"]
        if ln.get("proyecto"):
            g["proyectos"].add(ln["proyecto"])

    rr = rhdr + 1
    total_row_refs = []
    for pid, g in por_ppa.items():
        minimo_mwh = compromisos.get(pid)
        if minimo_mwh is None:
            continue
        minimo_kwh = float(minimo_mwh) * 1000.0
        wsr.cell(rr, 1, g["ppa"] or "—").font = _BASE
        wsr.cell(rr, 2, ", ".join(sorted(g["proyectos"])) or "—").font = _BASE
        mn = wsr.cell(rr, 3, round(minimo_kwh, 2)); mn.font = _BASE; mn.number_format = _KWH
        # Despachado: SUMIF sobre la hoja Despacho (col C = PPA, col E = total)
        ppa_txt = (g["ppa"] or "—").replace('"', '""')
        desp = wsr.cell(rr, 4, f'=SUMIF(Despacho!$C${dhdr+1}:$C${last_sic_row},"{ppa_txt}",Despacho!$E${dhdr+1}:$E${last_sic_row})')
        desp.font = _BASE; desp.number_format = _KWH
        tar = wsr.cell(rr, 5, g["tarifa"]); tar.font = _BASE; tar.number_format = _PRICE
        bol = wsr.cell(rr, 6, f"={pnba_ref}"); bol.font = _BASE; bol.number_format = _PRICE
        dif = wsr.cell(rr, 7, f"=F{rr}-E{rr}"); dif.font = _BASE; dif.number_format = _PRICE
        inc = wsr.cell(rr, 8, f"=MAX(0,C{rr}-D{rr})"); inc.font = _BASE; inc.number_format = _KWH
        val = wsr.cell(rr, 9, f"=MAX(0,H{rr}*G{rr})"); val.font = _BOLD; val.number_format = _MONEY
        total_row_refs.append(rr)
        rr += 1

    if total_row_refs:
        wsr.cell(rr, 8, "TOTAL").font = _BOLD
        wsr.cell(rr, 8).alignment = Alignment(horizontal="right")
        t = wsr.cell(rr, 9, f"=SUM(I{rhdr+1}:I{rr-1})")
        t.font = _BOLD; t.number_format = _MONEY
    widths_r = [22, 34, 15, 16, 15, 15, 15, 18, 20]
    for j, w in enumerate(widths_r, 1):
        wsr.column_dimensions[get_column_letter(j)].width = w
    wsr.freeze_panes = "A5"

    return wb
