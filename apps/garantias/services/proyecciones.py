"""Proyecciones de garantía: lo que toca la base y las fuentes externas.

El cálculo puro está en `calculo.py`; acá se cablean sus dependencias y se
persisten los resultados.

El balance de energía ya NO abre una sesión de SQLAlchemy propia: se portó con
`cumplimiento` y vive en
`apps/mercado_xm/services/cumplimiento/balance_energia.py`. Con eso desapareció
la última escotilla a SQLAlchemy que quedaba bajo `apps/`.
"""

import calendar
from datetime import date, timedelta

from apps.garantias import models as ga_models
from apps.garantias.services.calculo import (
    KWH_PLANTA_NUEVA_DEFAULT, aplicar_pagado, calcular_garantia, hoy_col,
    proyecciones,
)

DIAS_PRECIO_BOLSA = 25
LIMITE_HISTORIAL = 200


def _balance(anio: int, mes: int, corte: date | None = None) -> dict:
    """El balance del período, real o proyectado según sea pasado o futuro.

    `corte` fija la fecha "hoy" del cálculo: para el corte de hoy es None (usa el
    reloj), pero al replicar un corte pasado se pasa esa fecha y el balance usa la
    generación real HASTA ese día, no hasta hoy.
    """
    from apps.mercado_xm.services.cumplimiento.balance_energia import (
        calcular_balance, calcular_balance_proyectado,
    )

    # incluir_todos=True: la garantía es la exposición de TODA la empresa ante XM, así
    # que NO se aplica el filtro de "responsables ocultos" de Cumplimiento (p. ej.
    # "Externo"). XM cobra esos contratos igual; excluirlos subcontaría la garantía.
    hoy = corte or hoy_col()
    if (anio, mes) > (hoy.year, hoy.month):
        return calcular_balance_proyectado(anio, mes, hoy=hoy, incluir_todos=True)
    return calcular_balance(anio, mes, hoy=hoy, incluir_todos=True)


def _precio_bolsa(hasta: date | None = None) -> float | None:
    """Promedio de bolsa de los 7 días conocidos HASTA `hasta` (default: hoy).

    Al replicar un corte pasado se pasa esa fecha y el precio es el de ese
    viernes, no el de hoy.
    """
    from app.services.simem_bolsa import precio_bolsa_prom_7d

    hasta = hasta or date.today()
    inicio = hasta - timedelta(days=DIAS_PRECIO_BOLSA)
    return precio_bolsa_prom_7d(inicio.isoformat(), hasta.isoformat())


def _regulatorio(anio: int, mes: int) -> dict:
    from app.services.costo_regulatorio_drive import costo_regulatorio_del_mes

    return costo_regulatorio_del_mes(anio, mes)


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def guardar_balcttos(anio: int, mes: int, *, dia_corte: int, neto_mwh: float):
    fila, _ = ga_models.BalcttosNeto.objects.update_or_create(
        anio=anio, mes=mes,
        defaults={"dia_corte": dia_corte, "neto_mwh": neto_mwh},
    )
    return fila


def balcttos_de(anio: int, mes: int) -> dict | None:
    fila = ga_models.BalcttosNeto.objects.filter(anio=anio, mes=mes).first()
    if fila is None:
        return None
    return {"dia_corte": fila.dia_corte, "neto_mwh": float(fila.neto_mwh)}


def neto_de_ventana(anio: int, mes: int, dias_objetivo: int) -> float | None:
    """Neto de la ventana proyectando la tasa diaria REAL del BalCttos.

    `None` si no hay BalCttos del período: entonces manda la proyección del
    balance.
    """
    from app.services.balcttos import proyectar_neto_mwh

    dato = balcttos_de(anio, mes)
    if dato is None or not dato["dia_corte"]:
        return None
    return proyectar_neto_mwh(
        dato["neto_mwh"], dato["dia_corte"], dias_objetivo
    )


def pagado_por_periodo() -> dict:
    return {
        (p.anio, p.mes): float(p.valor)
        for p in ga_models.GarantiaPagado.objects.all()
    }


def set_pagado(anio: int, mes: int, valor: float):
    fila, _ = ga_models.GarantiaPagado.objects.update_or_create(
        anio=anio, mes=mes, defaults={"valor": valor}
    )
    return fila


def historial(limite: int = LIMITE_HISTORIAL):
    return ga_models.GarantiaSnapshot.objects.order_by(
        "-fecha_corte", "-id"
    )[:limite]


