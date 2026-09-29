"""Lo que la vista de Reporte de Energía muestra: resumen, histórico y listado.

Puerto de la parte de lectura de `app/api/v1/reporte_energia.py`.

**La agrupación de fuente NO es el vocabulario crudo** de `medidor_usado`/`caso`:
esos tienen demasiadas variantes técnicas para leerse como KPI (decidido con la
usuaria el 2026-08-21). Tres separaciones que importan y son fáciles de perder:

- **"Sin fuente" no es "Estimación"**: son casos donde no se pudo usar NADA y
  quedan pendientes de revisar, no un dato sustituto real.
- **"Apagado" tampoco es "Estimación"**: es un estado CONFIRMADO —el proyecto no
  está generando—, no un dato que se tuvo que adivinar.
- **"crudos" cuenta como estimación, no como inversor**: sale del nodo del
  medidor en Quoia (telemetría cruda integrada con Riemann) y tiene problemas de
  precisión documentados (San Pelayo ~14 % por debajo del medidor; Polaris 1/2
  ~1 150× por un error de escala).

"Excluida" no se cuenta: ese día no se reportó a propósito, no es una fuente.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import logging
from concurrent.futures import ThreadPoolExecutor

from rest_framework.exceptions import NotFound

from api.exceptions import NoProcesable
from apps.energia.services.reporte import curvas, historial
from apps.energia.services.reporte.utils import (
    curva_a_lista, curva_cambio, curva_respaldo_a_reportar,
)

# `ponytail: el cliente de Quoia sigue en app/services/mgs/`.
from app.services.mgs.gaia_client import GaiaClient

# Las respuestas de este modulo son dicts planos, NO modelos Pydantic.
#
# DRF no serializa un modelo Pydantic como objeto: lo RECORRE, y como un
# BaseModel itera dando pares (clave, valor), el cuerpo sale como
# `[[["etiqueta","CGM"],["total",7]]]` en vez de `[{"etiqueta":"CGM","total":7}]`.
# El frontend leia `d.total` -> undefined, y el Resumen mostraba
# "NaN dias-frontera" con las barras vacias (2026-09-05).
#
# La forma de la respuesta es la misma que documentan DistribucionFuenteItem,
# DesgloseFuenteItem y DetalleFuenteFronteraItem en app/schemas/reporte_energia.py
# (contrato de FastAPI); lo que se quita es la dependencia, no el contrato.

logger = logging.getLogger("operaciones.reporte_energia")
from apps.energia.models import ReporteEnergiaConsumo, ReporteEnergiaGeneracion
from apps.fronteras.models import Frontera


_NOMBRES_CORREGIDOS: dict[int, str] = {
    6: "MINIGRANJA SOLAR BARAYA SERV AUX",  # BD: "MINIGRANJA SOLAR BRAYA SERV AUX"
}


def _nombre_frontera(front: Frontera) -> str:
    return _NOMBRES_CORREGIDOS.get(front.id, front.nombre_frontera)


def _semaforo(caso, revisar: bool) -> str:
    if revisar:
        return "critical"
    if str(caso) in ("1", "CGM"):
        return "success"
    return "warning"


_GRUPO_FUENTE_GENERACION = {
    # CGM aparte de "Medidor" (pedido 2026-09-14). Es el dato oficial del
    # mercado, no una lectura nuestra: agrupado con los medidores, el resumen no
    # dejaba ver cuanto del reporte se sostiene en el CGM y cuanto en lo que
    # leemos nosotros, que es la pregunta que se le hace a este grafico.
    "cgm": "cgm",
    "principal": "medidor", "respaldo": "medidor",
    "principal_sin_cgm": "medidor", "respaldo_sin_cgm": "medidor",
    "principal_sin_historico": "medidor", "respaldo_sin_historico": "medidor",
    # El reconectador SI es equipo nuestro, asi que se queda en "Medidor".
    "reconectador": "medidor",
    # Estos dos no los medimos nosotros ni pasan por el mercado: son un Excel
    # que manda un tercero y un dato que reporta otra empresa. Contarlos como
    # "Medidor" hacia que un proyecto sin UNA sola lectura propia apareciera al
    # 100% en medidor -- el caso que destapo esto fue Complejo Industrial
    # Cedillanos: 27 dias de Excel de terceros y 3 de otra empresa, y el
    # resumen decia "Medidor 100%".
    "excel_terceros": "terceros", "externo": "terceros",
    "inversores": "inversor", "solenium_power": "inversor",
    # "crudos"/"crudos_parcial" NO son lectura de inversores -- salen del
    # nodo del medidor en Quoia (telemetría cruda "ap", integrada con
    # Riemann), con problemas de precisión documentados (San Pelayo ~14%
    # por debajo del medidor; Polaris 1/2, ~1.150x por error de escala de
    # unidades) -- son una reconstrucción con incertidumbre real, igual
    # que histórico/relleno horario (pedido 2026-08-21).
    "crudos": "estimacion", "crudos_parcial": "estimacion",
    "historico": "estimacion", "historico_vecino": "estimacion",
    "editado_manualmente": "estimacion", "relleno_horario": "estimacion",
    # "Apagado" es un estado CONFIRMADO (el proyecto no está generando),
    # no una estimación de un dato faltante -- categoría propia, distinta
    # de Estimación (pedido 2026-08-21: se veía como si "apagado" fuera lo
    # mismo que "no sabemos y adivinamos").
    "ninguno": "apagado",
    "revisar": "sin_fuente",
}


_GRUPO_FUENTE_CONSUMO = {
    "cgm": "cgm", "medidor": "medidor",
    "histórico": "estimacion",
    "sin dato": "sin_fuente", "error": "sin_fuente",
}


_ETIQUETA_GRUPO_FUENTE = {
    "cgm": "CGM", "medidor": "Medidor", "inversor": "Inversor",
    "terceros": "Reportado por terceros", "estimacion": "Estimación",
    "apagado": "Apagado", "sin_fuente": "Sin fuente", "otro": "Otro",
}


# De mayor a menor respaldo del dato: el CGM es oficial del mercado; el medidor
# y el inversor los leemos nosotros; lo de terceros es una medicion real pero
# que no podemos verificar; la estimacion es inferida.
#
# **Ya NO es el orden de las barras.** Desde el 2026-09-16 se dibujan de mayor a
# menor porcentaje, que es como se comparan tamanos de un vistazo (pedido de la
# usuaria). Este orden queda como desempate --dos grupos con el mismo conteo
# salen en el orden de confianza-- y como el orden de los colores, que son los
# que siguen diciendo que es cada cosa.
_ORDEN_GRUPO_FUENTE = [
    "cgm", "medidor", "inversor", "terceros", "estimacion", "apagado",
    "sin_fuente", "otro",
]


# Un día con MENOS de esta fracción de los reportes habituales del rango no fue
# una corrida: fue una corrida que no ocurrió. El umbral cae en un hueco enorme
# --los días normales traen 136-144 filas y los rotos 1-- así que dónde se ponga
# exactamente no cambia ningún resultado.
COBERTURA_MINIMA_DE_UNA_CORRIDA = 0.5

# Las dos mitades --generación y consumo-- se mueven juntas: un día normal trae
# 69 y 67, o 52 y 51. Si se separan, el clasificador hizo media corrida. El
# 2026-09-04 hizo las 69 de generación y dejó 19 de consumo sin clasificar
# (48 de 67, un equilibrio de 0,70), y ese día marcó 97% de CGM contra el ~35%
# habitual.
#
# **Es una PROPORCIÓN entre las dos mitades, no un mínimo contra la mediana del
# rango.** Las fronteras se van sumando --52 en agosto, 73 en septiembre-- así
# que cualquier umbral contra la mediana marcaría los días viejos como
# parciales. La proporción entre mitades no se entera del crecimiento: estuvo
# entre 0,96 y 0,98 todos los días buenos del rango medido.
EQUILIBRIO_MINIMO_ENTRE_MITADES = 0.85

MOTIVO_SIN_CORRIDA = "sin_corrida"
MOTIVO_PARCIAL = "corrida_parcial"
MOTIVO_SIN_CGM = "clasificacion_fallida"


def _clasificadas_y_cgm_por_dia(desde: date, hasta: date):
    """Una sola pasada de generación+consumo: qué fronteras se clasificaron
    cada día, cuántas por mitad, y cuáles usaron CGM -- lo que necesita
    `_motivos_del_rango` para juzgar cada día.
    """
    con_cgm: dict[date, set[int]] = defaultdict(set)
    clasificadas: dict[date, set[int]] = defaultdict(set)
    por_mitad: dict[str, dict[date, int]] = {
        "gen": defaultdict(int), "con": defaultdict(int),
    }
    for modelo, campo, mitad in ((ReporteEnergiaGeneracion, "medidor_usado", "gen"),
                                 (ReporteEnergiaConsumo, "caso", "con")):
        # `.lower()` a propósito: generación guarda "cgm" y consumo "CGM".
        for fila in (modelo.objects.filter(fecha__range=(desde, hasta))
                     .values("frontera_id", "fecha", campo)):
            fid = fila["frontera_id"]
            clasificadas[fila["fecha"]].add(fid)
            por_mitad[mitad][fila["fecha"]] += 1
            if (fila[campo] or "").strip().lower() == "cgm":
                con_cgm[fila["fecha"]].add(fid)
    return clasificadas, por_mitad, con_cgm


def _motivos_del_rango(
    desde: date, hasta: date,
    clasificadas: dict[date, set[int]], por_mitad: dict[str, dict[date, int]],
    con_cgm: dict[date, set[int]],
) -> dict[date, str | None]:
    """El motivo (o `None`) por el que un día no cuenta para la tasa de
    automático: los días en que el clasificador no hizo su trabajo. Son tres
    casos, y cada uno se detecta por una regla -- ninguno es una fecha escrita
    en el código, así que el mismo fallo dentro de seis meses también sale solo.
    Se evalúan en orden y la primera manda.

        `sin_corrida`           el día no trajo ni la mitad del volumen normal.
                                El 5 y 6 de septiembre de 2026 quedó UNA fila de
                                145: fue la migración del servidor.
        `corrida_parcial`       las dos mitades se separaron. El 4 de
                                septiembre hizo toda la generación y dejó 19
                                fronteras de consumo sin clasificar.
        `clasificacion_fallida` el día corrió entero y no usó CGM en NINGUNA
                                frontera. Eso no pasa nunca (confirmado con la
                                usuaria el 2026-09-16, sobre el 9 de agosto):
                                un cero absoluto es el programa fallando, no una
                                jornada sin automatización.

    Contarlos movería la métrica por causas que no son la automatización --una
    migración, media corrida, un fallo del clasificador-- y no habría forma de
    saber cuál.

    Se evalúa SIEMPRE sobre el día completo, con todas las fronteras: las tres
    reglas describen la corrida, no una frontera. En UNA frontera un día sin
    CGM es lo más normal del mundo; entre 145 no pasa nunca.
    """
    dias_del_rango = []
    dia = desde
    while dia <= hasta:
        dias_del_rango.append(dia)
        dia += timedelta(days=1)

    tipico = _mediana([len(clasificadas.get(d, ())) for d in dias_del_rango])
    # La regla del equilibrio solo tiene sentido si el rango trae las DOS
    # mitades: un rango con solo generación no tiene contra qué compararse.
    hay_las_dos = any(por_mitad["gen"].values()) and any(por_mitad["con"].values())

    motivos: dict[date, str | None] = {}
    for dia in dias_del_rango:
        corrio = bool(clasificadas.get(dia)
                      and len(clasificadas[dia]) >= tipico * COBERTURA_MINIMA_DE_UNA_CORRIDA)
        if not corrio:
            motivos[dia] = MOTIVO_SIN_CORRIDA
        elif hay_las_dos and _equilibrio(
            por_mitad["gen"][dia], por_mitad["con"][dia]
        ) < EQUILIBRIO_MINIMO_ENTRE_MITADES:
            motivos[dia] = MOTIVO_PARCIAL
        elif not con_cgm.get(dia):
            motivos[dia] = MOTIVO_SIN_CGM
        else:
            motivos[dia] = None
    return motivos


def _equilibrio(generacion: int, consumo: int) -> float:
    """Qué tan parejas quedaron las dos mitades de la corrida, de 0 a 1.

    1 es idéntico. Un día normal ronda 0,97 (69 y 67); el 2026-09-04 dio 0,70.
    """
    mayor = max(generacion, consumo)
    return (min(generacion, consumo) / mayor) if mayor else 1.0


def _mediana(valores: list[int]) -> float:
    """Mediana simple. Cero si no hay con qué."""
    limpios = sorted(valores)
    if not limpios:
        return 0.0
    mitad = len(limpios) // 2
    if len(limpios) % 2:
        return float(limpios[mitad])
    return (limpios[mitad - 1] + limpios[mitad]) / 2


def resumen(fecha: date) -> dict:
    """Los cuatro contadores del encabezado: total, a revisar, corregido
    automático y confiado.

    `puede_enviar` exige CERO filas marcadas para revisar: el envío a Quoia es
    del día completo, no por frontera.
    """
    gen = ReporteEnergiaGeneracion.objects.filter(fecha=fecha).values_list(
        "caso", "revisar_manualmente")
    con = ReporteEnergiaConsumo.objects.filter(fecha=fecha).values_list(
        "caso", "revisar_manualmente")

    filas = list(gen) + list(con)
    total = len(filas)
    revisar = sum(1 for _, r in filas if r)
    confiado = sum(1 for c, r in filas if not r and str(c) in ("1", "CGM"))
    corregido = total - revisar - confiado

    return {
        "fecha": fecha, "total": total, "revisar": revisar,
        "corregido_automatico": corregido, "confiado": confiado,
        "puede_enviar": (revisar == 0 and total > 0),
    }


def _ventana_anterior(desde: date, hasta: date) -> tuple[date, date]:
    """La ventana inmediatamente anterior, de la MISMA duración que `[desde,
    hasta]` -- lo que necesita `resumen_ventana` para "mejoró/empeoró"."""
    dias = (hasta - desde).days + 1
    return desde - timedelta(days=dias), desde - timedelta(days=1)


def _ventana_vacia() -> dict:
    return {
        "automaticos": 0, "no_automaticos": 0,
        "fechas_excluidas": [], "fuentes": {}, "dias": [],
    }


def _agregar_ventana_por_frontera(desde: date, hasta: date) -> list[dict]:
    """Una fila por frontera+tipo, con la ventana actual `[desde, hasta]` y la
    anterior de igual duración ya comparadas.

    La fuente se agrupa con `_GRUPO_FUENTE_GENERACION`/`_GRUPO_FUENTE_CONSUMO`,
    y un día de corrida rota (`_motivos_del_rango`) queda excluido para TODAS
    las fronteras por igual.

    **`nunca_clasificado`** no se decide contra un catálogo de qué frontera
    "debería" tener fila (`Frontera.tipo_frontera` no alcanza para eso: una
    frontera `consumo_propio` nunca va a tener fila en generación, y no es un
    error). Se decide por CONTINUIDAD: si tuvo fila en la ventana anterior y
    CERO en la actual, dejó de aparecer -- así pasaron desapercibidas las 9
    fronteras que se borraron el 2026-09-15. Una fila sin historial en NINGUNA de las
    dos ventanas simplemente no aparece en el resultado: no es un dato nuestro.
    """
    previo_desde, previo_hasta = _ventana_anterior(desde, hasta)
    rango_desde, rango_hasta = previo_desde, hasta

    clasificadas, por_mitad, con_cgm = _clasificadas_y_cgm_por_dia(rango_desde, rango_hasta)
    motivos = _motivos_del_rango(rango_desde, rango_hasta, clasificadas, por_mitad, con_cgm)

    acumulado: dict[tuple[int, str], dict] = {}

    for modelo, campo, tipo, mapa_fuente in (
        (ReporteEnergiaGeneracion, "medidor_usado", "generacion", _GRUPO_FUENTE_GENERACION),
        (ReporteEnergiaConsumo, "caso", "consumo", _GRUPO_FUENTE_CONSUMO),
    ):
        filas = (
            modelo.objects.filter(fecha__range=(rango_desde, rango_hasta))
            .values("frontera_id", "frontera__nombre_frontera", "frontera__codigo_frontera",
                    "fecha", campo)
        )
        for f in filas:
            fid = f["frontera_id"]
            clave = (fid, tipo)
            info = acumulado.setdefault(clave, {
                "nombre_proyecto": _NOMBRES_CORREGIDOS.get(fid, f["frontera__nombre_frontera"]),
                "codigo_frontera": f["frontera__codigo_frontera"],
                "actual": _ventana_vacia(), "previo": _ventana_vacia(),
            })
            v = info["actual"] if f["fecha"] >= desde else info["previo"]

            crudo = (f[campo] or "").strip().lower()
            # "excluida" es la exclusión A MANO de esta fila puntual (una
            # `ReporteEnergiaExclusion` resuelta) -- distinta del motivo de
            # `_motivos_del_rango`, que es la corrida ENTERA fallando. Las dos
            # cuentan como excluido.
            excluido = motivos[f["fecha"]] is not None or crudo == "excluida"
            if excluido:
                if v is info["actual"]:
                    v["fechas_excluidas"].append(f["fecha"])
                v["dias"].append({
                    "fecha": f["fecha"], "automatico": False, "excluido": True,
                    "grupo_fuente": None, "etiqueta_fuente": None,
                })
                continue

            es_automatico = crudo == "cgm"
            grupo = etiqueta = None
            if es_automatico:
                v["automaticos"] += 1
            else:
                grupo = mapa_fuente.get(crudo, "otro") if crudo else "sin_fuente"
                etiqueta = _ETIQUETA_GRUPO_FUENTE.get(grupo, "Otro")
                v["no_automaticos"] += 1
                v["fuentes"][grupo] = v["fuentes"].get(grupo, 0) + 1
            v["dias"].append({
                "fecha": f["fecha"], "automatico": es_automatico, "excluido": False,
                "grupo_fuente": grupo, "etiqueta_fuente": etiqueta,
            })

    filas_resultado = []
    for (fid, tipo), info in acumulado.items():
        act, prev = info["actual"], info["previo"]
        total_act = act["automaticos"] + act["no_automaticos"]
        total_prev = prev["automaticos"] + prev["no_automaticos"]
        nunca_clasificado = not act["dias"] and bool(prev["dias"])
        tasa = round(act["automaticos"] / total_act * 100, 1) if total_act else 0.0

        dominante = dominante_etiqueta = None
        desglose = []
        if act["fuentes"]:
            orden = sorted(
                act["fuentes"].items(),
                key=lambda kv: (-kv[1], _ORDEN_GRUPO_FUENTE.index(kv[0]) if kv[0] in _ORDEN_GRUPO_FUENTE else 99),
            )
            dominante = orden[0][0]
            dominante_etiqueta = _ETIQUETA_GRUPO_FUENTE.get(dominante, "Otro")
            desglose = [
                {"grupo": g, "etiqueta": _ETIQUETA_GRUPO_FUENTE.get(g, "Otro"), "dias": n}
                for g, n in orden
            ]

        filas_resultado.append({
            "frontera_id": fid, "tipo": tipo,
            "nombre_proyecto": info["nombre_proyecto"], "codigo_frontera": info["codigo_frontera"],
            "dias_automaticos": act["automaticos"], "dias_no_automaticos": act["no_automaticos"],
            "tasa": tasa, "nunca_clasificado": nunca_clasificado,
            "fuente_dominante": dominante, "fuente_dominante_etiqueta": dominante_etiqueta,
            "desglose_fuente": desglose,
            "fechas_excluidas": [d.isoformat() for d in sorted(act["fechas_excluidas"])],
            "dias": [
                {**d, "fecha": d["fecha"].isoformat()}
                for d in sorted(act["dias"], key=lambda d: d["fecha"])
            ],
            # Privados: solo para que `resumen_ventana` calcule mejoró/empeoró;
            # no van en la respuesta final.
            "_dias_automaticos_previo": prev["automaticos"],
            "_dias_totales_previo": total_prev,
        })
    return filas_resultado


# Más de esta cantidad de días de diferencia contra el período anterior cuenta
# como "cambió" -- en DÍAS, no en puntos porcentuales: un mismo umbral en % no
# significa lo mismo en una semana (1 día ya son ~14 pts) que en un mes (1 día
# son ~3 pts), así que un umbral en días es lo único comparable entre las dos
# ventanas (decidido con la usuaria, maqueta del Resumen).
UMBRAL_DIAS_CAMBIO = 2


def resumen_ventana(desde: date, hasta: date) -> dict:
    """La tabla unificada del Resumen: una fila por frontera+tipo con su tasa
    de automático, la fuente dominante cuando no lo fue, y cómo cambió contra
    el período inmediatamente anterior de igual duración -- reemplaza a los
    tres gráficos separados que mostraba el Resumen viejo (fuente de
    generación, fuente de consumo, automático-vs-otra-fuente): la maqueta que
    aprobó la usuaria fusiona esas tres preguntas en una sola fila por
    frontera, más 5 tarjetas KPI.
    """
    if hasta < desde:
        raise NoProcesable("'hasta' no puede ser anterior a 'desde'")

    filas = _agregar_ventana_por_frontera(desde, hasta)

    dias_automaticos_totales = sum(f["dias_automaticos"] for f in filas)
    dias_totales = sum(f["dias_automaticos"] + f["dias_no_automaticos"] for f in filas)
    siempre_automatico = 0
    nunca_automatico = 0
    mejoraron = 0
    empeoraron = 0

    for f in filas:
        total = f["dias_automaticos"] + f["dias_no_automaticos"]
        if f["nunca_clasificado"]:
            nunca_automatico += 1
        elif total > 0 and f["tasa"] == 100.0:
            siempre_automatico += 1
        elif total > 0 and f["tasa"] == 0.0:
            nunca_automatico += 1

        total_previo = f.pop("_dias_totales_previo")
        automaticos_previo = f.pop("_dias_automaticos_previo")
        if total > 0 and total_previo > 0:
            delta = f["dias_automaticos"] - automaticos_previo
            if delta > UMBRAL_DIAS_CAMBIO:
                mejoraron += 1
            elif delta < -UMBRAL_DIAS_CAMBIO:
                empeoraron += 1

    # Peor primero: las nunca clasificadas van antes que cualquier tasa, y
    # entre las demás la más baja primero -- es la cola de trabajo.
    filas.sort(key=lambda f: (0 if f["nunca_clasificado"] else 1, f["tasa"], f["nombre_proyecto"] or ""))

    return {
        "desde": desde, "hasta": hasta,
        "filas": filas,
        "kpis": {
            "tasa_general": (round(dias_automaticos_totales / dias_totales * 100, 1)
                              if dias_totales else 0.0),
            "dias_automaticos_totales": dias_automaticos_totales,
            "dias_totales": dias_totales,
            "siempre_automatico": siempre_automatico,
            "nunca_automatico": nunca_automatico,
            "mejoraron": mejoraron,
            "empeoraron": empeoraron,
        },
    }


def listar_fronteras(fecha: date, tipo: str | None = None,
                     solo_pendientes: bool = False, q: str | None = None) -> list[dict]:
    """Las fronteras del día, ordenadas por urgencia: primero las que hay que
    revisar, después las corregidas automáticamente, y al final las confiadas."""
    items: list[dict] = []

    if tipo in (None, "generacion"):
        for rep in (
            ReporteEnergiaGeneracion.objects
            .filter(fecha=fecha).select_related("frontera")
        ):
            front = rep.frontera
            proyecto_id = front.proyecto_id
            if solo_pendientes and not rep.revisar_manualmente:
                continue
            if q and q.lower() not in (_nombre_frontera(front) or "").lower():
                continue
            # `automatico`/`grupo_fuente`/`etiqueta_fuente`: para que el tab Día
            # del Resumen no reimplemente la agrupación de fuente en el
            # frontend -- misma regla que `_semaforo`/`resumen` (caso decide
            # automático) y `_agregar_ventana_por_frontera` (medidor_usado
            # decide la fuente, solo cuando NO fue automático).
            automatico = str(rep.caso) in ("1", "CGM")
            grupo_fuente = etiqueta_fuente = None
            if not automatico:
                crudo = (rep.medidor_usado or "").strip().lower()
                grupo_fuente = _GRUPO_FUENTE_GENERACION.get(crudo, "otro") if crudo else "sin_fuente"
                etiqueta_fuente = _ETIQUETA_GRUPO_FUENTE.get(grupo_fuente, "Otro")
            items.append({
                "frontera_id": front.id, "proyecto_id": proyecto_id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": "generacion", "caso": str(rep.caso),
                "medidor_usado": rep.medidor_usado,
                "automatico": automatico, "grupo_fuente": grupo_fuente,
                "etiqueta_fuente": etiqueta_fuente,
                "energia_final_kwh": (
                    float(rep.energia_final_kwh)
                    if rep.energia_final_kwh is not None else None
                ),
                "revisar_manualmente": rep.revisar_manualmente,
                "editado_manualmente": rep.editado_manualmente,
                "nota_solenium": rep.nota_solenium,
            })

    if tipo in (None, "consumo"):
        for rep in (
            ReporteEnergiaConsumo.objects
            .filter(fecha=fecha).select_related("frontera")
        ):
            front = rep.frontera
            proyecto_id = front.proyecto_id
            if solo_pendientes and not rep.revisar_manualmente:
                continue
            if q and q.lower() not in (_nombre_frontera(front) or "").lower():
                continue
            # Ver el comentario del bloque de generación -- acá `caso` decide
            # las dos cosas (automático Y fuente): en consumo es un solo campo.
            automatico = str(rep.caso) in ("1", "CGM")
            grupo_fuente = etiqueta_fuente = None
            if not automatico:
                crudo = (rep.caso or "").strip().lower()
                grupo_fuente = _GRUPO_FUENTE_CONSUMO.get(crudo, "otro") if crudo else "sin_fuente"
                etiqueta_fuente = _ETIQUETA_GRUPO_FUENTE.get(grupo_fuente, "Otro")
            items.append({
                "frontera_id": front.id, "proyecto_id": proyecto_id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": "consumo", "caso": rep.caso,
                "medidor_usado": rep.medidor_usado,
                "automatico": automatico, "grupo_fuente": grupo_fuente,
                "etiqueta_fuente": etiqueta_fuente,
                "energia_final_kwh": (
                    float(rep.energia_final_kwh)
                    if rep.energia_final_kwh is not None else None
                ),
                "revisar_manualmente": rep.revisar_manualmente,
                "editado_manualmente": rep.editado_manualmente,
            })

    # Prioridad: revisar primero, luego corregido, luego confiado.
    orden = {"critical": 0, "warning": 1, "success": 2}
    items.sort(key=lambda i: orden[_semaforo(i["caso"], i["revisar_manualmente"])])
    return items


def _fila_por_id(frontera_id: int, fecha: date):
    """`(frontera, fila del reporte, modelo)`.

    El TIPO de frontera decide la tabla: generación y consumo tienen columnas
    distintas y no comparten fila.
    """
    front = Frontera.objects.select_related("proyecto").filter(pk=frontera_id).first()
    if front is None:
        raise NotFound("Frontera no encontrada")
    Modelo = (
        ReporteEnergiaGeneracion if front.tipo_frontera == "generacion"
        else ReporteEnergiaConsumo
    )
    rep = Modelo.objects.filter(frontera_id=frontera_id, fecha=fecha).first()
    if rep is None:
        raise NotFound("No hay reporte para esa frontera y fecha")
    return front, rep, Modelo


def _construir_detalle(frontera_id: int, fecha: date) -> dict:
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    es_generacion = Modelo is ReporteEnergiaGeneracion

    # Curvas de referencia -- se prefiere lo que quedó GUARDADO al momento de
    # clasificar (no existía antes de este fix: MGS 0032 El Paso Norte
    # 2026-08-05, medidor doblado por un glitch de Quoia mostraba un número
    # arriba y otro distinto en "Detalle de las fuentes", sin explicación).
    # Se sigue consultando Quoia en vivo IGUAL que antes, pero ahora solo
    # para detectar si algo cambió desde entonces (medidor_actualizado_en_quoia)
    # -- si la fila es de antes de este fix (columnas en null), se cae a lo
    # que ya se hacía: mostrar directo lo que Quoia tiene ahora.
    curva_medidor_ppal_bd = rep.curva_medidor_principal
    curva_medidor_resp_bd = rep.curva_medidor_respaldo
    curva_sol_bd = rep.curva_solenium_referencia if es_generacion else None
    # Igual que Solenium -- se consulta SIEMPRE durante la clasificación
    # diaria (ver clasificador.clasificar_generacion) y queda persistida
    # ahí, no se vuelve a pedir en vivo cada vez que se abre el panel.
    curva_reconectador = rep.curva_reconectador_referencia if es_generacion else None

    # Solenium ya NO se consulta en vivo acá -- costaba ~2s en cada apertura
    # del panel, solo para detectar si Solenium cambió desde que se
    # clasificó (un caso mucho menos común que el del medidor). Se usa
    # directo lo que quedó persistido; si la fila es de antes del fix de
    # persistencia, simplemente no hay curva de Solenium que mostrar acá.
    capacidad_efectiva_mw = (
        float(front.proyecto.potencia_ac_kw) / 1000
        if es_generacion and front.proyecto_id and front.proyecto.potencia_ac_kw is not None else None
    )

    curva_medidor_ppal_viva = curva_medidor_resp_viva = None
    # La curva del reporte CGM de Quoia -- las 24 horas que Quoia ya tiene en su
    # sistema. Es lo que 'Reportar con otra fuente' carga en el editor cuando se
    # adopta el CGM a mano; sin curva de 24 horas no hay nada que cargar, y por
    # eso esa opción no existía (Paso Norte Consumo 2026-09-07: reporte
    # automático válido en Quoia, nuestra clasificación cayó a 'Histórico' +
    # revisar, y no había salida -- validar tal cual mandaba la matriz encima
    # del reporte oficial, y no validar bloqueaba el día entero).
    #
    # Se usa SIEMPRE la guardada al clasificar, nunca se pide en vivo: la curva
    # del CGM de un día ya cerrado no cambia -- a diferencia de los medidores,
    # que sí se corrigen después (por eso esos sí se consultan frescos). La
    # corrida de madrugada ya la consultó y la persistió.
    #
    # Antes se pedía en vivo para las filas anteriores a
    # `curva_cgm_referencia`, con dos guardas para acotar el costo. Esas filas
    # son cada vez menos (la columna existe desde 2026-09-07) y la llamada
    # estaba en una ruta que se abre muchas veces al día. Consecuencia asumida:
    # en una fila vieja sin la columna, "Reportar con otra fuente" no ofrece el
    # CGM; el dato sigue estando en Quoia para quien lo necesite.
    curva_cgm_bd = rep.curva_cgm_referencia
    try:
        gaia = GaiaClient()
        # Cacheados (ver curvas._CACHE_TTL) -- esta vista se abre repetidas
        # veces por sesión solo para mostrar curvas de referencia, no hace
        # falta traer el catálogo completo de Quoia en cada clic. En frío
        # (TTL vencido) son dos llamadas HTTP independientes -- en paralelo
        # en vez de secuencial, el costo es el máximo de las dos, no la suma.
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_nodo = executor.submit(curvas.construir_mapa_medidor_nodo, gaia)
            fut_borders = executor.submit(curvas.construir_mapa_borders, gaia)
            mapa_nodo = fut_nodo.result()
            borders = fut_borders.result()
        meta = borders.get((front.codigo_frontera or "").strip().lower())
        if meta:
            # curva_medidor_en_vivo() en vez de curvas_de_frontera(): acá solo
            # hace falta UNA variable (eae o iae, según el tipo de frontera) --
            # curvas_de_frontera() trae las 4 (eae+iae x principal+respaldo) de
            # forma secuencial porque el clasificador sí las necesita todas;
            # pedir las 2 de más y en secuencia era la mayor parte de la demora
            # al abrir el panel (2026-08-12). Sin recuperación activa tampoco
            # -- esto es solo para mostrar una curva de referencia, no tiene
            # sentido interrogar el medidor (hasta 90s) por eso.
            var_name = "eae" if es_generacion else "iae"
            curva_p, curva_r = curvas.curva_medidor_en_vivo(
                gaia, mapa_nodo, meta.get("main_meter"), meta.get("backup_meter"),
                str(fecha), front.codigo_frontera, var_name, capacidad_efectiva_mw,
            )
            curva_medidor_ppal_viva = curva_a_lista(curva_p)
            curva_medidor_resp_viva = curva_a_lista(curva_r)
    except Exception:
        pass  # las curvas de referencia son informativas -- si fallan, se muestra igual el resultado ya guardado

    # Por medidor, no solo el que ganó como medidor_usado (2026-08-20): si
    # el clasificador usó 'Histórico' porque el medidor estaba mal en ese
    # momento, y luego alguien recupera el medidor (a mano en Quoia, o con
    # el botón "Recuperar medidor"), esto tiene que poder avisarlo igual --
    # antes, al estar escopado solo al medidor usado, ese caso quedaba
    # invisible: ni el aviso ni la opción "(actualizado)" aparecían nunca.
    principal_actualizado_en_quoia = bool(curva_cambio(curva_medidor_ppal_bd, curva_medidor_ppal_viva))
    respaldo_actualizado_en_quoia = bool(curva_cambio(curva_medidor_resp_bd, curva_medidor_resp_viva))
    curva_medidor_ppal = curva_medidor_ppal_bd if curva_medidor_ppal_bd is not None else curva_medidor_ppal_viva
    curva_medidor_resp = curva_medidor_resp_bd if curva_medidor_resp_bd is not None else curva_medidor_resp_viva
    curva_sol = curva_sol_bd

    # Curva y total EN VIVO de cada medidor -- para el aviso "el medidor ya
    # muestra un valor distinto en Quoia" (curva_medidor_principal/respaldo
    # ya muestran lo persistido) y para que 'Reportar con otra fuente'
    # pueda ofrecer directamente ese valor actualizado, sin que la persona
    # tenga que copiarlo a mano (pedido 2026-08-12; ampliado a ambos
    # medidores 2026-08-20).
    principal_energia_actual_kwh = None
    principal_curva_actual: list | None = None
    if principal_actualizado_en_quoia and curva_medidor_ppal_viva is not None:
        principal_curva_actual = curva_medidor_ppal_viva
        principal_energia_actual_kwh = sum(v for v in principal_curva_actual if v is not None)

    respaldo_energia_actual_kwh = None
    respaldo_curva_actual: list | None = None
    if respaldo_actualizado_en_quoia and curva_medidor_resp_viva is not None:
        respaldo_curva_actual = curva_medidor_resp_viva
        respaldo_energia_actual_kwh = sum(v for v in respaldo_curva_actual if v is not None)

    # Lo que /enviar realmente manda como "Backup" -- congelado desde que se
    # fijó curva_final (ver actualizar_respaldo_final()); si es una fila de
    # antes de que existiera esa columna, se calcula al vuelo igual que
    # antes (mismo criterio de respaldo que /enviar usaría ahora mismo).
    # Generación y Consumo (extendido 2026-08-26).
    #
    # Si medidor_usado == 'cgm', /enviar en realidad NO manda nada
    # (_reporte_ya_valido() se lo salta) -- estos dos campos igual se
    # calculan, por consistencia visual con Principal (ver
    # curva_respaldo_a_reportar() en utils.py), pero ahí son informativos:
    # "así se vería", no "esto se va a enviar".
    curva_respaldo_reportada = respaldo_reportado_origen = None
    if rep.curva_final:
        curva_respaldo_reportada = rep.curva_respaldo_final
        respaldo_reportado_origen = rep.respaldo_final_origen
        if curva_respaldo_reportada is None:
            curva_respaldo_reportada, respaldo_reportado_origen = curva_respaldo_a_reportar(rep)

    # Mediana histórica de la frontera -- el clasificador compara el día
    # contra esto para decidir la marca de revisión, así que exponerlo es lo
    # que permite entender POR QUÉ algo quedó marcado sin tener que abrir
    # "Curva Típica" de la corrección manual (2026-09-02). Se calcula al
    # vuelo, no se persiste: es una consulta sobre las filas ya guardadas.
    try:
        if es_generacion:
            mediana_historica, dias_historial = historial.get_mediana_generacion(frontera_id, fecha)
        else:
            mediana_historica, dias_historial = historial.get_mediana_consumo(frontera_id, fecha)
        mediana_historica = float(mediana_historica) if mediana_historica is not None else None
    except Exception:
        # Nunca debe tumbar el detalle -- es informativo.
        logger.exception("No se pudo calcular la mediana histórica de la frontera %s", frontera_id)
        mediana_historica = dias_historial = None

    return {
        "frontera_id": front.id, "proyecto_id": front.proyecto_id, "nombre_proyecto": _nombre_frontera(front),
        "tipo": "generacion" if es_generacion else "consumo", "fecha": fecha,
        "caso": str(rep.caso), "medidor_usado": rep.medidor_usado,
        "energia_final_kwh": float(rep.energia_final_kwh) if rep.energia_final_kwh is not None else None,
        "curva_final": rep.curva_final or [None] * 24,
        "fp": float(rep.fp) if es_generacion and rep.fp is not None else None,
        "fp_calculada": float(rep.fp_calculada) if es_generacion and rep.fp_calculada is not None else None,
        "error_final_pct": float(rep.error_final_pct) if es_generacion and rep.error_final_pct is not None else None,
        "energia_cgm_kwh": float(rep.energia_cgm_kwh) if rep.energia_cgm_kwh is not None else None,
        "estado_reporte": rep.estado_reporte,
        "energia_solenium_kwh": float(rep.energia_solenium_kwh) if es_generacion and rep.energia_solenium_kwh is not None else None,
        "solenium_completo": rep.solenium_completo if es_generacion else None,
        "nota_solenium": rep.nota_solenium if es_generacion else None,
        "horas_rellenadas_reconectador": rep.horas_rellenadas_reconectador if es_generacion else None,
        "horas_rellenadas_solenium": rep.horas_rellenadas_solenium if es_generacion else None,
        "horas_rellenadas_historico": rep.horas_rellenadas_historico,
        "horas_rellenadas_medidor_cruzado": rep.horas_rellenadas_medidor_cruzado,
        "recuperacion_datos": rep.recuperacion_datos,
        "mediana_historica_kwh": mediana_historica,
        "dias_historial": dias_historial,
        "revisar_manualmente": rep.revisar_manualmente, "editado_manualmente": rep.editado_manualmente,
        "error_clasificacion": rep.error_clasificacion,
        "enviado_quoia_en": rep.enviado_quoia_en, "enviado_quoia_ok": rep.enviado_quoia_ok,
        "enviado_quoia_error": rep.enviado_quoia_error,
        "curva_medidor_principal": curva_medidor_ppal,
        "curva_medidor_respaldo": curva_medidor_resp,
        # En vivo, no persistida (ver el bloque de arriba). None si Quoia no
        # respondió o si ese día no hubo reporte -- ahí la opción de reportar
        # con CGM queda deshabilitada en el front.
        "curva_cgm": curva_cgm_bd,
        "curva_solenium": curva_sol,
        "curva_reconectador": curva_reconectador,
        "principal_actualizado_en_quoia": principal_actualizado_en_quoia,
        "principal_energia_actual_kwh": round(principal_energia_actual_kwh, 4) if principal_energia_actual_kwh is not None else None,
        "principal_curva_actual": principal_curva_actual,
        "respaldo_actualizado_en_quoia": respaldo_actualizado_en_quoia,
        "respaldo_energia_actual_kwh": round(respaldo_energia_actual_kwh, 4) if respaldo_energia_actual_kwh is not None else None,
        "respaldo_curva_actual": respaldo_curva_actual,
        "curva_respaldo_reportada": curva_respaldo_reportada,
        "respaldo_reportado_origen": respaldo_reportado_origen,
        "capacidad_efectiva_mw": capacidad_efectiva_mw,
    }
