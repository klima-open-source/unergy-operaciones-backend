"""Avisos internos de fallas: la campana de notificaciones de la plataforma.

No son los correos a los clientes (`notificacion.py`): estos van al equipo de
Operaciones.
"""

from __future__ import annotations

from datetime import datetime, time

from apps.monitoreo.models import Falla
from apps.plataforma.models import Notificacion, Rol, Usuario
from apps.plataforma.services.fechas import COL_TZ, hoy_col

TITULO_PROGRAMADA_HOY = "Falla programada para hoy"


def _coordinadores_ids() -> set[int]:
    return set(
        Usuario.objects.filter(rol=Rol.COORDINADOR, activo=True).values_list("id", flat=True)
    )


def _resumen(falla: Falla) -> str:
    nombre = falla.proyecto.nombre_comercial if falla.proyecto_id else f"Proyecto {falla.proyecto_id}"
    return f"{falla.codigo_interno} — {nombre}: {(falla.descripcion or '')[:80]}"


def falla_registrada(falla: Falla) -> None:
    """Avisa a los coordinadores que se registró una falla nueva."""
    mensaje = _resumen(falla)
    Notificacion.objects.bulk_create([
        Notificacion(
            usuario_id=uid, tipo="accion", titulo="Nueva falla registrada",
            mensaje=mensaje, leida=False,
        )
        for uid in _coordinadores_ids()
    ])


def programadas_hoy() -> int:
    """Avisa a coordinadores y a quien la registró de cada falla programada para hoy.

    Idempotente: si la tarea se reintenta el mismo día, no duplica avisos. Devuelve
    cuántos avisos nuevos creó.
    """
    hoy = hoy_col()
    fallas = (
        Falla.objects
        .filter(deleted_at__isnull=True, fecha_programada=hoy, estado__codigo="programado")
        .select_related("proyecto")
    )
    coordinadores = _coordinadores_ids()
    inicio_dia = datetime.combine(hoy, time(0, 0), tzinfo=COL_TZ)
    ya_avisados = set(
        Notificacion.objects
        .filter(titulo=TITULO_PROGRAMADA_HOY, created_at__gte=inicio_dia)
        .values_list("usuario_id", "mensaje")
    )

    nuevos = []
    for falla in fallas:
        mensaje = _resumen(falla)
        for uid in coordinadores | {falla.registrado_por_id}:
            if (uid, mensaje) not in ya_avisados:
                nuevos.append(Notificacion(
                    usuario_id=uid, tipo="alerta", titulo=TITULO_PROGRAMADA_HOY,
                    mensaje=mensaje, leida=False,
                ))
    Notificacion.objects.bulk_create(nuevos)
    return len(nuevos)
