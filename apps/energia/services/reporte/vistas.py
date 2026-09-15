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

from datetime import date

from django.db.models import Count, Q

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
_ORDEN_GRUPO_FUENTE = [
    "cgm", "medidor", "inversor", "terceros", "estimacion", "apagado",
    "sin_fuente", "otro",
]


_ETIQUETA_FUENTE_CRUDA_GENERACION = {
    "cgm": "CGM", "principal": "Medidor principal", "respaldo": "Medidor respaldo",
    "principal_sin_cgm": "Medidor principal", "respaldo_sin_cgm": "Medidor respaldo",
    "principal_sin_historico": "Medidor principal", "respaldo_sin_historico": "Medidor respaldo",
    "reconectador": "Reconectador", "excel_terceros": "Excel de terceros", "externo": "Reporta otra empresa",
    "inversores": "Inversores × FP", "crudos": "Datos crudos", "crudos_parcial": "Datos crudos (parcial)",
    "solenium_power": "Inversores (power)",
    "historico": "Histórico propio", "historico_vecino": "Histórico (vecino de predio)",
    "ninguno": "Apagado", "editado_manualmente": "Editado manualmente", "relleno_horario": "Relleno horario",
    "revisar": "Sin fuente",
}


AUTOMATICO = "Automático (CGM)"
OTRA_FUENTE = "Otra fuente"


def _distribucion_automatico(gen_filas, con_filas):
    """Cuantos dias-frontera se reportaron automatico via CGM, y cuantos no.

    Es una pregunta distinta de la de los otros dos graficos. Esos dicen **de
    donde salio el dato**; este dice **cuanto salio solo**, que es la metrica
    para saber si la automatizacion avanza.

    Generacion y consumo van JUNTOS: lo que se quiere medir es el reporte
    entero, no una de sus mitades. Un dia-frontera de generacion y uno de
    consumo cuentan igual, porque los dos son un reporte que alguien tuvo que
    hacer o que salio solo.

    Los dias EXCLUIDOS no entran en ninguno de los dos lados: no se reportaron a
    proposito, asi que no son ni un exito ni un fallo de la automatizacion.
    Contarlos como "otra fuente" empeoraria la metrica por una decision
    deliberada.
    """
    cuenta = {AUTOMATICO: 0, OTRA_FUENTE: 0}
    por_frontera: dict[int, dict] = {}

    for filas in (gen_filas, con_filas):
        for fid, nombre, etiqueta_cruda, n in filas:
            low = (etiqueta_cruda or "").strip().lower()
            if low == "excluida":
                continue
            grupo = AUTOMATICO if low == "cgm" else OTRA_FUENTE
            cuenta[grupo] += n
            info = por_frontera.setdefault(fid, {
                "nombre": _NOMBRES_CORREGIDOS.get(fid, nombre),
                "dias_totales": 0, "grupos": {},
            })
            info["dias_totales"] += n
            g = info["grupos"].setdefault(grupo, {"dias": 0})
            g["dias"] += n

    distribucion = [
        {"etiqueta": etq, "total": cuenta[etq]}
        for etq in (AUTOMATICO, OTRA_FUENTE) if cuenta[etq]
    ]
    detalle = [
        {
            "frontera_id": fid,
            "nombre_proyecto": info["nombre"],
            "grupo": grupo,
            "dias_totales": info["dias_totales"],
            "dias_grupo": g["dias"],
            "desglose": [],
        }
        for fid, info in por_frontera.items()
        for grupo, g in info["grupos"].items()
    ]
    detalle.sort(key=lambda d: (-d["dias_grupo"], d["nombre_proyecto"] or ""))
    return distribucion, detalle


