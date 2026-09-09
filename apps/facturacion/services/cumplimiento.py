"""Compromiso del PPA contra la energía realmente despachada.

Por contrato marco (PPA) se suma el despacho de TODOS sus contratos SIC y se
compara con el mínimo y el máximo del mes (`ppa_compromisos_energia`, en MWh).
El despacho viene en kWh, así que se convierte.

La energía sin PPA (bolsa / UNGC) no entra: no tiene contrato marco contra el
que comparar.

Cuando un PPA queda BAJO EL MÍNIMO se estima el **valor a indemnizar**: la
energía que faltó por entregar la tuvo que comprar el comprador en bolsa al
precio promedio del mes, más cara que el PPA. La indemnización es ese sobrecosto
(ver `_valor_indemnizar`).
"""

from apps.ppa import models as ppa_models

# Orden en que se muestran los estados: primero lo que hay que atender.
ORDEN_ESTADO = {
    "bajo_minimo": 0, "sobre_maximo": 1, "sin_compromiso": 2, "cumple": 3,
}

# Si el despachado y el mínimo difieren en más de este factor, casi seguro que
# alguien cargó kWh donde iban MWh (o al revés). Se marca en vez de callarlo.
FACTOR_SOSPECHA = 50


def _valor_indemnizar(faltante_kwh, precio_bolsa, tarifa_ppa):
    """Sobrecosto que asume el comprador por la energía que el PPA incumplió.

    La energía faltante la compra en bolsa al precio promedio del mes en vez de
    recibirla a la tarifa del PPA, así que paga de más `faltante × (bolsa − PPA)`.
    Todo en COP/kWh × kWh = COP. Solo cuenta cuando la bolsa fue MÁS cara que el
    PPA: si estuvo más barata, el comprador no se perjudicó y la indemnización es
    0 (se piso en 0). Devuelve `(valor_piso_0, bruto)`; el bruto conserva el signo
    para poder ver el caso en que la bolsa fue más barata. `(None, None)` si falta
    algún dato para calcular.
    """
    if not faltante_kwh or faltante_kwh <= 0 or precio_bolsa is None or tarifa_ppa is None:
        return None, None
    bruto = round(faltante_kwh * (precio_bolsa - tarifa_ppa), 2)
    return max(0.0, bruto), bruto


def build(datos_facturacion: dict, anio: int, mes: int, precio_bolsa: float | None = None) -> dict:
    compromisos = {
        c.contrato_id: (
            float(c.energia_minima) if c.energia_minima is not None else None,
            float(c.energia_maxima) if c.energia_maxima is not None else None,
        )
        for c in ppa_models.PpaCompromisoEnergia.objects.filter(
            **{"año": anio, "mes": mes}
        )
    }

    grupos: dict = {}
    for linea in datos_facturacion["lineas"]:
        ppa_id = linea.get("ppa_id")
        if not ppa_id:
            continue
        grupo = grupos.setdefault(ppa_id, {
            "ppa": linea["ppa"], "numero_contrato": linea["numero_contrato"],
            "compradores": set(), "proyectos": set(), "kwh": 0.0, "contratos": 0,
            "tarifa_ppa": None,
        })
        grupo["kwh"] += linea["kwh"]
        grupo["contratos"] += 1
        # La tarifa indexada es la misma para todo el PPA; tomo la primera no nula
        # (las líneas sin IPP del mes la traen en None).
        if grupo["tarifa_ppa"] is None and linea.get("tarifa_indexada") is not None:
            grupo["tarifa_ppa"] = linea["tarifa_indexada"]
        if linea["comprador"]:
            grupo["compradores"].add(linea["comprador"])
        if linea["proyecto"]:
            grupo["proyectos"].add(linea["proyecto"])

    filas, cumplen, por_debajo = [], 0, 0
    faltante_mwh = faltante_kwh = 0.0
    indemnizar_total = 0.0

    for ppa_id, grupo in grupos.items():
        minimo, maximo = compromisos.get(ppa_id, (None, None))
        if minimo is None:
            continue                     # solo los PPA con mínimo cargado
        despachado = round(grupo["kwh"] / 1000.0, 2)

        fila_faltante_kwh = 0.0
        if maximo and maximo > 0 and despachado > maximo:
            estado = "sobre_maximo"
        elif despachado >= minimo:
            estado = "cumple"
        else:
            estado = "bajo_minimo"
            faltante_mwh += minimo - despachado
            # El faltante en kWh se calcula exacto, no desde el MWh redondeado.
            fila_faltante_kwh = round(minimo * 1000.0 - grupo["kwh"], 2)
            faltante_kwh += minimo * 1000.0 - grupo["kwh"]

        if estado in ("cumple", "sobre_maximo"):
            cumplen += 1
        else:
            por_debajo += 1

        # Valor a indemnizar: solo aplica si quedó bajo el mínimo.
        tarifa_ppa = grupo["tarifa_ppa"]
        valor_indemnizar, valor_indemnizar_bruto = (
            _valor_indemnizar(fila_faltante_kwh, precio_bolsa, tarifa_ppa)
            if estado == "bajo_minimo" else (None, None)
        )
        if valor_indemnizar:
            indemnizar_total += valor_indemnizar

        filas.append({
            "ppa": grupo["ppa"],
            "numero_contrato": grupo["numero_contrato"],
            "comprador": ", ".join(sorted(grupo["compradores"])) or None,
            "proyecto": ", ".join(sorted(grupo["proyectos"])) or None,
            "contratos": grupo["contratos"],
            "minimo_mwh": minimo,
            "maximo_mwh": maximo,
            "despachado_mwh": despachado,
            "pct": round(despachado / minimo * 100, 1) if minimo else None,
            "diferencia_mwh": round(despachado - minimo, 2),
            "faltante_kwh": fila_faltante_kwh,
            "estado": estado,
            # Insumos y resultado del valor a indemnizar (COP). `valor_indemnizar`
            # va pisado en 0; el `_bruto` conserva el signo (bolsa < PPA → negativo).
            "tarifa_ppa_cop_kwh": tarifa_ppa,
            "precio_bolsa_cop_kwh": precio_bolsa,
            "valor_indemnizar_cop": valor_indemnizar,
            "valor_indemnizar_bruto_cop": valor_indemnizar_bruto,
            "unidad_sospechosa": bool(
                minimo > 0
                and (
                    despachado > minimo * FACTOR_SOSPECHA
                    or despachado < minimo / FACTOR_SOSPECHA
                )
            ),
        })

    filas.sort(
        key=lambda f: (
            ORDEN_ESTADO.get(f["estado"], 9), -(f["despachado_mwh"] or 0)
        )
    )
    return {
        "periodo": datos_facturacion["periodo"],
        "resumen": {
            "cumplen": cumplen,
            "bajo_minimo": por_debajo,
            "faltante_mwh": round(faltante_mwh, 1),
            "faltante_kwh": round(faltante_kwh, 2),
            "precio_bolsa_cop_kwh": precio_bolsa,
            "valor_indemnizar_total_cop": round(indemnizar_total, 2),
            "ppas": len(filas),
        },
        "filas": filas,
    }