def guardar_snapshot(resultado: dict) -> list:
    """Persiste una fila por ventana del resultado."""
    corte = date.fromisoformat(resultado["fecha_corte"])
    precio = resultado.get("precio_bolsa_cop_kwh")
    filas = []
    for ventana in resultado["ventanas"]:
        # El período del costo regulatorio puede NO ser el de la ventana: si no
        # hay dato del mes, se usa el del anterior y se marca `fallback`.
        regulatorio = ventana.get("regulatorio_periodo") or {}
        filas.append(ga_models.GarantiaSnapshot(
            fecha_corte=corte, clave=ventana["clave"],
            anio=ventana["anio"], mes=ventana["mes"],
            neto_mwh=ventana.get("neto_mwh"), precio_bolsa=precio,
            valor_energia=ventana.get("valor_energia"),
            valor_plantas_nuevas=ventana.get("valor_plantas_nuevas"),
            costo_regulatorio=ventana.get("costo_regulatorio"),
            garantia_total=ventana.get("garantia_total"),
            plantas_nuevas=resultado.get("plantas_nuevas", 0),
            kwh_planta_nueva=resultado.get("kwh_planta_nueva"),
            regulatorio_anio=regulatorio.get("anio"),
            regulatorio_mes=regulatorio.get("mes"),
            regulatorio_fallback=bool(regulatorio.get("fallback")),
        ))
    ga_models.GarantiaSnapshot.objects.bulk_create(filas)
    return filas


# ---------------------------------------------------------------------------
# Cálculo en vivo
# ---------------------------------------------------------------------------

def en_vivo(hoy: date | None = None, *, plantas_nuevas: int = 0,
            kwh_planta_nueva: float = KWH_PLANTA_NUEVA_DEFAULT) -> dict:
    """Las dos ventanas de garantía al corte de hoy, sin guardar nada.

    **El BalCttos manda sobre la proyección.** Si hay uno guardado para el
    período de una ventana, su neto real —proyectado a la tasa diaria— sustituye
    al del balance y la garantía se recalcula; la ventana queda marcada con
    `fuente_neto='balcttos'`. Si no lo hay, queda `'proyeccion'`.
    """
    hoy = hoy or hoy_col()
    resultado = proyecciones(
        hoy,
        # El corte se enhebra al balance y al precio para poder REPLICAR un corte
        # pasado (mismo precio 7d y misma generación que ese día), no solo el de hoy.
        calcular_balance_fn=lambda anio, mes: _balance(anio, mes, corte=hoy),
        precio_fn=lambda: _precio_bolsa(hasta=hoy),
        regulatorio_fn=_regulatorio,
        plantas_nuevas=plantas_nuevas,
        kwh_planta_nueva=kwh_planta_nueva,
    )
    precio = resultado.get("precio_bolsa_cop_kwh")

    for ventana in resultado["ventanas"]:
        dias_del_mes = calendar.monthrange(ventana["anio"], ventana["mes"])[1]
        # En el mes en curso la ventana es lo que FALTA; en el siguiente, el mes
        # entero.
        dias = (
            dias_del_mes - hoy.day
            if ventana["clave"] == "resto_mes_actual" else dias_del_mes
        )
        neto = neto_de_ventana(ventana["anio"], ventana["mes"], dias)
        if neto is None:
            ventana["fuente_neto"] = "proyeccion"
            continue

        # Se recalcula con el neto real, conservando plantas nuevas y
        # regulatorio.
        ventana.update(calcular_garantia(
            neto, precio or 0.0, ventana.get("costo_regulatorio") or 0.0,
            plantas_nuevas, kwh_planta_nueva,
        ))
        ventana["neto_mwh"] = neto
        ventana["fuente_neto"] = "balcttos"

    return aplicar_pagado(resultado, pagado_por_periodo())


def ingerir_balcttos(anio: int, mes: int, contenido: bytes) -> dict:
    """Parsea el archivo BalCttos y guarda el neto real del período."""
    from app.services.balcttos import neto_compras_bolsa_de_bytes

    parseado = neto_compras_bolsa_de_bytes(contenido)
    dias = sorted(parseado["por_dia"])
    # El día de corte sale del último día con dato del archivo.
    dia_corte = int(dias[-1][8:10]) if dias else 0
    guardar_balcttos(
        anio, mes, dia_corte=dia_corte, neto_mwh=parseado["total_mwh"]
    )
    return {
        "anio": anio, "mes": mes, "dia_corte": dia_corte,
        "neto_mwh": parseado["total_mwh"],
    }