def _distribucion_y_detalle(
    filas: list[tuple[int, str, str | None, int]], mapa: dict[str, str],
    etiquetas_legibles: dict[str, str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """A partir de (frontera_id, nombre_frontera, etiqueta_cruda, n) --
    devuelve (a) el total agrupado global (para las tarjetas KPI) y (b) el
    detalle por frontera+grupo con desglose de fuentes crudas (para el
    drill-down al hacer clic en una tarjeta). 'excluida' no se cuenta como
    fuente, pero SÍ cuenta en dias_totales (ese día sí tuvo fila, solo que
    a propósito no se reportó)."""
    conteos_globales: dict[str, int] = {}
    por_frontera: dict[int, dict] = {}
    for fid, nombre, etiqueta_cruda, n in filas:
        info = por_frontera.setdefault(fid, {
            "nombre": _NOMBRES_CORREGIDOS.get(fid, nombre), "dias_totales": 0, "grupos": {},
        })
        info["dias_totales"] += n
        if etiqueta_cruda is None:
            grupo, etq_legible = "sin_fuente", "Sin fuente"
        else:
            low = etiqueta_cruda.strip().lower()
            if low == "excluida":
                continue  # no es una fuente -- ese día no se reportó a propósito
            grupo = mapa.get(low, "otro")
            etq_legible = (etiquetas_legibles or {}).get(low, etiqueta_cruda)
        conteos_globales[grupo] = conteos_globales.get(grupo, 0) + n
        g = info["grupos"].setdefault(grupo, {"dias": 0, "desglose": {}})
        g["dias"] += n
        g["desglose"][etq_legible] = g["desglose"].get(etq_legible, 0) + n

    global_dist = [
        {"etiqueta": _ETIQUETA_GRUPO_FUENTE[g], "total": conteos_globales[g]}
        for g in _ORDEN_GRUPO_FUENTE if g in conteos_globales
    ]
    detalle = [
        {
            "frontera_id": fid,
            "nombre_proyecto": info["nombre"],
            "grupo": _ETIQUETA_GRUPO_FUENTE[grupo],
            "dias_totales": info["dias_totales"],
            "dias_grupo": g["dias"],
            "desglose": [
                {"etiqueta": e, "dias": n}
                for e, n in sorted(g["desglose"].items(), key=lambda kv: -kv[1])
            ],
        }
        for fid, info in por_frontera.items()
        for grupo, g in info["grupos"].items()
    ]
    return global_dist, detalle


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


def resumen_historico(desde: date, hasta: date) -> dict:
    """Patrones a lo largo de VARIOS días, por frontera — distinto de `resumen`,
    que es de un solo día.

    Responde dos cosas: de qué fuente salió el dato de cada día-frontera, y
    cuánto del reporte salió automático por CGM.

    Tres secciones se quitaron por el camino: "Intervención manual recurrente" y
    "Recuperación activa de medidores" (2026-08-26), y "Datos incompletos de
    medidores e inversores" (2026-09-15).

    Esa última contaba en cuántos días llegó incompleta cada fuente, y se quitó
    porque la pregunta estaba mal planteada: "incompleto" es una bandera de sí/no
    que junta cuatro situaciones distintas --el medidor arrancó tarde, se cayó
    temprano, tuvo huecos en medio, o la planta no generó ese día-- y faltar una
    hora se veía igual que faltar diez. Ni el conteo ni el porcentaje arreglaban
    eso. Medirlo bien es contar HORAS faltantes, y para que eso no cueste una
    lectura de ~1.000 curvas JSON por consulta habría que calcularlas al generar
    el reporte y guardarlas en columnas nuevas. Se decidió que no valía el
    trabajo por ahora.

    **Las banderas del modelo se quedan**: `medidor_principal_completo`,
    `medidor_respaldo_completo` y `solenium_completo` las usa el clasificador
    para decidir el caso de cada reporte. Lo que se fue es la métrica construida
    encima de ellas.
    """
    if hasta < desde:
        raise NoProcesable("'hasta' no puede ser anterior a 'desde'")

    # 1) Distribución de fuente -- agrupada en Medidor/Inversor/Estimación/
    # Sin fuente (decidido con el usuario 2026-08-21: el vocabulario crudo
    # de medidor_usado/caso tiene demasiadas variantes técnicas para leerse
    # como KPI). Se trae por frontera (no solo el total) para poder armar
    # el drill-down al hacer clic en una tarjeta.
    gen_filas = [
        (f["frontera_id"], f["frontera__nombre_frontera"], f["medidor_usado"], f["n"])
        for f in ReporteEnergiaGeneracion.objects
        .filter(fecha__range=(desde, hasta))
        .values("frontera_id", "frontera__nombre_frontera", "medidor_usado")
        .annotate(n=Count("id"))
    ]
    con_filas = [
        (f["frontera_id"], f["frontera__nombre_frontera"], f["caso"], f["n"])
        for f in ReporteEnergiaConsumo.objects
        .filter(fecha__range=(desde, hasta))
        .values("frontera_id", "frontera__nombre_frontera", "caso")
        .annotate(n=Count("id"))
    ]
    dist_gen, detalle_gen = _distribucion_y_detalle(gen_filas, _GRUPO_FUENTE_GENERACION, _ETIQUETA_FUENTE_CRUDA_GENERACION)
    dist_con, detalle_con = _distribucion_y_detalle(con_filas, _GRUPO_FUENTE_CONSUMO)
    dist_auto, detalle_auto = _distribucion_automatico(gen_filas, con_filas)

    return {
        "desde": desde, "hasta": hasta,
        "distribucion_fuente_generacion": dist_gen,
        "distribucion_fuente_consumo": dist_con,
        "detalle_fuente_generacion": detalle_gen,
        "detalle_fuente_consumo": detalle_con,
        # Cuanto del reporte salio automatico por CGM. Generacion y consumo
        # juntos: la pregunta es sobre el reporte entero, no sobre una mitad.
        "distribucion_automatico": dist_auto,
        "detalle_automatico": detalle_auto,
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
            items.append({
                "frontera_id": front.id, "proyecto_id": proyecto_id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": "generacion", "caso": str(rep.caso),
                "medidor_usado": rep.medidor_usado,
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
            items.append({
                "frontera_id": front.id, "proyecto_id": proyecto_id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": "consumo", "caso": rep.caso,
                "medidor_usado": rep.medidor_usado,
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
