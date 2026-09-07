"""Correo de notificación de una falla.

Puerto de `_enviar_notificacion` de `app/api/v1/fallas.py`. **Nunca lanza**: la
falla ya está guardada cuando esto corre, y un problema de correo no puede
deshacerla.

El cliente SMTP y la plantilla siguen en `app/services/email_service.py`: es un
módulo sin sesión de base que se mueve cuando se retire FastAPI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apps.clientes.services.contactos import correos as correos_de
from apps.comun.config import settings
from apps.monitoreo.services.fallas.titulo import titulo_falla

logger = logging.getLogger("operaciones.fallas.notificacion")


def enviar_notificacion(falla, accion: str, usuario_nombre: str) -> dict:
    """`{"ok", "enviados", "errores", "sin_correos"}`."""
    from app.services.email_service import send_falla_notification_email

    correos = correos_de("operacional", proyecto_id=falla.proyecto_id)
    ts = datetime.now(timezone.utc).isoformat()

    if not correos:
        logger.warning(
            "[%s] usuario=%s falla=%s accion=%s — SIN correos operacionales para proyecto %s",
            ts, usuario_nombre, falla.codigo_interno, accion, falla.proyecto_id,
        )
        return {
            "ok": False, "enviados": [],
            "errores": ["Sin correos operacionales configurados para este cliente"],
            "sin_correos": True,
        }

    resultado = send_falla_notification_email(
        to_emails=correos,
        codigo_falla=falla.codigo_interno,
        proyecto_nombre=(
            falla.proyecto.nombre_comercial if falla.proyecto_id else str(falla.proyecto_id)
        ),
        descripcion=falla.descripcion or "",
        estado_codigo=falla.estado.codigo if falla.estado_id else "",
        estado_etiqueta=falla.estado.etiqueta if falla.estado_id else "",
        prioridad_etiqueta=falla.prioridad.etiqueta if falla.prioridad_id else "",
        tipo_nombre=titulo_falla(falla),
        fecha_identificacion=str(falla.fecha_identificacion or ""),
        hora_identificacion=str(falla.hora_identificacion or ""),
        fecha_programada=str(falla.fecha_programada or ""),
        registrado_por=usuario_nombre,
        accion=accion,
        proyecto_id=falla.proyecto_id,
    )
    resultado["sin_correos"] = False

    registrar = logger.info if resultado.get("ok") else logger.error
    registrar(
        "[%s] usuario=%s falla=%s accion=%s — %s",
        ts, usuario_nombre, falla.codigo_interno, accion,
        resultado.get("enviados") if resultado.get("ok") else resultado.get("errores"),
    )
    return resultado
