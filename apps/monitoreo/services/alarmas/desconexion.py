"""Alarmas de desconexión / fuentes de medición.

Evalúa cada proyecto monitoreado comparando sus dos fuentes (inversores SolarView vs
medidor Gaia) y notifica vía el sistema in-app (campana) cuando detecta:
  - FUENTE_UNICA        → el proyecto no tiene medidor configurado (no se puede cruzar)
  - SIN_DATOS           → de día, ambas fuentes en 0 / sin datos
  - POSIBLE_DESCONEXION → de día, una fuente genera y la otra en 0 (peligro)
  - RECUPERACION        → vuelve a reportar normal tras una alarma

Anti-spam: notifica solo en cambios de estado; re-notifica una vez al día si persiste.
Corre dentro del ciclo de 15 min de la tarea `monitoreo.sondeo_mgs`. No re-implementa la
lógica de monitoreo: reutiliza los clientes SolarView/Gaia ya existentes.

Migrado de Solenium a SolarView (Fase 2 de la migración -- Fase 1 fue Reporte de
Energía, ver commit c417d30). `avail_map` viene de GET /solarview/kpis/availability/,
que sí trae toda la flota en una sola llamada con la categoría `disconnect` ya
calculada (equivalente exacto a SoleniumClient.get_availability()) -- a diferencia
de la potencia instantánea, que SolarView solo expone por proyecto
(GET /solarview/measurements/power/), así que esa parte sigue necesitando una
llamada por proyecto (en paralelo, mismo patrón que ya usa Gaia acá abajo), y
solo para los proyectos que de verdad la necesitan (de día + con medidor).

El anti-spam contra alarma_estado y el envío de notificaciones viven en
apps.monitoreo.services.alarmas.estado, compartido con fallas/alarmas.py."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections
from datetime import datetime, timezone, timedelta

from apps.contratos.services import plantas
from apps.fronteras.models import Frontera
from apps.monitoreo.services.alarmas.estado import (
    cargar_estados, decidir_notificar, guardar_estados, notificar, usuarios_notificables,
)
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto

TIPOS_GENERACION = ["generacion", "generacion_consumo"]

logger = logging.getLogger("operaciones.alarmas.desconexion")

# ── Parámetros ajustables ─────────────────────────────────────────────────────
ZERO_KW = 0.5        # potencia <= esto se considera "en cero"
DAY_START_H = 7      # ventana de día (Colombia) para evaluar desconexión
DAY_END_H = 17


def _col_now() -> datetime:
    """Ahora en hora Colombia (UTC-5, sin DST)."""
    return datetime.now(timezone.utc) - timedelta(hours=5)


def _is_daylight() -> bool:
    return DAY_START_H <= _col_now().hour < DAY_END_H


def _latest_meter_kw(snap: dict | None) -> float | None:
    """Potencia actual del medidor (kW) = último punto de la serie de potencia."""
    if not snap:
        return None
    series = (snap.get("time_series") or {}).get("power") or []
    for pt in reversed(series):
        kw = pt.get("kw")
        if kw is not None:
            return abs(float(kw))
    return None


def _latest_inverter_kw(resp: dict | None) -> float | None:
    """Potencia actual de inversores (kW) = último punto de
    GET /solarview/measurements/power/ (total_power=1 -- ya viene sumada entre
    todos los inversores del proyecto, ver SolarViewClient.get_power)."""
    if not resp:
        return None
    serie = (resp.get("results") or {}).get("power") or {}
    if not serie:
        return None
    ultimo_ts = max(serie.keys())
    valor = serie.get(ultimo_ts)
    return abs(float(valor)) if valor is not None else None


def _registrar_comunicacion(proyectos, node_pairs, power_map, snap_map, *,
                            gaia_activo: bool) -> None:
    """Guarda la comunicación de cada fuente con lo que este ciclo ya pidió.

    No hace llamadas: reusa `power_map` y `snap_map`. Una fuente que falló
    ("ERROR") o que no se pudo consultar no se incluye, y `comunicacion.registrar`
    le conserva el estado anterior. Ver `apps/monitoreo/services/comunicacion.py`.
    """
    from apps.monitoreo.services import comunicacion
    from apps.plataforma.services.fechas import ahora_col

    # `ahora_col()` y no `_col_now()`: ese resta 5 h pero deja la zona en UTC, y
    # comparado contra horas de Bogotá daría 5 h de error.
    ahora = ahora_col()
    nuevos: dict[int, dict] = {}
    for p in proyectos:
        fila: dict = {}
        if p.id in power_map and power_map[p.id] != "ERROR":
            fila["inversores"] = comunicacion.evaluar(
                comunicacion.ultimo_dato_inversores(power_map[p.id]), ahora)
        node_p, node_r = node_pairs.get(p.id) or (None, None)
        if not (node_p or node_r):
            fila["medidor"] = None  # sin medidor vinculado: no hay fuente que evaluar
        elif gaia_activo and p.id in snap_map and snap_map[p.id] != "ERROR":
            fila["medidor"] = comunicacion.evaluar(
                comunicacion.ultimo_dato_medidor(snap_map[p.id]), ahora)
        if fila:
            nuevos[p.id] = fila
    comunicacion.registrar(nuevos, ahora)

    # El aviso de "no respondió": solo si fallaron TODAS las consultas de la
    # fuente en esta corrida (un servicio caído), no por una planta suelta.
    pedidas_sv = list(power_map.values())
    if pedidas_sv:
        comunicacion.registrar_consulta(
            "inversores", fallo=all(r == "ERROR" for r in pedidas_sv), ahora=ahora)
    # Solo las plantas CON medidor: a las demás no se les preguntó nada.
    pedidas_gaia = [snap_map[p.id] for p in proyectos
                    if any(node_pairs.get(p.id) or ()) and p.id in snap_map]
    if not gaia_activo:
        comunicacion.registrar_consulta("medidor", fallo=True, ahora=ahora)
    elif pedidas_gaia:
        comunicacion.registrar_consulta(
            "medidor", fallo=all(r == "ERROR" for r in pedidas_gaia), ahora=ahora)


_MENSAJES = {
    "fuente_unica": (
        "alerta", "Fuente única de medición",
        "{n} solo tiene inversores (sin medidor configurado) — no se puede cruzar inversores vs medidor.",
    ),
    "sin_datos": (
        "alerta", "Proyecto sin datos",
        "{n}: ni inversores ni medidor reportan generación de día (posible desconexión).",
    ),
    "posible_desconexion": (
        "alerta", "⚠️ Posible desconexión",
        "{n}: una fuente genera y la otra está en 0 (inversores {inv} kW / medidor {met} kW). Revisar.",
    ),
}


def _procesar(cache, pending_writes: list[dict], usuarios,
              proyecto: Proyecto, categoria: str, estado_nuevo: str, ctx: dict):
    """Envoltorio delgado sobre estado.decidir_notificar(): sabe qué mensaje
    corresponde a cada estado de desconexión (`_MENSAJES`) y arma el texto;
    la decisión de si toca notificar y el UPSERT masivo viven en el módulo
    compartido (ver import)."""
    hoy = _col_now().date()
    notify, recovery = decidir_notificar(cache, pending_writes, proyecto.id, categoria, estado_nuevo, hoy)
    if not notify:
        return

    nombre = proyecto.nombre_comercial or f"Proyecto {proyecto.id}"
    if recovery:
        notificar(usuarios, "info", "Proyecto recuperado",
                  f"{nombre} volvió a reportar normal.")
    else:
        tipo, titulo, plantilla = _MENSAJES[estado_nuevo]
        mensaje = plantilla.format(n=nombre, inv=ctx.get("inv", "—"), met=ctx.get("met", "—"))
        notificar(usuarios, tipo, titulo, mensaje)


# ── Entrada principal ─────────────────────────────────────────────────────────
def evaluar_desconexiones(gaia=None):
    """Evalúa todos los proyectos monitoreados y emite notificaciones. Idempotente.

    `gaia`: el `GaiaClient` del sondeo que la llama, para no iniciar una
    segunda sesión en Quoia en la misma corrida (96 logins al día menos).
    Sin él, crea uno propio.
    """
    from apps.comun.integraciones.solarview_client import SolarViewClient

    sv = SolarViewClient()
    if not sv.enabled:
        logger.info("SolarView no configurado — alarmas de desconexión omitidas")
        return

    try:
        proyectos = list(Proyecto.objects.filter(
            plantas.filtro_operadas(hoy_col()),
            project_id_solarview__isnull=False,
            tipo_proyecto="minigranja",
        ))
        if not proyectos:
            return

        # Precarga de alarma_estado: un solo SELECT para todos los proyectos
        # de este ciclo (ver apps.monitoreo.services.alarmas.estado) en vez de uno por
        # (proyecto, categoria) dentro de _procesar().
        cache = cargar_estados([p.id for p in proyectos])
        pending_writes: list[dict] = []
        usuarios = usuarios_notificables()

        # Inversores: disponibilidad de TODA la flota en una sola llamada
        # (GET /solarview/kpis/availability/, ya trae la categoria
        # 'disconnect' calculada -- ver SolarViewClient.get_availability).
        avail_map = sv.get_availability() or {}
        if not avail_map:
            logger.warning("SolarView devolvió vacío — se omite evaluación (evita falsas alarmas)")
            if _is_daylight():
                from apps.monitoreo.services import comunicacion
                from apps.plataforma.services.fechas import ahora_col

                comunicacion.registrar_consulta("inversores", fallo=True, ahora=ahora_col())
            return

        if gaia is None:
            from apps.comun.integraciones.gaia_client import GaiaClient

            gaia = GaiaClient()
        daylight = _is_daylight()

        # Vínculo directo fronteras.proyecto_id -> codigo_frontera (fuente de verdad
        # reconciliada, ver scripts/etl_fronteras_proyectos.py). Evita adivinar por
        # nombre para la gran mayoría de los proyectos.
        from apps.comun.integraciones.gaia_client import (
            build_db_proyecto_frt_map, find_gaia_node_pair,
        )

        _db_fronteras = list(Frontera.objects.filter(
            tipo_frontera__in=TIPOS_GENERACION, codigo_frontera__isnull=False,
        ).values_list("proyecto_id", "codigo_frontera"))
        _db_proyecto_frt_map = build_db_proyecto_frt_map(_db_fronteras)

        # Resolver medidor (vínculo directo en BD, sin red) y traer snapshots en paralelo
        node_pairs = {}
        for p in proyectos:
            node_pairs[p.id] = find_gaia_node_pair(
                proyecto_id=p.id, db_proyecto_frt_map=_db_proyecto_frt_map,
            )

        snap_map: dict[int, dict | None] = {}
        if gaia and gaia.enabled:
            def _snap(p):
                node_p, node_r = node_pairs[p.id]
                node = node_p or node_r
                if not node:
                    return p.id, None
                try:
                    snap, fallo = gaia.get_node_electrical_snapshot_con_estado(node)
                except Exception:
                    fallo = True
                # distinguir fallo de red de "el medidor no mandó datos"
                return p.id, "ERROR" if fallo else snap
            with ThreadPoolExecutor(max_workers=6) as ex:
                for pid, snap in ex.map(_snap, proyectos):
                    snap_map[pid] = snap
            close_old_connections()

        # Potencia de inversores: SolarView solo la expone por proyecto
        # (GET /solarview/measurements/power/), a diferencia de
        # get_availability(). Se pide en paralelo (mismo patrón que Gaia
        # arriba), solo de día, y para TODAS las plantas con id de SolarView:
        # la comunicación de inversores (`comunicacion.py`) se evalúa también
        # en las que no tienen medidor. Un fallo de la llamada es "ERROR", no
        # una serie vacía: con `get_power` los dos eran None, y una caída de
        # SolarView se leía como 0 kW.
        power_map: dict[int, dict | None] = {}
        if daylight:
            hoy_str = _col_now().strftime("%Y-%m-%d")
            proyectos_runtime = [p for p in proyectos if p.project_id_solarview]

            def _power(p):
                try:
                    estado, datos = sv.get_power_con_estado(
                        int(p.project_id_solarview), hoy_str, hoy_str)
                except Exception:
                    return p.id, "ERROR"
                return p.id, "ERROR" if estado == "error" else datos
            with ThreadPoolExecutor(max_workers=6) as ex:
                for pid, resp in ex.map(_power, proyectos_runtime):
                    power_map[pid] = resp
            close_old_connections()

        if daylight:
            _registrar_comunicacion(proyectos, node_pairs, power_map, snap_map,
                                    gaia_activo=bool(gaia and gaia.enabled))

        for p in proyectos:
            try:
                sv_id = int(p.project_id_solarview)
                node_p, node_r = node_pairs[p.id]
                meter_present = bool(node_p or node_r)

                # ── Dimensión config: fuente única ──────────────────────────────
                _procesar(cache, pending_writes, usuarios, p,
                          "fuente", "fuente_unica" if not meter_present else "ok", {})

                # ── Dimensión runtime: solo de día y con ambas fuentes ──────────
                if not daylight or not meter_present:
                    continue
                # inversores: requiere dato conocido de SolarView para este proyecto
                if sv_id not in avail_map:
                    continue
                cat = (avail_map.get(sv_id) or {}).get("category")
                power_resp = power_map.get(p.id)
                if power_resp == "ERROR":
                    continue  # fallo de red SolarView → no evaluar runtime este ciclo
                inv_power = _latest_inverter_kw(power_resp) or 0.0
                inv_has = cat != "disconnect" and inv_power > ZERO_KW

                snap = snap_map.get(p.id)
                if snap == "ERROR":
                    continue  # fallo de red Gaia → no evaluar runtime este ciclo
                met_kw = _latest_meter_kw(snap)
                met_has = met_kw is not None and met_kw > ZERO_KW

                if not inv_has and not met_has:
                    estado = "sin_datos"
                elif inv_has != met_has:
                    estado = "posible_desconexion"
                else:
                    estado = "ok"

                _procesar(cache, pending_writes, usuarios, p, "runtime", estado, {
                    "inv": round(inv_power, 1),
                    "met": round(met_kw, 1) if met_kw is not None else 0,
                })
            except Exception:
                logger.exception("Error evaluando proyecto %s", p.id)

        guardar_estados(pending_writes)
        logger.info("Alarmas de desconexión evaluadas: %d proyectos", len(proyectos))
    except Exception:
        logger.exception("evaluar_desconexiones falló")
