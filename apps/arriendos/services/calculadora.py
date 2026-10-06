"""
Cálculo de Arriendos — puro, sin DB ni FastAPI. Base mensual indexada por IPC
con convención DANE (ipc[año-1]), aplicada por AÑO CALENDARIO: el incremento se
aplica cada 1 de enero, usando solo el AÑO de fecha_firma_contrato (no el mes/día
de la firma). En enero del año Y se aplica ipc[Y-1].
Canon mostrado = siempre el calculado (o el congelado, si aplica).
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from apps.om.services.calculadora import corresponde_cobro_este_mes

MESES = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
         "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]


def _redondear(v: float) -> int:
    return int(Decimal(str(v)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calcular_arriendo(
    *,
    proyecto_id: int,
    nombre: str,
    codigo: str | None,
    fecha_firma_contrato: date | None,
    valor_base: float | None,
    periodo: str,
    ipc_tasas: dict[int, float],
    incluido: bool = True,
    facturado: bool = False,
    valor_congelado: int | None = None,
    periodicidad: str | None = None,
    anticipo_pagado_desde: date | None = None,
    anticipo_pagado_hasta: date | None = None,
) -> dict:
    año_periodo = int(periodo.split("-")[0])
    mes = int(periodo.split("-")[1])
    mes_label = MESES[mes - 1]

    def deshabilitada(historial: str) -> dict:
        return {
            "id": proyecto_id, "proyecto": nombre, "codigo": codigo,
            "periodo": periodo, "mes_año": f"{mes_label} {año_periodo}",
            "habilitado": False, "incluido": False, "facturado": facturado,
            "valor_base": valor_base, "n_indexaciones": 0, "factor_acumulado": 1.0,
            "valor_anual_indexado": None, "canon_calculado": None,
            "canon_a_facturar": None,
            "valor_facturado_congelado": int(valor_congelado) if valor_congelado is not None else None,
            "ipc_incompleto": False,
            "aplica_este_mes": True,
            "periodicidad": periodicidad,
            "historial_texto": historial, "historial_detalle": historial,
        }

    if not (valor_base and valor_base > 0):
        return deshabilitada("Sin valor base")
    # Base de indexación = fecha_firma_contrato (fecha de contrato), sin fallback
    # a otra fecha: Arriendos siempre indexa por la fecha de firma del contrato
    # (a diferencia de Mantenimiento, que usa una fecha de inicio distinta).
    fecha_base = fecha_firma_contrato
    if fecha_base is None:
        return deshabilitada("Sin fecha de contrato")

    año_firma = fecha_base.year
    aplica_este_mes = corresponde_cobro_este_mes(periodicidad, fecha_base, periodo)

    if anticipo_pagado_desde and anticipo_pagado_hasta:
        periodo_ym = (año_periodo, mes)
        desde_ym = (anticipo_pagado_desde.year, anticipo_pagado_desde.month)
        hasta_ym = (anticipo_pagado_hasta.year, anticipo_pagado_hasta.month)
        if desde_ym < periodo_ym <= hasta_ym:
            aplica_este_mes = False

    factor = 1.0
    n = 0
    pasos = []
    ipc_incompleto = False
    detalle = [f"Base {año_firma}: {valor_base}"]
    # Indexar cada 1-enero (año calendario), usando solo el año de fecha_firma_contrato.
    # Convención DANE: en enero del año Y se aplica ipc[Y-1].
    for añoC in range(año_firma + 1, año_periodo + 1):
        ipc = ipc_tasas.get(añoC - 1)
        if ipc is None:
            ipc_incompleto = True
            detalle.append(f"Ene {añoC}: IPC dic {añoC - 1} no disponible")
            break
        factor *= (1.0 + ipc)
        n += 1
        pasos.append(f"IPC dic {añoC - 1}: {ipc * 100:.2f}%")
        detalle.append(f"Ene {añoC} (IPC dic {añoC - 1}: {ipc * 100:.2f}%)")

    canon_calculado = _redondear(valor_base * factor)
    canon_a_facturar = canon_calculado
    if valor_congelado is not None:
        canon_a_facturar = int(valor_congelado)   # mes ya facturado → canon congelado

    return {
        "id": proyecto_id, "proyecto": nombre, "codigo": codigo,
        "periodo": periodo, "mes_año": f"{mes_label} {año_periodo}",
        "habilitado": True, "incluido": incluido, "facturado": facturado,
        "valor_base": valor_base, "n_indexaciones": n,
        "factor_acumulado": round(factor, 6),
        "valor_anual_indexado": _redondear(canon_calculado * 12),
        "canon_calculado": canon_calculado,
        "canon_a_facturar": canon_a_facturar,
        "valor_facturado_congelado": int(valor_congelado) if valor_congelado is not None else None,
        "ipc_incompleto": ipc_incompleto,
        "aplica_este_mes": aplica_este_mes,
        "periodicidad": periodicidad,
        "historial_texto": " → ".join(pasos) if pasos else f"Sin indexaciones (base: {valor_base})",
        "historial_detalle": "\n".join(detalle),
    }


def calcular_iva(canon_a_facturar: int | None, iva_pct: float) -> int | None:
    """IVA sobre el canon a facturar, al porcentaje que le toque al arrendador.

    **`iva_pct` es un PORCENTAJE, no un si/no.** Antes el parametro era el
    booleano `responsable_iva` y el 19% estaba escrito aca dentro; ahora la tasa
    la resuelve `apps/arriendos/services/iva.py::pct_de`, que la busca en la
    ficha del cliente vinculado y cae al 19% de siempre cuando no hay ninguna
    configurada. Pasar un booleano aca daria 1% en vez de 19%.
    """
    if not iva_pct or canon_a_facturar is None:
        return None
    return _redondear(canon_a_facturar * (float(iva_pct) / 100.0))


def serie_indexacion(
    fecha_base: date | None,
    valor_base: float | None,
    ipc_tasas: dict[int, float],
    año_hasta: int,
    mes_hasta: int,
) -> list[dict]:
    """Serie de indexación de arriendo por año calendario (1-enero), con la misma
    convención DANE que calcular_arriendo (ipc[año-1]). Solo se usa el AÑO de
    fecha_base, no el mes/día. valor_base es el canon MENSUAL base (misma
    semántica que calcular_arriendo). Lista vacía si falta la fecha o el valor."""
    if fecha_base is None or not valor_base or valor_base <= 0:
        return []

    año_firma = fecha_base.year
    filas = [{
        "anio": año_firma,
        "ipc_aplicado": None,
        "valor_mensual": _redondear(valor_base),
        "valor_anual": _redondear(valor_base * 12),
    }]
    factor = 1.0
    for añoC in range(año_firma + 1, año_hasta + 1):
        tasa = ipc_tasas.get(añoC - 1)
        ipc_pct = None
        if tasa is not None:
            factor *= (1.0 + tasa)
            ipc_pct = round(tasa * 100, 2)
        valor_mensual = valor_base * factor
        filas.append({
            "anio": añoC,
            "ipc_aplicado": ipc_pct,
            "valor_mensual": _redondear(valor_mensual),
            "valor_anual": _redondear(valor_mensual * 12),
        })
    return filas
