"""Los KPIs del dashboard — una consulta por métrica, seis dominios.

Vive en `api/` y no en un dominio a propósito: ninguna de estas cifras la
necesita nadie más, y la composición de las seis áreas solo tiene sentido como
esta pantalla. Un servicio de dominio que devolviera "el dashboard" sería un
servicio que no puede reusar nadie.

**Cada bloque va en su propio try/except.** Es como está hoy y no es descuido:
varias métricas leen tablas sin modelo o servicios externos, y una que falle no
debe tumbar el resto del tablero — la pantalla muestra el hueco y lo demás
sigue. La alternativa (un 500) deja al operador sin ningún dato.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from django.db.models import Count, Sum

from apps.clientes import models as cl_models
from apps.liquidaciones import models as lq_models
from apps.monitoreo import models as mo_models
from apps.ppa import models as ppa_models
from apps.proyectos import models as py_models

logger = logging.getLogger("operaciones.dashboard")

# Falla viva = su estado no es final y no está borrada. Sin el filtro de
# `deleted_at` las fallas soft-borradas seguían contando como abiertas e
# inflaban el KPI (arreglo del 2026-08-19).
def _fallas_vivas():
    return mo_models.Falla.objects.filter(
        estado__es_estado_final=False, deleted_at__isnull=True
    )


def kpis() -> dict:
    ahora = datetime.now(timezone.utc)
    inicio_mes = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hoy = date.today()

    datos = {
        "proyectos_total": py_models.Proyecto.objects.filter(
            deleted_at__isnull=True).count(),
        "proyectos_operacion": py_models.Proyecto.objects.filter(
            estado="en_operacion", deleted_at__isnull=True).count(),
        "clientes_total": cl_models.Cliente.objects.filter(
            deleted_at__isnull=True).count(),
        "fallas_abiertas": _fallas_vivas().count(),
        "liquidaciones_mes": lq_models.Liquidacion.objects.filter(
            created_at__gte=inicio_mes, deleted_at__isnull=True).count(),
        "ppa_activos": ppa_models.PpaContrato.objects.filter(
            deleted_at__isnull=True).count(),
    }

    kwh = py_models.GeneracionDiaria.objects.filter(
        fecha__gte=inicio_mes.date()
    ).aggregate(total=Sum("kwh_real"))["total"]
    datos["mwh_mes"] = round(float(kwh) / 1000, 1) if kwh else 0

    datos.update(_alarmas_mgs())
    datos.update(_desglose_de_fallas(hoy))
    datos.update(_compromisos_ppa(hoy))
    datos["liquidaciones_pendientes"] = _liquidaciones_pendientes(hoy)
    datos["precio_bolsa_cop_kwh"] = _precio_bolsa()
    return datos


def _alarmas_mgs() -> dict:
    try:
        vivas = mo_models.AlarmaMonitoreo.objects.filter(resolved_at__isnull=True)
        return {
            "alarmas_mgs": vivas.count(),
            "alarmas_mgs_criticas": vivas.filter(severity="CRITICAL").count(),
        }
    except Exception:
        logger.debug("conteos de alarmas_monitoreo no disponibles", exc_info=True)
        return {"alarmas_mgs": 0, "alarmas_mgs_criticas": 0}


def _desglose_de_fallas(hoy: date) -> dict:
    salida = {"fallas_por_prioridad": {}, "fallas_criticas_antiguas": 0}
    try:
        salida["fallas_por_prioridad"] = {
            fila["prioridad__codigo"]: fila["n"]
            for fila in _fallas_vivas().values("prioridad__codigo")
            .annotate(n=Count("id"))
            if fila["prioridad__codigo"]
        }
    except Exception:
        logger.debug("fallas_por_prioridad no disponible", exc_info=True)
    try:
        # Críticas que llevan más de una semana abiertas: el indicador de que
        # algo se quedó sin dueño.
        salida["fallas_criticas_antiguas"] = _fallas_vivas().filter(
            prioridad__codigo="critica",
            fecha_identificacion__lte=hoy - timedelta(days=7),
        ).count()
    except Exception:
        logger.debug("fallas_criticas_antiguas no disponible", exc_info=True)
    return salida


def _compromisos_ppa(hoy: date) -> dict:
    try:
        total = (
            ppa_models.PpaCompromisoEnergia.objects
            .filter(**{"año": hoy.year, "mes": hoy.month})
            .values("contrato_id").distinct().count()
        )
        return {"ppa_con_compromisos": total}
    except Exception:
        logger.debug("ppa_con_compromisos no disponible", exc_info=True)
        return {"ppa_con_compromisos": 0}


def _liquidaciones_pendientes(hoy: date) -> int:
    """Proyectos en operación que aún no tienen liquidación de este mes."""
    try:
        liquidados = lq_models.Liquidacion.objects.filter(
            periodo=hoy.replace(day=1), deleted_at__isnull=True
        ).values("proyecto_id")
        return py_models.Proyecto.objects.filter(
            estado="en_operacion", deleted_at__isnull=True
        ).exclude(id__in=liquidados).count()
    except Exception:
        logger.debug("liquidaciones_pendientes no disponible", exc_info=True)
        return 0


def _precio_bolsa() -> float | None:
    """Último precio de bolsa. SQL crudo: `precios_bolsa_diario` no tiene modelo.

    Es una de las 10 tablas sin modelo que siguen en uso (ver apps/README.md).
    Cuando se declare el modelo, esto pasa a ORM.
    """
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT precio_promedio FROM precios_bolsa_diario "
                "ORDER BY fecha DESC LIMIT 1"
            )
            fila = cursor.fetchone()
        return round(float(fila[0]), 1) if fila else None
    except Exception:
        logger.debug("precio_bolsa no disponible", exc_info=True)
        return None
