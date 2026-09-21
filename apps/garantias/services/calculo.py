"""Cálculo de las proyecciones de garantía (precobro XM). Parte PURA.

`proyecciones` recibe sus dependencias inyectadas (`calcular_balance_fn`,
`precio_fn`, `regulatorio_fn`) precisamente para poder probarse sin base ni red.
Lo que las cablea contra los servicios reales está en `proyecciones.py`.
"""

import calendar
from datetime import date, datetime, timedelta, timezone

# Colombia es UTC−5 sin horario de verano; el contenedor corre en UTC.
_COL_TZ = timezone(timedelta(hours=-5))

MWH_A_KWH = 1000.0
# Generación MENSUAL estimada de una planta nueva (kWh). Entra a la garantía como
# kWh × precio, prorrateada por la fracción de mes que cubre la ventana.
KWH_PLANTA_NUEVA_DEFAULT = 180_000.0


def hoy_col() -> date:
    """La fecha de hoy en Colombia, no la del servidor."""
    return datetime.now(_COL_TZ).date()


def calcular_garantia(neto_mwh: float, precio_cop_kwh: float, costo_regulatorio: float,
                      plantas_nuevas: int = 0,
                      kwh_planta_nueva: float = KWH_PLANTA_NUEVA_DEFAULT,
                      fraccion_periodo: float = 1.0) -> dict:
    """(ventas−compras)×precio + regulatorio, con override aditivo de plantas nuevas.

    `kwh_planta_nueva` es la generación MENSUAL de una planta nueva; `fraccion_periodo`
    es la fracción de mes que cubre la ventana (1.0 = mes completo). Así una planta
    nueva aporta su generación mensual × precio, prorrateada por los días de la ventana.
    Devuelve el total y sus componentes (para el snapshot/desglose)."""
    energia_neta_kwh = neto_mwh * MWH_A_KWH
    valor_energia = energia_neta_kwh * precio_cop_kwh
    valor_plantas_nuevas = plantas_nuevas * kwh_planta_nueva * fraccion_periodo * precio_cop_kwh
    return {
        "energia_neta_kwh": energia_neta_kwh,
        "valor_energia": valor_energia,
        "valor_plantas_nuevas": valor_plantas_nuevas,
        "costo_regulatorio": costo_regulatorio,
        "garantia_total": valor_energia + valor_plantas_nuevas + costo_regulatorio,
    }


def neto_de_balance(balance: dict, campo: str) -> float:
    """venta_bolsa − compra_bolsa_directa (UNGG) del campo dado ('proyectado' | 'total').
    'compras' = SOLO duplicados (compra_bolsa_directa), no el compra_bolsa_total."""
    ungg = balance["ungg"]
    venta = ungg["venta_bolsa"].get(campo, 0.0)
    compra_directa = ungg["compra_bolsa_directa"].get(campo, 0.0)
    return venta - compra_directa


def _mes_anterior(anio: int, mes: int) -> tuple[int, int]:
    return (anio - 1, 12) if mes == 1 else (anio, mes - 1)


def _mes_siguiente(anio: int, mes: int) -> tuple[int, int]:
    return (anio + 1, 1) if mes == 12 else (anio, mes + 1)


def proyecciones(hoy: date, *, calcular_balance_fn, precio_fn, regulatorio_fn,
                 plantas_nuevas: int = 0,
                 kwh_planta_nueva: float = KWH_PLANTA_NUEVA_DEFAULT) -> dict:
    """Las dos estimaciones al corte `hoy`. Cada ventana pide su balance a SU mes:
    resto del mes actual = campo 'proyectado' del mes actual; mes siguiente = campo
    'total' del balance (proyectado) del mes siguiente. Deps inyectadas."""
    anio_act, mes_act = hoy.year, hoy.month
    precio = precio_fn()
    a_prev, m_prev = _mes_anterior(anio_act, mes_act)
    a_sig, m_sig = _mes_siguiente(anio_act, mes_act)

    bal_actual = calcular_balance_fn(anio_act, mes_act)["balance"]
    bal_sig = calcular_balance_fn(a_sig, m_sig)["balance"]
    reg_actual = regulatorio_fn(a_prev, m_prev)
    reg_siguiente = regulatorio_fn(anio_act, mes_act)

    # Fracción de mes de cada ventana: el mes siguiente entra completo; el resto del
    # mes en curso, solo los días que faltan (una planta nueva aporta a prorrata).
    dias_mes_act = calendar.monthrange(anio_act, mes_act)[1]
    frac_resto = max(0, dias_mes_act - hoy.day) / dias_mes_act

    def ventana(clave, anio, mes, balance, campo, reg, fraccion):
        neto = neto_de_balance(balance, campo)
        calc = calcular_garantia(neto, precio, (reg or {}).get("valor") or 0.0,
                                 plantas_nuevas, kwh_planta_nueva, fraccion)
        return {"clave": clave, "anio": anio, "mes": mes, "neto_mwh": neto,
                "regulatorio_periodo": {"anio": (reg or {}).get("anio"),
                                        "mes": (reg or {}).get("mes"),
                                        "fallback": (reg or {}).get("fallback")},
                **calc}

    return {
        "fecha_corte": hoy.isoformat(),
        "precio_bolsa_cop_kwh": precio,
        "plantas_nuevas": plantas_nuevas,
        "kwh_planta_nueva": kwh_planta_nueva,
        "ventanas": [
            ventana("resto_mes_actual", anio_act, mes_act, bal_actual, "proyectado", reg_actual, frac_resto),
            ventana("mes_siguiente", a_sig, m_sig, bal_sig, "total", reg_siguiente, 1.0),
        ],
    }


def aplicar_pagado(resultado: dict, pagado_por_periodo: dict) -> dict:
    """Anexa `pagado` y `saldo` (pagado − garantia_total) a cada ventana. `pagado` es
    None si no hay dato para ese (anio, mes) → `saldo` None. Muta y devuelve el resultado."""
    for v in resultado.get("ventanas", []):
        pagado = pagado_por_periodo.get((v["anio"], v["mes"]))
        v["pagado"] = pagado
        v["saldo"] = None if pagado is None else pagado - (v.get("garantia_total") or 0.0)
    return resultado
