"""Helpers compartidos por los módulos del pipeline de reporte de energía."""
from __future__ import annotations

import random

import pandas as pd

HORAS = list(range(24))

# Tolerancia para aceptar curva_medidor_respaldo como dato real al reportar
# (en vez de la estimación ±1%) -- ver curva_respaldo_a_reportar(). Definida
# a partir del histórico real (auditoría 2026-08-25): con ambos medidores
# completos y curva_final del medidor principal, el respaldo o coincide casi
# exacto (58% de los días con <=0.5% de error total) o está claramente mal
# (26% con >90% de error, medidor desconectado/descalibrado) -- casi no hay
# término medio. 1.5 kWh de diferencia en el TOTAL DIARIO de generación
# (arriba o abajo, no por hora) es el rango que confirmó el equipo de campo
# -- 41/140 días del histórico pasan este criterio.
TOLERANCIA_RESPALDO_REAL_KWH = 1.5

# Ventanas horarias para el relleno horario centralizado (ver
# reconectador.rellenar_horas_faltantes) -- fuera de estas horas la
# generación real esperada es ~0, así que rellenar ahí no aporta nada y solo
# arriesga meter ruido de telemetría nocturna como si fuera dato real.
HORAS_SOLARES = range(6, 18)        # 6am a 6pm -- Solenium×FP e histórico
HORAS_RECONECTADOR = range(7, 17)   # 7am a 5pm -- más angosta: es la primera
                                     # fuente que se intenta y la menos
                                     # verificable (dato físico crudo, sin
                                     # cruzarlo contra nada más en ese momento)

CURVA_CERO: pd.Series = pd.Series({h: 0.0 for h in HORAS}, dtype=float)
CURVA_VACIA: pd.Series = pd.Series({h: None for h in HORAS}, dtype=float)  # sin dato -- no confundir con "generó 0"

# Un panel solar puede superar brevemente su capacidad nominal (irradiancia
# alta + temperatura baja), pero no por un margen enorme -- un multiplicador
# generoso (3x) sigue descartando lecturas físicamente imposibles sin
# arriesgar falsos positivos sobre picos reales. Ver MGS 0033 Sabana de
# Torres 2026-08-21: el reconectador reportó ~235.000 kWh en una sola hora
# para una frontera de 0,99 MW -- ~237x su capacidad, un error de escala de
# unidades del dispositivo, no generación real. Mismo criterio reusado para
# SolarView /generation/ (ver MGS 0010 Villanueva 2026-08-26: un valor de
# ~48.090 kWh en una hora para una frontera de 0,99 MW -- ~48x su capacidad
# -- se colaba en e_inv/curva_solenium_referencia, inflando la escala del
# eje Y del gráfico y contaminando la comparación medidor-vs-inversores que
# decide Caso 3).
MULTIPLICADOR_MAX_PLAUSIBLE = 3.0


def limite_plausible_kwh(capacidad_efectiva_mw: float | None) -> float | None:
    if capacidad_efectiva_mw is None or capacidad_efectiva_mw <= 0:
        return None
    return capacidad_efectiva_mw * 1000 * MULTIPLICADOR_MAX_PLAUSIBLE


def escalar_curva(curva: pd.Series, total_objetivo: float) -> pd.Series:
    """Escala una curva horaria al total objetivo manteniendo la distribución."""
    total_actual = curva.fillna(0).sum()
    if total_actual == 0:
        return CURVA_CERO.copy()
    factor = total_objetivo / total_actual
    return (curva.fillna(0) * factor).round(4)


def escalar_curva_con_huecos(curva: pd.Series, total_objetivo: float) -> pd.Series:
    """Igual que escalar_curva, pero preserva NaN en las horas sin dato en vez
    de taparlas con 0 -- para que sigan siendo candidatas al relleno horario
    centralizado (reconectador -> Solenium -> histórico)."""
    total_actual = curva.fillna(0).sum()
    if total_actual == 0:
        return curva.where(curva.isna(), 0.0)
    factor = total_objetivo / total_actual
    return (curva * factor).round(4)


def curva_a_lista(curva: pd.Series | None) -> list[float | None] | None:
    """Serializa una pd.Series[0..23] a una lista JSON-friendly (para JSONB)."""
    if curva is None:
        return None
    return [None if pd.isna(curva.get(h)) else round(float(curva[h]), 4) for h in HORAS]


def lista_a_curva(valores: list[float | None] | None) -> pd.Series:
    """Deserializa una lista JSONB de 24 valores a pd.Series[0..23]."""
    if not valores:
        return CURVA_VACIA.copy()
    return pd.Series({h: valores[h] if h < len(valores) else None for h in HORAS}, dtype=float)


