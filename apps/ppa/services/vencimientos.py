"""Alertas proactivas de vencimiento de contratos PPA.

Puerto de `app/jobs/ppa_expiration_checker.py`. Para cada PPA activo cuya
`fecha_fin` cae dentro del horizonte (el mayor umbral de `PPA_ALERT_DAYS`,
90/60/30 días por defecto) se dispara la ventana MÁS AJUSTADA que el contrato ya
cruzó y, si aún no existe la alerta de esa ventana, se crea y se notifica.

**Cruce por umbral (`<=`) y no coincidencia exacta (`==`), a propósito.** Así el
job tolera corridas perdidas —un deploy, una caída— y contratos dados de alta ya
dentro de una ventana. Con `==`, un contrato importado a 45 días nunca
dispararía 90 ni 60, y uno importado a menos de 30 no dispararía NUNCA: el
equipo quedaría a ciegas.

Idempotente: la restricción única `(ppa_id, days_to_expiration)` más la
comprobación previa garantizan que correrlo dos veces no duplica la alerta de
una misma ventana. Un contrato escala de ventana en ventana (60 → 30) creando
una alerta distinta por cada umbral que cruza.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from apps.monitoreo.models import Alerta
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato

logger = logging.getLogger("operaciones.ppa.vencimientos")

TIPO_ALERTA = "PPA_EXPIRING"
UMBRALES_POR_DEFECTO = "90,60,30"


def correos_de_alerta() -> list[str]:
    """Destinatarios de las alertas de vencimiento.

    PPA y Representación/CGM comparten el mismo grupo a propósito (confirmado
    con negocio el 2026-08-25); antes estaba escrito por duplicado en dos
    archivos.
    """
    crudo = os.environ.get("PPA_ALERT_EMAILS", "adhara@unergy.io,jessica@unergy.io")
    return [t.strip() for t in (crudo or "").split(",") if t.strip()]


def umbrales() -> list[int]:
    """'90,60,30' → [90, 60, 30]. Ignora lo vacío y lo que no sea número."""
    crudo = os.environ.get("PPA_ALERT_DAYS") or UMBRALES_POR_DEFECTO
    dias: list[int] = []
    for token in crudo.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            dias.append(int(token))
        except ValueError:
            continue
    return dias


def etiqueta(ppa: PpaContrato) -> str:
    """Nombre legible del contrato para el mensaje."""
    return ppa.nombre_interno or ppa.numero_codigo_contrato or f"PPA #{ppa.id}"


def elegir_umbral(dias: int, disponibles: list[int]) -> int | None:
    """El umbral más ajustado (el menor) que el contrato YA cruzó.

    `dias` va de hoy a `fecha_fin`. Devuelve el menor `T` tal que
    `0 <= dias <= T`, o None si el contrato aún no entra en ninguna ventana o ya
    venció. Elegir el más ajustado evita avisar "faltan 90 días" a un contrato
    que en realidad está a 45.
    """
    if dias < 0:
        return None
    cruzados = [t for t in disponibles if dias <= t]
    return min(cruzados) if cruzados else None


def construir_mensaje(ppa: PpaContrato, proyecto_nombre: str | None, dias: int) -> str:
    partes = [f"Contrato PPA por vencer -- {etiqueta(ppa)}"]
    if ppa.comprador_nombre:
        partes.append(f"Comprador: {ppa.comprador_nombre}")
    if proyecto_nombre:
        partes.append(f"Proyecto: {proyecto_nombre}")
    vence = ppa.fecha_fin.strftime("%d/%m/%Y") if ppa.fecha_fin else "-"
    cuando = "hoy" if dias == 0 else f"en {dias} día{'s' if dias != 1 else ''}"
    partes.append(f"Vence el {vence} ({cuando}).")
    return "\n".join(partes)


def _enviar_correo(mensaje: str, dias: int) -> bool:
    """Manda la alerta. Best-effort: un fallo de envío NO tumba el job — la
    alerta ya quedó persistida antes de llegar acá."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from app.services.email_service import _log_envio, _smtp_send

    destinatarios = correos_de_alerta()
    if not os.environ.get("SMTP_HOST") or not destinatarios:
        return False

    asunto = f"Contrato PPA por vencer en {dias} día{'s' if dias != 1 else ''}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = os.environ.get("SMTP_FROM", "operaciones@unergy.io")
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(mensaje, "plain", "utf-8"))

    filas = [{"email": e, "tipo": "to"} for e in destinatarios]
    try:
        _smtp_send(msg, destinatarios)
        _log_envio(destinatarios=filas, subject=asunto,
                   tipo="alerta_ppa_vencimiento", success=True)
        return True
    except Exception as exc:
        _log_envio(destinatarios=filas, subject=asunto,
                   tipo="alerta_ppa_vencimiento", success=False, error_msg=str(exc))
        return False


def revisar_vencimientos() -> list[int]:
    """Crea y notifica las alertas pendientes. Devuelve los ids creados."""
    disponibles = umbrales()
    if not disponibles:
        return []

    hoy = hoy_col()
    horizonte = hoy + timedelta(days=max(disponibles))

    # Un solo barrido de los contratos dentro del horizonte. `prefetch_related`
    # sobre la M2M para no pagar una consulta por contrato al buscar su proyecto.
    contratos = list(
        PpaContrato.objects.filter(
            fecha_fin__isnull=False, fecha_fin__gte=hoy, fecha_fin__lte=horizonte,
            deleted_at__isnull=True,
        ).prefetch_related("proyectos__proyecto")
    )
    if not contratos:
        return []

    # Las ventanas ya alertadas, de una: la comprobación de idempotencia hacía
    # una consulta por contrato.
    ya_alertadas = set(
        Alerta.objects.filter(ppa_id__in=[c.id for c in contratos])
        .values_list("ppa_id", "days_to_expiration")
    )

    creadas: list[int] = []
    for ppa in contratos:
        dias = (ppa.fecha_fin - hoy).days
        umbral = elegir_umbral(dias, disponibles)
        if umbral is None or (ppa.id, umbral) in ya_alertadas:
            continue

        # Un PPA se vincula a 0..N proyectos; se toma el primero si existe.
        vinculos = list(ppa.proyectos.all())
        proyecto = vinculos[0].proyecto if vinculos else None
        if proyecto is None:
            logger.warning(
                "PPA %s (%s) sin ningún proyecto vinculado — la alerta se crea "
                "con project_id NULL", ppa.id, etiqueta(ppa),
            )

        mensaje = construir_mensaje(ppa, proyecto.nombre_comercial if proyecto else None, dias)
        alerta = Alerta.objects.create(
            ppa_id=ppa.id,
            project_id=proyecto.id if proyecto else None,
            alert_type=TIPO_ALERTA,
            description=mensaje,
            due_date=ppa.fecha_fin,
            days_to_expiration=umbral,
            status="new",
        )
        creadas.append(alerta.id)
        _enviar_correo(mensaje, umbral)

    return creadas
