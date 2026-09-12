"""Generación mensual promedio por proyecto.

Puerto de `app/services/gen_promedio.py`.

**Por qué existe.** Cada vista que necesitaba "cuánto genera esta planta en un
mes" salía a la API de generación planta por planta: lento (minutos para 70
proyectos), frágil (si esa API está caída no hay vista) y repetido en cada
consulta aunque el número casi no cambie. Acá se calcula UNA vez y se persiste
en `proyectos.gen_mensual_promedio_mwh`.

**Qué es el promedio.** MWh en un mes típico, medido sobre una ventana MÓVIL de
los últimos 30 días corridos, no sobre el mes calendario anterior: un promedio
"de julio" consultado el 9 de agosto describe algo que terminó hace más de una
semana. El día de hoy no entra —está a medias— y si la ventana tiene muchos días
sin lectura no se devuelve número: sería medir la caída del monitoreo, no la de
la planta.

**Manual contra API.** Una planta recién energizada no tiene histórico y su
promedio se carga a mano; `gen_promedio_origen` distingue los dos casos y el
recálculo respeta lo manual salvo que se pida `force`. Mismo patrón que
`fecha_comercializacion_editada_manual`.

El módulo se divide a propósito en dos capas: `promedio_mensual` y `decidir` son
PURAS —sin red ni base— y ahí vive la regla; `recalcular` orquesta.

`ponytail: el fan-out es un pool de hilos, no asyncio`. El original usaba
`asyncio.gather` con un semáforo de 8; las vistas de DRF son sincrónicas y
mezclar los dos modelos por una llamada HTTP no compra nada. Mismo criterio que
`apps/energia/services/unergy_api.py`.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.db.models import Q

from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto

logger = logging.getLogger("operaciones.proyectos.gen_promedio")

HILOS = 8




# La ventana vale si al menos este ratio de sus días tiene lectura. 0.85 deja
# pasar un fin de semana perdido del monitoreo pero descarta una planta recién
# energizada, que daría un promedio construido sobre cuatro días.
RATIO_DIAS_MINIMO = 0.85

# Largo de la ventana móvil, en días corridos hacia atrás desde ayer. 30 ≈ un mes
# y es lo que hace que el número describa a la planta HOY y no el mes pasado.
DIAS_POR_DEFECTO = 30

# El campo se expresa siempre como "MWh en un mes de 30 días", sea cual sea el
# largo de la ventana: si no, cambiar `dias` cambiaría la unidad del dato.
DIAS_MES_REFERENCIA = 30

ORIGEN_API = "api"
ORIGEN_MANUAL = "manual"


# ── Núcleo puro ──────────────────────────────────────────────────────────────


def promedio_mensual(
    por_dia: dict[date, float],
    hoy: date,
    dias: int = DIAS_POR_DEFECTO,
) -> dict:
    """Promedio en MWh/mes sobre los últimos `dias` días corridos.

    La ventana es `[hoy - dias, hoy - 1]`: **hoy no entra** porque está a medias,
    y lo anterior a la ventana tampoco, aunque haya histórico. Esa es la
    diferencia con promediar el mes calendario pasado — el número describe a la
    planta ahora.

    El promedio se normaliza por los días **con lectura**, no por el largo de la
    ventana: si el monitoreo se cayó tres días, la planta no generó menos, y
    dividir por 30 haría ver una caída que no existe.

    Devuelve siempre la misma forma, también cuando no alcanza para calcular:
    `promedio_mwh=None` con el motivo. Un `None` explícito es mejor que un cero —
    "no sé" y "genera cero" son cosas distintas.
    """
    hasta = hoy - timedelta(days=1)
    desde = hoy - timedelta(days=dias)
    vacio = {"promedio_mwh": None, "dias": dias, "dias_con_datos": 0,
             "desde": desde, "hasta": hasta, "motivo": None}

    en_ventana = {d: float(k or 0) for d, k in por_dia.items() if desde <= d <= hasta}
    if not en_ventana:
        return {**vacio, "motivo": f"sin lecturas entre {desde} y {hasta}"}

    n = len(en_ventana)
    if n < dias * RATIO_DIAS_MINIMO:
        return {**vacio, "dias_con_datos": n,
                "motivo": f"solo {n} de {dias} días con lecturas: "
                          "muy poco para un promedio confiable"}

    promedio_diario = sum(en_ventana.values()) / n
    return {
        "promedio_mwh": round(promedio_diario * DIAS_MES_REFERENCIA / 1000.0, 3),
        "dias": dias,
        "dias_con_datos": n,
        "desde": desde,
        "hasta": hasta,
        "motivo": None,
    }


def proyectos_objetivo(proyecto_ids: list[int] | None = None) -> list[Proyecto]:
    """Los proyectos a los que tiene sentido calcularles el promedio.

    Mismo universo que Cumplimiento: en operación y no autoconsumo. Se incluyen
    los que NO tienen `sub_project` — no se les puede calcular nada, pero deben
    aparecer en el reporte para saber cuáles hay que cargar a mano.
    """
    qs = Proyecto.objects.filter(deleted_at__isnull=True, estado="en_operacion").filter(
        # `!= autoconsumo` a secas deja fuera los NULL: en SQL una comparación
        # contra NULL da NULL, y NULL no pasa el WHERE. Con eso, toda planta sin
        # tipo cargado quedaba fuera del recálculo en silencio y nunca recibía
        # promedio, se corriera las veces que se corriera.
        Q(tipo_proyecto__isnull=True) | ~Q(tipo_proyecto="autoconsumo")
    )
    if proyecto_ids:
        qs = qs.filter(id__in=proyecto_ids)
    return list(qs.order_by("nombre_comercial"))


def aplicar(proyecto: Proyecto, resultado: dict, ahora: datetime) -> None:
    """Escribe el resultado del cálculo en el proyecto (en memoria)."""
    proyecto.gen_mensual_promedio_mwh = Decimal(str(resultado["promedio_mwh"]))
    proyecto.gen_promedio_origen = ORIGEN_API
    proyecto.gen_promedio_dias = resultado["dias_con_datos"]
    proyecto.gen_promedio_desde = resultado["desde"]
    proyecto.gen_promedio_hasta = resultado["hasta"]
    proyecto.gen_promedio_actualizado_en = ahora


CAMPOS_APLICADOS = [
    "gen_mensual_promedio_mwh", "gen_promedio_origen", "gen_promedio_dias",
    "gen_promedio_desde", "gen_promedio_hasta", "gen_promedio_actualizado_en",
]


def decidir(proyecto: Proyecto, force: bool) -> str | None:
    """¿Hay que saltarse este proyecto? Devuelve el motivo, o None si se procesa."""
    if proyecto.gen_promedio_origen == ORIGEN_MANUAL and not force:
        return "valor manual (usar force=true para pisarlo)"
    if not proyecto.sub_project:
        return "sin identificador de monitoreo: cargar el promedio a mano"
    return None


def recalcular(dias: int = DIAS_POR_DEFECTO, dry_run: bool = True,
               force: bool = False, proyecto_ids: list[int] | None = None,
               hoy: date | None = None) -> dict:
    """Recalcula el promedio desde la API de generación y lo persiste.

    `dry_run=True` (el default) reporta sin escribir; `force=True` pisa también
    los valores cargados a mano.

    Un proyecto que falla NO tumba la corrida: queda en `fallidos` con su motivo.
    Nada de huecos callados — un proyecto que no se pudo calcular tiene que verse.
    """
    from apps.energia.services import unergy_api

    hoy = hoy or hoy_col()
    ahora = datetime.now(timezone.utc)
    objetivo = proyectos_objetivo(proyecto_ids)

    actualizados, sin_datos, saltados, fallidos = [], [], [], []
    a_procesar = []
    for p in objetivo:
        motivo = decidir(p, force)
        if motivo:
            saltados.append({"id": p.id, "nombre": p.nombre_comercial, "motivo": motivo})
        else:
            a_procesar.append(p)

    if a_procesar:
        try:
            token = unergy_api.token()
        except Exception as exc:
            return {
                "error": f"no se pudo autenticar contra la API de generación: {exc}",
                "actualizados": [], "sin_datos": [], "saltados": saltados, "fallidos": [],
            }

        # Se pide la ventana con días de colchón: el primer delta necesita una
        # lectura previa con la que restarse (el contador es acumulado).
        inicio = hoy - timedelta(days=dias + 2)
        (pedir_desde, pedir_hasta), (desde_dt, hasta_dt) = unergy_api.ventana_utc(inicio, hoy)

        def uno(p: Proyecto):
            try:
                # La fuente (verificada o cruda) no se guarda: esto calcula un
                # promedio historico y la etiqueta no tendria donde vivir.
                lecturas, _fuente = unergy_api.lecturas_con_respaldo(
                    token, p.sub_project, pedir_desde, pedir_hasta
                )
                return p, unergy_api.deltas(lecturas, desde_dt, hasta_dt), None
            except Exception as exc:
                return p, None, str(exc)

        with ThreadPoolExecutor(max_workers=min(len(a_procesar), HILOS)) as pool:
            resultados = list(pool.map(uno, a_procesar))

        a_guardar = []
        for p, entradas, error in resultados:
            if error is not None:
                fallidos.append({"id": p.id, "nombre": p.nombre_comercial, "error": error})
                continue

            por_dia: dict[date, float] = {}
            for e in entradas or []:
                try:
                    d = date.fromisoformat(str(e.get("date"))[:10])
                except (TypeError, ValueError):
                    continue
                por_dia[d] = por_dia.get(d, 0.0) + float(e.get("kwh") or 0)

            r = promedio_mensual(por_dia, hoy=hoy, dias=dias)
            if r["promedio_mwh"] is None:
                sin_datos.append({
                    "id": p.id, "nombre": p.nombre_comercial,
                    "motivo": r["motivo"], "dias_con_datos": r["dias_con_datos"],
                })
                continue

            anterior = (
                float(p.gen_mensual_promedio_mwh)
                if p.gen_mensual_promedio_mwh is not None else None
            )
            if not dry_run:
                aplicar(p, r, ahora)
                a_guardar.append(p)
            actualizados.append({
                "id": p.id, "nombre": p.nombre_comercial,
                "anterior_mwh": anterior, "nuevo_mwh": r["promedio_mwh"],
                "dias_con_datos": r["dias_con_datos"],
                "desde": r["desde"].isoformat(), "hasta": r["hasta"].isoformat(),
            })

        if a_guardar:
            Proyecto.objects.bulk_update(a_guardar, CAMPOS_APLICADOS)

    return {
        "dry_run": dry_run,
        "force": force,
        "dias_ventana": dias,
        "hoy": hoy.isoformat(),
        "n_objetivo": len(objetivo),
        "n_actualizados": len(actualizados),
        "n_sin_datos": len(sin_datos),
        "n_saltados": len(saltados),
        "n_fallidos": len(fallidos),
        "actualizados": actualizados,
        # Estas tres listas son el valor del reporte: dicen exactamente qué
        # plantas hay que cargar a mano y cuáles hay que mirar.
        "sin_datos": sin_datos,
        "saltados": saltados,
        "fallidos": fallidos,
    }
