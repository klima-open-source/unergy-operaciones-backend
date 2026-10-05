"""Lectura del medidor de frontera para la vista de Generación Solar.

De los dos medidores de la frontera (principal y respaldo) se muestra el que
tenga el día más completo -- ver elegir_medidor().

Se piden dos variables al nodo, por el mismo método que usa el pipeline del
ASIC (`GaiaClient.get_node_measurements`):

  · `ap`  -- potencia activa. Es la curva del día.
  · `eae` -- energía activa exportada. Sumada, es el total del día.

Reemplaza a `GaiaClient.get_node_electrical_snapshot()` en este uso, que pedía
las 8 familias de variables del nodo (v, c, ap, rp, pf, eae, iae, ere) -- y
como la tarjeta lo hacía para principal Y respaldo, eran hasta 16 llamadas
externas por proyecto para dibujar dos líneas.

Solo se corrigen dos cosas de los datos crudos, ambas verificadas contra
Quoia el 2026-09-03 y ambas necesarias:

  · **El signo.** Hay medidores que reportan la generación en negativo (nodo
    1731: -721,7 kW a las 09:45). Es polaridad de CT invertida en sitio, no
    consumo.
  · **La unidad.** Unos nodos entregan vatios y otros kilovatios, y conviven:
    de 6 nodos con dato ese día, 603 y 883 rondaban 1.050.000 (vatios) y 1731
    marcaba 726,6 (kilovatios). No hay campo que lo declare, así que se deduce
    contra la capacidad instalada de la planta.

    No es un hallazgo nuevo: reporte_energia/datos_crudos.py ya lo documenta
    como "un error CRÓNICO de escala de unidad (ej. Polaris 1/2, ~1.150x, un
    nodo que reporta en W en vez de kW)". La diferencia es qué se hace con
    esas lecturas. El pipeline las DESCARTA -- para el ASIC un valor mal
    escalado es inservible y conviene caer a otra fuente. Acá se CONVIERTEN,
    porque esta vista no reporta a nadie y descartarlas dejaría la gráfica de
    medidores vacía para la mitad de los nodos.

Lo que a propósito NO se hace, y por qué:

  · **No se rellenan los huecos.** El compuesto reconstruye potencia a partir
    de los deltas del contador cuando `ap` lleva rato sin reportar, y la
    dibuja igual que una medición real. Eso no protege ningún número -- la
    energía del día sale del contador, no de integrar la curva -- así que
    solo maquilla la gráfica, y en una vista de tiempo real inventa un
    detalle que no existe. Acá el hueco se ve.
  · **No se filtran las filas `recovered`.** El pipeline del ASIC sí lo hace,
    pero porque integra la curva por Riemann y un cero sintético le distorsiona
    la energía. Acá solo se dibujan puntos, así que esa razón no aplica.
"""
from __future__ import annotations

_FASES_AP = ("app1", "app2", "app3")
_FASES_EAE = ("eaepd1", "eaepd2", "eaepd3")

# Cuánto puede superar una lectura a la capacidad de su propia planta antes de
# que la única explicación sea que viene en vatios. Un medidor no entrega 10x
# lo que la planta puede producir.
_FACTOR_VATIOS = 10.0

# Sin capacidad instalada con qué comparar: para una minigranja, un valor así
# solo tiene sentido en vatios.
_TECHO_KW_SIN_CAPACIDAD = 5000.0


def _suma(fila: dict, fases: tuple[str, ...]) -> float:
    return sum(float(fila.get(f) or 0) for f in fases)


def divisor_a_kw(maximo: float, capacidad_kwp: float | None) -> float:
    """1000 si las lecturas vienen en vatios, 1 si ya vienen en kilovatios.

    Quoia no declara la unidad de la potencia activa y los nodos no coinciden
    entre si: verificado el 2026-09-03, los nodos 603 y 883 rondaban 1.050.000
    (vatios) mientras 1731 marcaba 726,6 (kilovatios). Se deduce comparando
    contra la capacidad instalada, que es un dato propio y confiable -- un
    medidor no entrega 10x lo que su planta puede producir.

    Publica a proposito: es el unico lugar donde vive este criterio. Antes
    estaba escrito tambien en gaia_client (heuristica de "si pasa de 5000 es
    W", que divide por error una planta de mas de 5 MW) y faltaba por completo
    en informe_om, que exponia ap_total crudo como si fuera kW.
    """
    techo = capacidad_kwp * _FACTOR_VATIOS if capacidad_kwp else _TECHO_KW_SIN_CAPACIDAD
    return 1000.0 if maximo > techo else 1.0


