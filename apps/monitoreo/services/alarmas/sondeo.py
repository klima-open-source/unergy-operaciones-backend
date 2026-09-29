"""El ciclo de monitoreo de MGS: evalúa los nodos de Quoia y persiste alarmas.

Puerto de `app/services/mgs/scheduler.py`. El motor (`AlarmEngine`) y los
clientes (`GaiaClient`, `SoleniumClient`, `SoleniumChecker`) se reusan de
`app/services/mgs/` tal cual: no tocan la base y no saben de framework. Lo que
vive acá es lo que sí la toca — resolver qué proyecto es cada nodo, guardar las
alarmas y cerrar las que se superaron.

**El estado del motor vive en el proceso.** `AlarmEngine` recuerda entre ciclos
qué alarmas están activas y en qué estado quedó cada proyecto, y de ahí sale
cuáles se superaron. En FastAPI eso funcionaba porque el scheduler corría dentro
del único worker web; acá exige que las corridas de `monitoreo.sondeo_mgs` pasen
siempre por el MISMO proceso. Con un solo worker de Celery se cumple. Si algún
día hay varios, este estado tiene que salir a la base o a Redis — el síntoma de
no hacerlo es sutil: las alarmas se siguen creando, pero dejan de cerrarse.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from django.db import transaction

from apps.fronteras.models import Frontera
from apps.monitoreo.models import AlarmaMonitoreo
from apps.plataforma.services.fechas import ahora_col
from apps.proyectos.models import Proyecto

logger = logging.getLogger("operaciones.mgs")

TIPOS_GENERACION = ["generacion", "generacion_consumo"]
TIPOS_MONITOREADOS = ["minigranja", "gd"]

_motor = None
_solenium_checker = None


def _motor_y_checker():
    """El motor y el checker, creados una sola vez por proceso.

    Perezoso a propósito: importarlos arrastra los clientes HTTP, y el arranque
    de Django no tiene por qué pagarlo si nadie va a sondear.
    """
    global _motor, _solenium_checker
    if _motor is None:
        from app.services.mgs.alarm_engine import AlarmEngine
        from app.services.mgs.solenium_checker import SoleniumChecker
        from app.services.mgs.solenium_client import SoleniumClient

        _motor = AlarmEngine()
        _solenium_checker = SoleniumChecker(SoleniumClient())
    return _motor, _solenium_checker


def _resolver_mapa_proyectos(gaia) -> tuple[dict[int, int], dict[int, str]]:
    """Para cada minigranja/GD en operación, sus nodos reales de Quoia.

    Se resuelve por `fronteras.proyecto_id` → `codigo_frontera`, la misma fuente
    de verdad que usa el resto del monitoreo, en vez de adivinar por nombre
    (comparar el nombre del nodo contra `proyectos.nombre_comercial`), que
    fallaba en silencio cuando no calzaba con el patrón esperado — auditoría de
    alarmas del 2026-08-31.

    Solo plantas con servicio de operación contratado: antes entraba cualquier
    minigranja o GD activa sin importar si Unergy la opera, generando alarmas
    para plantas fuera de alcance (decisión de negocio del 2026-09-01).

    Devuelve `(node_id → proyecto_id, proyecto_id → nombre)`. Los proyectos sin
    frontera de generación vinculada quedan fuera y se reportan aparte — antes
    desaparecían sin ningún aviso.
    """
    from app.services.mgs.gaia_client import (
        build_db_proyecto_frt_map, find_gaia_node_pair,
    )

    proyectos = list(
        Proyecto.objects.filter(
            estado="en_operacion", deleted_at__isnull=True,
            tipo_proyecto__in=TIPOS_MONITOREADOS, srv_operacion=True,
        ).values_list("id", "nombre_comercial")
    )
    fronteras = list(
        Frontera.objects.filter(
            tipo_frontera__in=TIPOS_GENERACION, codigo_frontera__isnull=False,
        ).values_list("proyecto_id", "codigo_frontera")
    )
    mapa_frt = build_db_proyecto_frt_map(fronteras)

    nodo_a_proyecto: dict[int, int] = {}
    nombres: dict[int, str] = {}
    sin_vinculo: list[str] = []
    for pid, nombre in proyectos:
        nodo_p, nodo_r = find_gaia_node_pair(
            gaia=gaia, proyecto_id=pid, db_proyecto_frt_map=mapa_frt,
        )
        if nodo_p is None and nodo_r is None:
            sin_vinculo.append(nombre)
            continue
        nombres[pid] = nombre
        for nid in (nodo_p, nodo_r):
            if nid is not None:
                nodo_a_proyecto[nid] = pid

    if sin_vinculo:
        logger.warning(
            "%d proyectos en operación sin frontera de generación vinculada — "
            "sin monitoreo este ciclo: %s",
            len(sin_vinculo), ", ".join(sin_vinculo),
        )
    return nodo_a_proyecto, nombres


def sondear() -> dict:
    """Un ciclo completo de monitoreo. Devuelve el resumen de lo que hizo."""
    from app.services.mgs.gaia_client import GaiaClient

    # Alarmas de desconexión (inversores contra medidor): aislado, un fallo suyo
    # no puede tumbar el ciclo de MGS.
    try:
        from apps.monitoreo.services.alarmas.desconexion import evaluar_desconexiones

        evaluar_desconexiones()
    except Exception:
        logger.exception("evaluar_desconexiones falló (no afecta a MGS)")

    gaia = GaiaClient()
    if not gaia.enabled:
        logger.warning("GAIA_USER/GAIA_PASS sin configurar — monitoreo desactivado")
        return {"omitido": "gaia_sin_credenciales"}

    nodo_a_proyecto, nombres = _resolver_mapa_proyectos(gaia)
    if not nodo_a_proyecto:
        logger.warning("Ningún proyecto resuelto a nodos de Quoia — se omite el ciclo")
        return {"omitido": "sin_proyectos_resueltos"}

    nodos = gaia.get_all_nodes()
    if not nodos:
        logger.warning("Quoia devolvió una lista de nodos vacía")
        return {"omitido": "sin_nodos"}

    motor, checker = _motor_y_checker()

    # Foto ANTES de evaluar, para poder cerrar en la base los tipos que el motor
    # descarta internamente sin emitir una alarma explícita — ver `_superadas`.
    activas_antes = {k: set(v) for k, v in motor.active_alarms.items()}
    alarmas = motor.evaluate(nodos, nodo_a_proyecto, nombres)

    resumen = motor.get_summary(nodos, nodo_a_proyecto, nombres)
    try:
        observaciones = checker.get_inverter_observations(
            [p["name"] for p in resumen.get("projects", [])]
        )
    except Exception:
        logger.exception("La revisión de inversores falló — se sigue sin ella")
        observaciones = {}

    for alarma in alarmas:
        nota = observaciones.get(alarma.proyecto_nombre)
        if nota and alarma.alarm_type.value != "RECUPERACION":
            alarma.details += f" | Inversores: {nota}"

    guardar_alarmas(alarmas, activas_antes, nombres)

    logger.info(
        "ciclo de monitoreo: %d nodos, %d proyectos, %d alarmas, %d observaciones",
        len(nodos), len(nombres), len(alarmas), len(observaciones),
    )
    return {
        "nodos": len(nodos), "proyectos": len(nombres),
        "alarmas": len(alarmas), "observaciones": len(observaciones),
    }


def guardar_alarmas(alarmas, activas_antes: dict | None = None,
                    nombres: dict[int, str] | None = None) -> None:
    if not alarmas and not activas_antes:
        return

    from app.services.mgs.alarm_engine import AlarmType

    try:
        with transaction.atomic():
            AlarmaMonitoreo.objects.bulk_create([
                AlarmaMonitoreo(
                    proyecto_nombre=a.proyecto_nombre,
                    severity=a.severity.value,
                    alarm_type=a.alarm_type.value,
                    details=a.details,
                    source_data=json.loads(json.dumps(asdict(a), default=str)),
                    created_at=a.timestamp,
                    # RECUPERACION es un evento puntual ("volvió la
                    # conectividad"), no una condición en curso: se guarda ya
                    # resuelta para que no cuente como activa en el dashboard.
                    # Antes ninguna fila fijaba `resolved_at`, así que el conteo
                    # de alarmas activas solo crecía y nunca reflejaba el estado
                    # real (auditoría del 2026-08-31).
                    resolved_at=(a.timestamp if a.alarm_type == AlarmType.RECUPERACION
                                 else None),
                )
                for a in alarmas
            ])
    except Exception:
        logger.exception("No se pudieron guardar las alarmas")
        return

    if activas_antes is not None:
        motor, _ = _motor_y_checker()
        try:
            cerrar_superadas(activas_antes, motor.active_alarms,
                             motor.previous_states, nombres or {})
        except Exception:
            logger.exception("No se pudieron cerrar las alarmas superadas")

    # El envío automático de correo a clientes está DESACTIVADO desde el
    # 2026-09-01 por pedido explícito de negocio ("no quiero que se haga el
    # envío automático de clientes, no tengo control"): se disparaba sin ningún
    # filtro para toda alarma CRITICAL o WARNING. Las alarmas se siguen
    # detectando, guardando y mostrando; lo único apagado es el correo. Si se
    # retoma con un control real —opt-in por proyecto, o un digest en vez de un
    # correo por alarma— se reconstruye sobre `send_alarm_notification_email` y
    # `apps.clientes.services.contactos.correos("operacional", proyecto_id=…)`.


def _superadas(antes: dict[int, set], ahora: dict[int, set]) -> dict[int, set]:
    """Qué tipos estaban activos antes de evaluar y ya no lo están.

    Función pura. La condición se superó aunque el motor no haya emitido una
    alarma explícita para avisarlo: con SIN_GENERACION solo descarta el tipo
    cuando la planta vuelve a generar, sin crear ningún evento de recuperación.
    """
    return {
        pid: cerrados
        for pid, previos in antes.items()
        if (cerrados := previos - ahora.get(pid, set()))
    }


def cerrar_superadas(antes: dict[int, set], ahora: dict[int, set],
                     estados_previos: dict[int, str],
                     nombres: dict[int, str]) -> None:
    """Fija `resolved_at` en las condiciones que ya no están activas.

    `alarmas_monitoreo` guarda `proyecto_nombre` y no `proyecto_id`, así que acá
    se traduce de vuelta el id que usa el motor al nombre con el que se
    persistió la fila.
    """
    sellado = ahora_col()

    with transaction.atomic():
        for pid, tipos in _superadas(antes, ahora).items():
            nombre = nombres.get(pid)
            if nombre is None:
                continue
            AlarmaMonitoreo.objects.filter(
                proyecto_nombre=nombre,
                alarm_type__in=[t.value for t in tipos],
                resolved_at__isnull=True,
            ).update(resolved_at=sellado)

        # CORTE_ZONA no vive en `active_alarms`: es un evento derivado que
        # agrupa varios proyectos a la vez, no el estado de uno solo. Se cierra
        # cuando ninguno de los proyectos que lista en su nombre (unidos por
        # coma) sigue en NO_DATA o ERROR.
        estado_por_nombre = {
            nombre: estados_previos.get(pid) for pid, nombre in nombres.items()
        }
        zonales = AlarmaMonitoreo.objects.filter(
            alarm_type="CORTE_ZONA", resolved_at__isnull=True,
        ).values_list("id", "proyecto_nombre")
        cerrables = [
            aid for aid, nombre in zonales
            if not any(
                estado_por_nombre.get(m.strip()) in ("NO_DATA", "ERROR")
                for m in nombre.split(",")
            )
        ]
        if cerrables:
            AlarmaMonitoreo.objects.filter(id__in=cerrables).update(
                resolved_at=sellado)


# ── Creación y cierre automático de fallas: ELIMINADO (2026-09-02) ───────────
# Decisión de negocio: las fallas se registran solo a mano desde la plataforma o
# por el integrador externo vía POST /fallas. El monitoreo ya no las crea ni las
# cierra solo. Lo que se quitó, con el mapeo que usaba (conocimiento de negocio,
# no solo código):
#     PLANTA_CAIDA    -> "9.1"  Sin suministro eléctrico
#     SIN_GENERACION  -> "4.6"  Inversor con derating o eficiencia reducida
#     CORTE_ZONA      -> "9.1"  Sin suministro eléctrico
# `fallas.alarma_monitoreo_id` se conserva: distingue las fallas históricas que
# creó este motor, y es la única señal no falsificable de "origen automático".
# El punto de enganche, si se retoma, es `guardar_alarmas`.