def rellenar_con_otro_medidor(
    curva: pd.Series, medidor_usado: str | None,
    curva_principal: list[float | None] | None, curva_respaldo: list[float | None] | None,
) -> tuple[pd.Series, set[int]]:
    """Rellena los huecos de `curva` con el OTRO medidor (el que NO ganó
    como medidor_usado), para las mismas horas -- mismo consumo/generación
    física, otro canal de lectura (ver MGS 0021 Ibirico Consumo
    2026-08-11: medidor respaldo usado sin dato a las 4h, pero principal sí
    la tenía). No es una estimación como histórico/Solenium/reconectador --
    es dato real de un medidor, así que es la PRIMERA fuente a intentar en
    la acción manual 'Rellenar horas' (Generación y Consumo), antes de
    reconectador/Solenium×FP/histórico.

    Retorna (curva_rellenada, horas_rellenadas)."""
    if not curva.isna().any():
        return curva, set()
    mu = medidor_usado or ""
    if mu.startswith("principal"):
        otra = curva_respaldo
    elif mu.startswith("respaldo"):
        otra = curva_principal
    else:
        otra = None
    if otra is None:
        return curva, set()

    curva_otro = lista_a_curva(otra)
    curva = curva.copy()
    horas: set[int] = set()
    for h in list(curva[curva.isna()].index):
        valor = curva_otro.get(h)
        if pd.notna(valor):
            curva[h] = valor
            horas.add(h)
    return curva, horas


def curva_respaldo_a_reportar(rep, curva_medidor_respaldo: list | None = None) -> tuple[list[float], str]:
    """Qué se reporta como 'Backup' a Quoia -- dato real cuando existe y es
    confiable, si no la estimación ±1% de siempre. Usado tanto por
    _enviar_a_quoia() (reporte_energia.py) como por _construir_detalle()
    para que el frontend muestre exactamente lo que se va a enviar, antes
    de enviarlo.

    `curva_medidor_respaldo` (opcional): override explícito -- si se pasa,
    se usa esto en vez de `rep.curva_medidor_respaldo` para el chequeo del
    paso 2, SIN persistirlo en el objeto. Pensado para editar_curva()
    (reporte_energia.py): alimentar la decisión con un valor en vivo más
    fresco que el snapshot guardado, sin pisar ese snapshot -- pisarlo
    apagaría el aviso "el medidor cambió" para un medidor que la persona
    nunca revisó explícitamente (ver MGS GD La Hormiguita 2026-08-26).

    Prioridad:
    1. curva_respaldo_terceros (FRONTERAS_TERCEROS, ej. Cedillanos) -- dato
       real que trae el Excel del tercero, tal cual (sin cambios).
    2. curva_medidor_respaldo -- SOLO si curva_final vino del medidor
       principal (medidor_usado empieza con 'principal') o del reporte CGM
       ya validado (medidor_usado == 'cgm', Caso 1 -- ampliado 2026-08-26
       por consistencia visual: el medidor de nodo se sigue leyendo en
       pasivo incluso en Caso 1, así que hay el mismo dato con qué
       comparar), y el TOTAL DIARIO de generación del respaldo está a
       TOLERANCIA_RESPALDO_REAL_KWH o menos de diferencia (arriba o abajo)
       del que se va a reportar como Principal/CGM. Si se aleja más, no se
       usa -- no es dato confiable (medidor descalibrado o desconectado),
       se cae al paso 3 igual que siempre. No se exige que el respaldo
       esté 100% completo (decidido 2026-08-26, ver MGS 0025 El Copey
       Occidente: respaldo con huecos en horas nocturnas de generación
       ~0, descartado igual aunque coincidía casi exacto con el
       principal) -- la tolerancia de 1.5 kWh en el TOTAL ya protege
       sola: un hueco en una hora con generación real infla la diferencia
       mucho más allá de la tolerancia (las horas sin dato cuentan como 0
       en la suma), así que solo pasan huecos fisicamente irrelevantes
       (de noche).
    3. Estimación ±1% sobre curva_final (comportamiento de siempre).

    Consumo (extendido 2026-08-26) tiene acceso a los pasos 2 y 3 igual
    que Generación -- solo el paso 1 (terceros) no aplica, no tiene
    curva_respaldo_terceros.

    Retorna (curva, origen) -- origen es 'terceros' | 'medidor' | 'estimado',
    para que el frontend pueda distinguir dato real de estimado."""
    curva_final = rep.curva_final or [0.0] * 24
    principal_readings = [float(v) if v is not None else 0.0 for v in curva_final]

    respaldo_terceros = getattr(rep, "curva_respaldo_terceros", None)
    if respaldo_terceros:
        return [float(v) if v is not None else 0.0 for v in respaldo_terceros], "terceros"

    mu = rep.medidor_usado or ""
    if curva_medidor_respaldo is None:
        curva_medidor_respaldo = getattr(rep, "curva_medidor_respaldo", None)
    if (mu.startswith("principal") or mu == "cgm") and curva_medidor_respaldo:
        respaldo_medidor = [float(v) if v is not None else 0.0 for v in curva_medidor_respaldo]
        dif_total = abs(sum(respaldo_medidor) - sum(principal_readings))
        if dif_total <= TOLERANCIA_RESPALDO_REAL_KWH:
            return respaldo_medidor, "medidor"

    estimado = [round(v * (1 + random.uniform(-0.01, 0.01)), 4) for v in principal_readings]
    return estimado, "estimado"