def snapshot_medidor(
    gaia,
    node_id: int | None,
    fecha_str: str,
    capacidad_efectiva_mw: float | None = None,
) -> dict | None:
    """Potencia y energía del día de un nodo. None si no hay nodo.

    Si el nodo existe pero no reportó, devuelve la estructura con la curva
    vacía: "el medidor no reportó" es un dato, distinto de "no hay medidor".
    """
    if not gaia or not node_id:
        return None

    filas_ap = gaia.get_node_measurements(node_id, fecha_str, "ap") or []
    filas_eae = gaia.get_node_measurements(node_id, fecha_str, "eae") or []

    crudos = [(f["time"], abs(_suma(f, _FASES_AP))) for f in filas_ap if f.get("time")]
    divisor = divisor_a_kw(
        max((kw for _, kw in crudos), default=0.0),
        (capacidad_efectiva_mw * 1000) if capacidad_efectiva_mw else None,
    )

    curva = sorted(
        ({"time": t, "kw": round(kw / divisor, 3)} for t, kw in crudos),
        key=lambda p: p["time"],
    )
    energia = sum(_suma(f, _FASES_EAE) for f in filas_eae) if filas_eae else None
    # El contador se reporta en intervalos mas largos que la potencia, asi que
    # el acumulado puede ir hasta media hora por detras del numero de "ahora".
    # Se expone hasta cuando cubre para poder decirlo, en vez de dar a entender
    # que es del ultimo instante.
    energia_hasta = max((f["time"] for f in filas_eae if f.get("time")), default=None)

    return {
        "node_id": node_id,
        "energia_kwh": round(energia, 3) if energia is not None else None,
        "energia_hasta": energia_hasta,
        "curva": curva,
    }


def _horas_cubiertas(snap: dict | None, con_generacion: bool = True) -> int:
    """Horas distintas del dia con lectura de potencia.

    Se cuentan HORAS y no puntos porque los dos medidores pueden reportar a
    intervalos distintos, y ahi comparar cantidades no diria nada.

    `con_generacion` exige ademas que la lectura sea > 0. Hay medidores que
    pasan el dia entero reportando 0,0 kW en el canal de potencia mientras su
    contador de energia si acumula -- verificado el 2026-09-03 en el nodo 1693:
    89 filas de `ap` TODAS en cero, contra 47 filas de `eae` con valor real
    (11 kWh a las 06:15, 19,8 a las 06:30...). Fisicamente no se acumula
    energia sin potencia, asi que ese canal esta roto y devuelve ceros en vez
    de no devolver nada.

    Sin esta condicion, un medidor pegado en cero "cubre" mas horas que uno con
    lecturas reales pero dispersas, y le gana la eleccion.
    """
    if not snap:
        return 0
    puntos = snap.get("curva") or []
    if con_generacion:
        puntos = [p for p in puntos if p.get("kw")]
    return len({p["time"][11:13] for p in puntos})


def elegir_medidor(principal: dict | None, respaldo: dict | None) -> tuple[dict | None, str | None]:
    """El medidor con el dia mas completo -- horas con generacion real, y si
    ninguno genero, horas con cualquier lectura. En empate, el principal.

    La decision vive SOLO aca: el frontend la recibe resuelta. Antes el mismo
    criterio estaba escrito tambien en SolarLiveView.vue y podian
    desincronizarse en silencio -- la grafica mostrando un medidor y el resto
    de la tarjeta hablando de otro.

    "Completitud" NO es "mayor valor", que es lo que el clasificador de
    Consumo tuvo que descartar en julio. Al contrario: son criterios opuestos.
    Un hueco de telemetria acumula horas sin reportar en un solo pico
    artificial, asi que con "mayor valor" el medidor averiado GANABA
    (Chiriguana Norte 1); contando horas cubiertas, ese mismo hueco lo hace
    PERDER, que es lo que uno espera.

    El empate se rompe hacia el principal porque es el medidor de
    liquidacion: sin una razon para preferir el otro, ese.
    """
    if not principal and not respaldo:
        return None, None

    # Primero por horas con generacion real; si ninguno genero (de noche, o los
    # dos con el canal de potencia caido), por horas con cualquier lectura.
    for con_generacion in (True, False):
        hp = _horas_cubiertas(principal, con_generacion)
        hr = _horas_cubiertas(respaldo, con_generacion)
        if hp or hr:
            return (respaldo, "respaldo") if hr > hp else (principal, "principal")

    # Ninguno trajo curva. Puede que igual haya contador: las dos variables se
    # caen por separado, y el acumulado es el numero principal de la tarjeta.
    if principal and principal.get("energia_kwh") is not None:
        return principal, "principal"
    if respaldo and respaldo.get("energia_kwh") is not None:
        return respaldo, "respaldo"

    # Los dos mudos: se devuelve el principal igual, para poder decir "no
    # reporto" en vez de "no hay medidor".
    return (principal, "principal") if principal else (respaldo, "respaldo")
