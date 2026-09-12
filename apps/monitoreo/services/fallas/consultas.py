"""Las consultas de fallas: actividad del día, filtros del listado y backfill.

`sla_dashboard` y `stats_resumen` vivian aca y **se retiraron el 2026-09-08**:
calculaban numeros que ninguna pantalla mostraba. El bug 4 de la revision de
Fallas --contaban las fallas borradas-- estaba en `stats_resumen`, o sea que se
arreglo un contador que nadie miraba; ese fue el argumento para retirarlos.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from django.db.models import Count, Q, QuerySet

from apps.monitoreo.models import (
    Falla, FallaCatEstado, FallaSeguimiento,
)
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import ProyectoInversionista

from .dominio import (
    BOT_DESCONEXION_CATEGORIA, BOT_DESCONEXION_SUBTIPO,
    _COL_TZ, limite_sla, sla_limite_horas_efectivo,
)

# Lo que la tabla y el "hero" del drawer muestran de entrada. NO incluye
# seguimientos ni inversores: eso solo hace falta al abrir el detalle de UNA
# falla, no en cada fila de un listado de cientos.
RELACIONES_LISTA = (
    "proyecto", "tipo__categoria", "estado", "prioridad", "resolucion",
    "registrado_por",
)
RELACIONES_DETALLE = ("inversores_afectados",
                      "seguimientos__usuario", "seguimientos__estado_nuevo")


def base_lista():
    return (
        Falla.objects
        .filter(deleted_at__isnull=True)
        .select_related(*RELACIONES_LISTA)
        # `tiempo_afectacion_horas` del serializer de LISTA lee
        # `falla.intervalos`: sin esto era una query por fila (hasta 5000).
        .prefetch_related("intervalos")
    )


def base_detalle():
    return base_lista().prefetch_related(*RELACIONES_DETALLE)


def dia_col_en_utc() -> datetime:
    """El inicio del día actual en hora de Colombia, expresado en UTC."""
    ahora_col = datetime.now(_COL_TZ)
    return ahora_col.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def actividad_hoy() -> tuple[str, list[Falla], list[dict], dict[int, Falla]]:
    """`(fecha, creadas, cambios_de_estado, fallas_por_id)` del día en hora Colombia.

    El `estado_anterior` se deduce del seguimiento de cambio inmediatamente
    previo, no de una columna: no existe historial de estado fuera de
    `fallas_seguimientos`.
    """
    inicio_dia = dia_col_en_utc()
    hoy_str = datetime.now(_COL_TZ).date().isoformat()

    creadas = list(base_detalle().filter(created_at__gte=inicio_dia).order_by("-created_at"))

    ids_con_cambio = set(
        FallaSeguimiento.objects
        .filter(estado_nuevo_id__isnull=False, created_at__gte=inicio_dia)
        .values_list("falla_id", flat=True)
    )
    cambios: list[dict] = []
    fallas_map: dict[int, Falla] = {}
    if ids_con_cambio:
        historial = (
            FallaSeguimiento.objects
            .filter(falla_id__in=ids_con_cambio, estado_nuevo_id__isnull=False)
            .select_related("estado_nuevo")
            .order_by("falla_id", "created_at")
        )
        por_falla: dict[int, list] = {}
        for s in historial:
            por_falla.setdefault(s.falla_id, []).append(s)

        fallas_map = {f.id: f for f in base_detalle().filter(id__in=ids_con_cambio)}

        def _estado(estado):
            if not estado:
                return None
            return {"codigo": estado.codigo, "etiqueta": estado.etiqueta}

        for fid, segs in por_falla.items():
            if fid not in fallas_map:
                continue
            posiciones_hoy = [i for i, s in enumerate(segs) if s.created_at >= inicio_dia]
            if not posiciones_hoy:
                continue
            i = posiciones_hoy[-1]
            cambios.append({
                "falla_id": fid,
                "estado_anterior": _estado(segs[i - 1].estado_nuevo if i > 0 else None),
                "estado_nuevo": _estado(segs[i].estado_nuevo),
                "hora": segs[i].created_at.isoformat(),
            })
        cambios.sort(key=lambda c: c["hora"], reverse=True)

    return hoy_str, creadas, cambios, fallas_map


def filtrar(params) -> QuerySet:
    """Aplica los filtros de `GET /fallas` sobre el listado base."""
    qs = base_lista()

    buscar = params.get("q") or params.get("buscar")
    if buscar:
        qs = qs.filter(
            Q(descripcion__icontains=buscar) | Q(codigo_interno__icontains=buscar)
        )
    for campo, clave in (
        ("estado_id", "estado_id"), ("prioridad_id", "prioridad_id"),
        ("proyecto_id", "proyecto_id"),
    ):
        if params.get(clave):
            qs = qs.filter(**{campo: params[clave]})
    for lookup, clave in (
        ("estado__codigo", "estado_codigo"),
        ("prioridad__codigo", "prioridad_codigo"),
        ("tipo__codigo", "tipo_codigo"),
    ):
        if params.get(clave):
            qs = qs.filter(**{lookup: params[clave]})

    if params.get("cliente_id"):
        # Proyectos donde el cliente es inversionista VIGENTE (fin nulo o futuro).
        hoy = hoy_col()
        proyectos = (
            ProyectoInversionista.objects
            .filter(cliente_id=params["cliente_id"])
            .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
            .values("proyecto_id")
        )
        qs = qs.filter(proyecto_id__in=proyectos)

    if params.get("activa_en_fecha"):
        # "Activa a la fecha X", no "activa ahora mismo": en el detalle de un día
        # ya clasificado interesa qué estaba abierto ENTONCES. `fecha_resolucion`
        # nula solo cuenta como "sigue abierta" si el estado REAL tampoco es
        # final — si no, una falla cerrada hace meses sin fecha_resolucion (dato
        # legacy) colaba como activa para cualquier fecha consultada.
        fecha = params["activa_en_fecha"]
        qs = qs.filter(fecha_identificacion__lte=fecha).filter(
            (Q(fecha_resolucion__isnull=True) & Q(estado__es_estado_final=False))
            | Q(fecha_resolucion__date__gte=fecha)
        )
    if params.get("solo_activas"):
        qs = qs.filter(estado__es_estado_final=False)
    if params.get("solo_alerta"):
        qs = qs.filter(
            estado__es_estado_final=False,
            fecha_identificacion__lte=hoy_col() - timedelta(days=7),
        )
    # El rango sobre CUANDO SE IDENTIFICO la falla, que es la fecha por la que
    # se mira un periodo hacia atras ("que paso en agosto"). Distinto de
    # `fecha_programada_*`, que es cuando se planeo atenderla, y de
    # `activa_en_fecha`, que pregunta que estaba abierto en un dia.
    #
    # Faltaba, y por eso el Historico de Generacion se traia el historial
    # COMPLETO --6.400 fallas en 33 peticiones-- para despues filtrarlo por
    # fecha en el navegador y mostrar las de un mes.
    if params.get("fecha_identificacion_desde"):
        qs = qs.filter(fecha_identificacion__gte=params["fecha_identificacion_desde"])
    if params.get("fecha_identificacion_hasta"):
        qs = qs.filter(fecha_identificacion__lte=params["fecha_identificacion_hasta"])

    if params.get("fecha_programada_desde"):
        qs = qs.filter(fecha_programada__gte=params["fecha_programada_desde"])
    if params.get("fecha_programada_hasta"):
        qs = qs.filter(fecha_programada__lte=params["fecha_programada_hasta"])
    if params.get("con_fecha_programada"):
        qs = qs.filter(fecha_programada__isnull=False)

    pendiente = params.get("pendiente_reclasificar")
    if pendiente is not None:
        qs = qs.filter(pendiente_reclasificar=pendiente)
        if pendiente:
            # La cola de pendientes REALES excluye el patrón del bot externo: sus
            # fallas nunca se resuelven por el flujo normal de reclasificación
            # (833 de 851 casos reales). Ver `dominio.es_patron_bot_externo`.
            qs = qs.exclude(
                alarma_monitoreo_id__isnull=True,
                categoria_codigo=BOT_DESCONEXION_CATEGORIA,
                subtipo_codigo=BOT_DESCONEXION_SUBTIPO,
            )
    return qs.order_by("-created_at")


def backfill_sla_cumplido(dry_run: bool = False) -> dict:
    """Recalcula `sla_cumplido` para TODAS las fallas resueltas.

    Incluye las que ya tenían un valor manual: ese subconjunto era justo el sesgo
    que desaparece al volver el campo 100 % calculado. Idempotente — solo cuenta
    como corregida si el valor cambia.
    """
    fallas = list(
        Falla.objects
        .filter(
            deleted_at__isnull=True,
            estado__es_estado_final=True,
            fecha_resolucion__isnull=False,
        )
        .select_related("prioridad")
    )
    cambiadas, a_guardar = [], []
    for f in fallas:
        anterior = f.sla_cumplido
        f.sla_cumplido = f.fecha_resolucion <= limite_sla(f)
        if f.sla_cumplido != anterior:
            cambiadas.append({
                "codigo": f.codigo_interno,
                "sla_cumplido_anterior": anterior,
                "sla_cumplido_nuevo": f.sla_cumplido,
            })
            a_guardar.append(f)

    if not dry_run and a_guardar:
        Falla.objects.bulk_update(a_guardar, ["sla_cumplido"])

    return {
        "dry_run": dry_run,
        "total_resueltas": len(fallas),
        "corregidas": len(cambiadas),
        "detalle": cambiadas[:200],
    }