def curva_cambio(persistida: list | None, viva: list | None, tolerancia: float = 0.01) -> bool | None:
    """True si la curva en vivo difiere de la persistida por más del 1% del
    total del día -- señal de que Quoia corrigió algo desde que se
    clasificó (ver MGS 0032 El Paso Norte 2026-08-05: medidor doblado al
    momento de clasificar, ya corregido para cuando se revisó). None si no
    hay curva persistida con qué comparar (fila anterior a este fix).

    Compartida por _construir_detalle() (reporte_energia.py, comparación al
    abrir el detalle) y verificar_drift_medidores() (drift_medidores.py,
    mismo chequeo corrido en lote justo después de clasificar)."""
    if persistida is None or viva is None:
        return None
    total_p = sum(v for v in persistida if v is not None)
    total_v = sum(v for v in viva if v is not None)
    base = max(abs(total_p), abs(total_v))
    if base == 0:
        return False
    return abs(total_p - total_v) / base > tolerancia


def actualizar_respaldo_final(rep, curva_medidor_respaldo: list | None = None) -> None:
    """Recalcula y persiste curva_respaldo_final/respaldo_final_origen en
    `rep` -- llamar cada vez que curva_final/medidor_usado (o las curvas de
    medidor que alimentan la comparación) se terminan de fijar: clasificar
    (orquestador._upsert_generacion/_upsert_consumo), edición manual
    (editar_curva) y Excel de terceros (aplicar_excel_terceros, solo
    Generación). ReporteEnergiaGeneracion y ReporteEnergiaConsumo tienen
    ambas columnas (extendido a Consumo 2026-08-26).

    `curva_medidor_respaldo` (opcional): ver curva_respaldo_a_reportar()."""
    curva, origen = curva_respaldo_a_reportar(rep, curva_medidor_respaldo)
    rep.curva_respaldo_final = curva
    rep.respaldo_final_origen = origen


def reporte_ya_valido(rep, es_generacion: bool) -> bool:
    """Si esta fila NO debe reportarse: Quoia ya tiene el dato bueno, o la
    frontera esta excluida.

    Vive aca porque la comparten los DOS caminos por los que sale una matriz
    -- `/enviar` (envio._enviar_a_quoia) y el Excel del dia (excel.py) -- y
    tenerla duplicada ya los dejo divergir: el Excel no miraba 'excluida' y le
    escribia 24 horas de 0,0 a una frontera excluida, justo la curva fabricada
    que el chequeo de `/enviar` existe para evitar. Cualquier criterio nuevo
    de "no reportar" va aca, no en una copia.

    Los campos que decide mirar son distintos por tipo, y no es un descuido:
    al adoptar el CGM a mano, editar_curva() fija `medidor_usado='cgm'` y
    `caso='CGM'`/1 juntos, pero el clasificador automatico de Generacion deja
    `medidor_usado='cgm'` con un `caso` numerico, y el de Consumo deja
    `caso='CGM'`. Mirar el campo equivocado en cada arbol sobreescribiria en
    Quoia su propio reporte con una copia nuestra.

    'excluida' -- `curva_final` es None mientras dura la exclusion (ver
    orquestador._exclusion_activa), asi que sin este chequeo se mandaria una
    curva de 0 kWh a una frontera que justamente no debe reportar nada
    mientras se resuelve lo que la excluyo.
    """
    if rep.medidor_usado == "excluida":
        return True
    return rep.medidor_usado == "cgm" if es_generacion else str(rep.caso) == "CGM"
