"""
Servicio de email — informes aprobados, alarmas, reset password, reporte CGM.
"""
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger("email_service")

_LOGO_UNERGY = Path(__file__).resolve().parent.parent / "assets" / "logo_unergy.png"

_REPORTE_CGM_TEXTO = (
    "Cordial Saludo,\n\n"
    "Por medio del presente correo remitimos el reporte de mediciones CGM correspondiente "
    "{fecha_frase}, reportado al ASIC.\n\n"
    "{nota_mensual}"
    "Quedamos atentos a cualquier observación al respecto.\n\n"
    "Atentamente,\n\n"
    "--\n"
    "Operaciones Unergy\n"
    "Unergy Energia Digital SAS E.S.P — Empresa de Servicios Públicos\n"
    "operaciones@unergy.io — Cl. 46 #70a 65 Laureles, Medellín\n\n"
    "Este documento y los archivos que lo acompañan han sido elaborados exclusivamente para la persona "
    "o entidad a la que van dirigidos y pueden contener información de carácter reservado, confidencial "
    "o legalmente protegida. En caso de haber recibido este mensaje por error, le solicitamos notificarlo "
    "de manera inmediata al remitente y proceder con la eliminación total del documento y de cualquier "
    "anexo. Queda expresamente prohibido cualquier acceso no autorizado, uso indebido, conservación, "
    "difusión, reproducción, copia, modificación o redistribución, total o parcial, de su contenido, sin "
    "autorización expresa, pudiendo acarrear responsabilidades legales conforme a la normativa aplicable. "
    "Agradecemos su comprensión. Unergy Energía Digital S.A.S. E.S.P. Empresa de Servicios Públicos de "
    "Energía"
)

_REPORTE_CGM_HTML = """\
<div style="font-family: Arial, sans-serif; font-size: 14px; color:#222;">
  <p>Cordial Saludo,</p>
  <p>Por medio del presente correo remitimos el reporte de mediciones CGM correspondiente
  {fecha_frase}, reportado al ASIC.</p>
  {nota_mensual}
  <p>Quedamos atentos a cualquier observación al respecto.</p>
  <p>Atentamente,</p>
  <br>
  <div>--</div>
  <table cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-family: Arial, sans-serif;">
    <tr>
      <td style="vertical-align:top; padding-right:18px;">
        <img src="cid:logo_unergy" width="100" alt="Unergy" style="display:block; margin-bottom:4px;">
        <div style="font-size:11px; color:#333;">Unergy Energia Digital SAS E.S.P</div>
        <div style="font-size:11px; font-weight:bold; color:#333;">Empresa de Servicios Públicos</div>
      </td>
      <td style="vertical-align:top; padding:0 18px; border-left:1px solid #ddd;">
        <div style="font-weight:bold; font-size:16px; color:#222;">Operaciones Unergy</div>
        <div style="width:36px; height:4px; background-color:#8B5CF6; border-radius:2px; margin:6px 0;"></div>
        <div style="font-style:italic; font-size:12px; color:#555; max-width:240px;">
          "Sumemos esfuerzos para que la energía del futuro sea sostenible y accesible para todos"
        </div>
      </td>
      <td style="vertical-align:top; padding-left:18px; border-left:1px solid #ddd; font-size:12px; color:#333;">
        <div>operaciones@unergy.io</div>
        <div>Cl. 46 #70a 65 Laureles, Medellín</div>
      </td>
    </tr>
  </table>
  <p style="font-size:10px; color:#888; margin-top:16px;">
    Este documento y los archivos que lo acompañan han sido elaborados exclusivamente para la persona o
    entidad a la que van dirigidos y pueden contener información de carácter reservado, confidencial o
    legalmente protegida. En caso de haber recibido este mensaje por error, le solicitamos notificarlo de
    manera inmediata al remitente y proceder con la eliminación total del documento y de cualquier anexo.
    Queda expresamente prohibido cualquier acceso no autorizado, uso indebido, conservación, difusión,
    reproducción, copia, modificación o redistribución, total o parcial, de su contenido, sin autorización
    expresa, pudiendo acarrear responsabilidades legales conforme a la normativa aplicable. Agradecemos su
    comprensión. Unergy Energía Digital S.A.S. E.S.P. Empresa de Servicios Públicos de Energía
  </p>
</div>
"""


def _alertar_fallo_envio(*, tipo: str, destinatario: str, error: str) -> None:
    """Avisa por correo cuando un envío falla -- best effort: si la cuenta SMTP
    está caída (la causa más común de fallo, ver incidente 2026-08-29/30), este
    mismo aviso puede fallar también. En ese caso queda al menos en los logs
    del servidor, que es lo único que sigue funcionando cuando Gmail rechaza
    las credenciales."""
    if not settings.ALERTA_FALLOS_EMAIL or not settings.SMTP_HOST:
        return

    subject = f"⚠️ Fallo de envío de correo — {tipo}"
    body = (
        f"Un envío de tipo '{tipo}' falló.\n\n"
        f"Destinatario: {destinatario}\n"
        f"Error: {error}\n\n"
        "Revisa la tabla email_envios para el detalle completo."
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = settings.ALERTA_FALLOS_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        _smtp_send(msg, [settings.ALERTA_FALLOS_EMAIL])
    except Exception as exc:
        print(
            f"[ALERTA_FALLO_ENVIO] No se pudo notificar el fallo de '{tipo}' "
            f"hacia {destinatario} -- el envío de la alerta también falló: {exc}"
        )


def _log_envio(
    *,
    destinatarios: list[dict],
    subject: str,
    tipo: str,
    success: bool,
    error_msg: str | None = None,
    proyectos: list[str] | None = None,
    proyectos_total: int | None = None,
    cliente_id: int | None = None,
    operador_red_id: int | None = None,
    proyecto_id: int | None = None,
) -> None:
    """Log an email send EVENT to the database (fire-and-forget): una fila en
    email_envios (el evento) + una fila por destinatario real en
    email_envio_destinatarios (auditoría 2026-09-01).

    Antes, `_log_send()` insertaba una fila completa en email_envios POR CADA
    destinatario -- si un operador tenía 3 contactos configurados, el
    historial mostraba 3 "Enviado" idénticos aunque el correo real por SMTP
    se mandó una sola vez (ver Reporte CGM). Ahora el evento es una sola fila
    y los destinatarios reales viven en la tabla hija.

    destinatarios: [{"email": str, "tipo": "to"|"cc"|"cco", "exitoso"?: bool,
    "error"?: str}] -- "exitoso"/"error" ausentes heredan success/error_msg
    del evento (caso normal: un solo envío SMTP para todos). Se pueden pasar
    con su propio resultado cuando el envío real no fue uno solo para todos
    (ver send_falla_notification_email, que manda un SMTP separado por
    persona y cada uno puede fallar independiente).

    cliente_id/operador_red_id/proyecto_id: FKs reales opcionales (auditoría
    2026-08-26) -- antes email_envios no tenía ninguna, aunque cada llamador
    ya resolvía el id correspondiente antes de loguear. Solo uno (o ninguno)
    aplica según el tipo de envío -- no es un vínculo polimórfico real a
    nivel de BD, son tres columnas nullable independientes."""
    for d in destinatarios:
        if not d.get("exitoso", success):
            _alertar_fallo_envio(
                tipo=tipo, destinatario=d["email"],
                error=d.get("error") or error_msg or "error desconocido",
            )

    try:
        from app.core.database import SessionLocal
        from sqlalchemy import text as sa_text
        db = SessionLocal()
        try:
            envio_id = db.execute(sa_text("""
                INSERT INTO email_envios
                    (asunto, tipo, exitoso, error, enviado_at, proyectos, proyectos_total,
                     cliente_id, operador_red_id, proyecto_id)
                VALUES (:subject, :tipo, :ok, :err, :ts, :proyectos, :proyectos_total,
                        :cliente_id, :operador_red_id, :proyecto_id)
                RETURNING id
            """), {
                "subject": subject,
                "tipo": tipo,
                "ok": success,
                "err": error_msg,
                "ts": datetime.now(timezone.utc),
                "proyectos": ",".join(proyectos) if proyectos else None,
                "proyectos_total": proyectos_total,
                "cliente_id": cliente_id,
                "operador_red_id": operador_red_id,
                "proyecto_id": proyecto_id,
            }).scalar_one()

            for d in destinatarios:
                db.execute(sa_text("""
                    INSERT INTO email_envio_destinatarios (envio_id, email, tipo_destinatario, exitoso, error)
                    VALUES (:envio_id, :email, :tipo_dest, :ok, :err)
                """), {
                    "envio_id": envio_id,
                    "email": d["email"],
                    "tipo_dest": d.get("tipo", "to"),
                    "ok": d.get("exitoso", success),
                    "err": d.get("error", error_msg),
                })
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("Failed to log email send: %s", e)
        finally:
            db.close()
    except Exception:
        pass


def _smtp_send(msg: MIMEMultipart, recipients: list[str]) -> None:
    """Send an email via SMTP with TLS."""
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.sendmail(settings.SMTP_FROM, recipients, msg.as_string())


def send_reset_password_email(*, to_email: str, token: str) -> None:
    """
    Envía el enlace de restablecimiento de contraseña al correo indicado.
    Si SMTP no está configurado, imprime el token en los logs del servidor.
    """
    # El token va en el PATH, no en el query. Las dos generaciones del frontend
    # declaran la ruta con el token como segmento --el legacy en
    # `src/router/index.js` (`/reset-password/:token`) y el Nuxt en
    # `app/pages/reset-password/[token]/index.vue`, que lo lee de
    # `route.params.token`--, asi que con `?token=` no hay ruta que empareje: la
    # peticion cae en el catch-all, que redirige a `/dashboard`, y el usuario
    # termina en el login sin haber podido cambiar la clave.
    # El token es un `uuid4().hex` (32 caracteres hex), o sea que viaja en un
    # segmento de ruta sin escapar nada.
    reset_url = f"{settings.FRONTEND_URL}/reset-password/{token}"

    if not settings.SMTP_HOST:
        print(f"[RESET] Token para {to_email}: {token}  (SMTP no configurado — solo en logs)")
        print(f"[RESET] URL: {reset_url}")
        return

    subject = "Restablecer contraseña — Monitoreo Unergy"
    body_html = f"""
<html>
<body style="font-family:Arial,sans-serif;color:#1A0F2E;max-width:480px;margin:0 auto;padding:0">
  <div style="background:#1A0F2E;padding:24px 28px;border-radius:10px 10px 0 0">
    <div style="color:#F6FF72;font-size:20px;font-weight:800;letter-spacing:1px">UNERGY</div>
    <div style="color:#6B5F80;font-size:11px;letter-spacing:.8px;text-transform:uppercase;margin-top:2px">Restablecer contraseña</div>
  </div>
  <div style="background:#F7F4FD;padding:28px;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 10px 10px">
    <p style="margin:0 0 20px">Recibimos una solicitud para restablecer tu contraseña. Haz clic en el siguiente enlace:</p>
    <div style="text-align:center;margin:0 0 20px">
      <a href="{reset_url}" style="background:#915BD8;color:#fff;padding:14px 28px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block">Restablecer contraseña</a>
    </div>
    <p style="color:#6B5F80;font-size:12px;margin:0">
      Este enlace es válido por 1 hora.<br>
      Si no solicitaste este cambio, ignora este correo.<br>
      Contacto: <a href="mailto:operaciones@unergy.io" style="color:#915BD8">operaciones@unergy.io</a>
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_email
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        _smtp_send(msg, [to_email])
        _log_envio(destinatarios=[{"email": to_email, "tipo": "to"}], subject=subject, tipo="reset_password", success=True)
    except Exception as exc:
        print(f"[RESET] Error enviando email a {to_email}: {exc}")
        _log_envio(destinatarios=[{"email": to_email, "tipo": "to"}], subject=subject, tipo="reset_password", success=False, error_msg=str(exc))
        raise RuntimeError(f"No se pudo enviar el email: {exc}") from exc


def send_informe_email(
    *,
    to_emails: list[str],
    cc: list[str] | None = None,
    proyecto_nombre: str,
    periodo_display: str,
    aprobado_por: str,
    html_content: str,
    proyecto_id: int | None = None,
) -> None:
    """
    Envía el informe como email HTML con el contenido embebido.
    Lanza RuntimeError si SMTP no está configurado o falla el envío.
    """
    if not settings.SMTP_HOST:
        raise RuntimeError(
            "SMTP no configurado. Define SMTP_HOST, SMTP_PORT, SMTP_USER, "
            "SMTP_PASSWORD y SMTP_FROM en las variables de entorno."
        )

    subject = f"Informe Operacional — {proyecto_nombre} — {periodo_display}"

    body_html = f"""
<html>
<body style="font-family:Arial,sans-serif;color:#1A0F2E;max-width:720px;margin:0 auto;padding:0">
  <div style="background:#1A0F2E;padding:24px 28px;border-radius:10px 10px 0 0">
    <div style="color:#F6FF72;font-size:20px;font-weight:800;letter-spacing:1px">UNERGY</div>
    <div style="color:#6B5F80;font-size:11px;letter-spacing:.8px;text-transform:uppercase;margin-top:2px">Informe Operacional</div>
  </div>
  <div style="background:#F7F4FD;padding:24px 28px;border:1px solid #EDE8F5;border-top:none">
    <p style="margin:0 0 16px">Estimado cliente,</p>
    <p style="margin:0 0 16px">
      A continuación encontrará el <strong>Informe Operacional de {proyecto_nombre}</strong>
      correspondiente al período <strong>{periodo_display}</strong>.
    </p>
    <div style="background:#fff;border:1px solid #EDE8F5;border-radius:8px;padding:14px 18px;margin:20px 0">
      <div style="font-size:11px;font-weight:700;color:#A89EC0;letter-spacing:.7px;text-transform:uppercase;margin-bottom:6px">APROBADO POR</div>
      <div style="font-size:14px;font-weight:700;color:#1A0F2E">{aprobado_por}</div>
    </div>
  </div>
  <div style="padding:28px;background:#fff;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 10px 10px">
    {html_content}
  </div>
  <div style="padding:16px 28px;text-align:center">
    <p style="color:#6B5F80;font-size:12px;margin:0">
      Cualquier consulta, escríbenos a
      <a href="mailto:operaciones@unergy.io" style="color:#915BD8">operaciones@unergy.io</a>
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    recipients = list(to_emails)
    if cc:
        recipients.extend(cc)

    destinatarios = [{"email": e, "tipo": "to"} for e in to_emails] + [{"email": e, "tipo": "cc"} for e in (cc or [])]

    try:
        _smtp_send(msg, recipients)
        _log_envio(destinatarios=destinatarios, subject=subject, tipo="informe", success=True, proyecto_id=proyecto_id)
    except Exception as exc:
        _log_envio(destinatarios=destinatarios, subject=subject, tipo="informe", success=False,
                   error_msg=str(exc), proyecto_id=proyecto_id)
        raise


def send_alarm_notification_email(
    *,
    to_emails: list[str],
    proyecto_nombre: str,
    alarm_type: str,
    severity: str,
    details: str,
) -> None:
    """
    Send email notification for critical MGS alarms.
    Falls back silently if SMTP is not configured (logs to console).
    """
    if not settings.SMTP_HOST:
        print(
            f"[ALARM_EMAIL] SMTP not configured — alarm for {proyecto_nombre}: "
            f"{alarm_type} ({severity}) — {details}"
        )
        return

    severity_color = {"CRITICAL": "#FF3B30", "WARNING": "#FF9500", "INFO": "#34C759"}.get(severity, "#8E8E93")
    severity_label = {"CRITICAL": "CRITICA", "WARNING": "ADVERTENCIA", "INFO": "INFORMACION"}.get(severity, severity)

    subject = f"[{severity_label}] Alarma {alarm_type} — {proyecto_nombre}"
    body_html = f"""
<html>
<body style="font-family:Arial,sans-serif;color:#1A0F2E;max-width:560px;margin:0 auto;padding:0">
  <div style="background:#1A0F2E;padding:24px 28px;border-radius:10px 10px 0 0">
    <div style="color:#F6FF72;font-size:20px;font-weight:800;letter-spacing:1px">UNERGY</div>
    <div style="color:#6B5F80;font-size:11px;letter-spacing:.8px;text-transform:uppercase;margin-top:2px">Alerta de Monitoreo</div>
  </div>
  <div style="background:#F7F4FD;padding:24px 28px;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 10px 10px">
    <div style="background:{severity_color};color:#fff;display:inline-block;padding:4px 12px;border-radius:4px;font-size:12px;font-weight:700;letter-spacing:.5px;margin-bottom:16px">{severity_label}</div>
    <h2 style="margin:0 0 8px;font-size:18px">{proyecto_nombre}</h2>
    <p style="margin:0 0 16px;color:#6B5F80"><strong>Tipo:</strong> {alarm_type}</p>
    <div style="background:#fff;border:1px solid #EDE8F5;border-radius:8px;padding:14px 18px;margin:0 0 20px">
      <div style="font-size:11px;font-weight:700;color:#A89EC0;letter-spacing:.7px;text-transform:uppercase;margin-bottom:6px">DETALLE</div>
      <div style="font-size:14px;color:#1A0F2E">{details}</div>
    </div>
    <p style="color:#6B5F80;font-size:12px;margin:0">
      Este es un mensaje automatico del sistema de monitoreo MGS.<br>
      <a href="mailto:operaciones@unergy.io" style="color:#915BD8">operaciones@unergy.io</a>
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    destinatarios = [{"email": e, "tipo": "to"} for e in to_emails]

    try:
        _smtp_send(msg, to_emails)
        _log_envio(destinatarios=destinatarios, subject=subject, tipo="alarma", success=True)
        print(f"[ALARM_EMAIL] Sent to {to_emails} for {proyecto_nombre}")
    except Exception as exc:
        _log_envio(destinatarios=destinatarios, subject=subject, tipo="alarma", success=False, error_msg=str(exc))
        print(f"[ALARM_EMAIL] Failed to send to {to_emails}: {exc}")


def send_falla_notification_email(
    *,
    to_emails: list[str],
    codigo_falla: str,
    proyecto_nombre: str,
    descripcion: str,
    estado_codigo: str = "",
    estado_etiqueta: str,
    prioridad_etiqueta: str,
    fecha_identificacion: str,
    hora_identificacion: str = "",
    fecha_programada: str = "",
    registrado_por: str,
    accion: str = "creada",
    # No es del enlace: va al log de auditoria del envio (_log_envio).
    proyecto_id: int | None = None,
    # backwards-compat
    estado_color: str = "",
    tipo_nombre: str = "",
    categoria_etiqueta: str = "",
) -> dict:
    """
    Envía notificación de falla con diseño Unergy de 9 secciones.
    No lanza excepción — retorna {"ok", "enviados", "errores"}.
    """
    if not to_emails:
        logger.warning("[FALLA_EMAIL] Sin destinatarios para %s", codigo_falla)
        return {"ok": False, "enviados": [], "errores": ["Sin destinatarios configurados"]}

    if not settings.SMTP_HOST:
        msg = f"[FALLA_EMAIL] SMTP no configurado — falla {codigo_falla} ({accion}) para {proyecto_nombre}"
        logger.warning(msg)
        print(msg)
        return {"ok": False, "enviados": [], "errores": ["SMTP no configurado"]}

    # ── Colores y labels según estado ────────────────────────────────────────
    _ESTADO_MAP = {
        "abierta":      ("#FF5757", "Falla Activa",     "⚠️"),
        "en_gestion":   ("#F6A623", "En Revisión",      "🔧"),
        "en_espera":    ("#F6A623", "En Revisión",      "🔧"),
        "programado":   ("#915BD8", "Falla Programada", "📅"),
        "cerrada":      ("#4ADE80", "Falla Resuelta",   "✅"),
        "sin_solucion": ("#A89EC0", "Sin Solución",     "🔕"),
    }
    strip_color, strip_label, emoji = _ESTADO_MAP.get(
        estado_codigo, ("#915BD8", estado_etiqueta or "Notificación", "🔔")
    )

    fecha_hora = f"{fecha_identificacion}" + (f" · {hora_identificacion}" if hora_identificacion else "")

    # Texto dinámico del aviso verde según estado
    _AVISO_MAP = {
        "abierta":      ("Falla activa — en seguimiento",
                         "Esta falla ha sido registrada y está siendo atendida por nuestro equipo de operaciones. Te notificaremos ante cualquier cambio de estado."),
        "en_gestion":   ("En revisión técnica",
                         "Nuestro equipo está analizando la causa raíz y ejecutando las acciones correctivas. Te informaremos cuando se resuelva o haya una actualización importante."),
        "en_espera":    ("En espera de acción externa",
                         "La falla está pendiente de una gestión con un tercero (operador de red, proveedor u otro). Haremos seguimiento y te notificaremos con el avance."),
        "programado":   ("Intervención programada",
                         f"Se ha programado una intervención para atender esta falla el <strong>{fecha_programada or fecha_identificacion}</strong>. El equipo de O&M ejecutará las acciones en la fecha acordada."),
        "cerrada":      ("Falla resuelta ✓",
                         "Esta falla ha sido cerrada satisfactoriamente. Puedes consultar el detalle de la resolución y las acciones correctivas aplicadas en la plataforma."),
        "sin_solucion": ("Cerrada sin solución",
                         "Esta falla fue cerrada sin solución definitiva. Queda registrada en el historial. Si el problema persiste, por favor crea un nuevo reporte."),
    }
    aviso_titulo, aviso_texto = _AVISO_MAP.get(
        estado_codigo,
        ("Seguimiento en curso",
         "Nuestro equipo está gestionando esta falla. Recibirás actualizaciones ante cualquier cambio de estado o resolución.")
    )
    logo_svg   = (
        '<svg width="44" height="36" viewBox="0 0 44 36" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="22" cy="4" r="3" fill="white"/>'
        '<path d="M8 10 L8 24 Q8 34 22 34 Q36 34 36 24 L36 10" stroke="white" stroke-width="5" fill="none" stroke-linecap="round"/>'
        '</svg>'
    )

    subject = f"Unergy {emoji} {strip_label} — {proyecto_nombre} | {codigo_falla}"

    body_html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:24px 0;background:#F4F1F9;font-family:-apple-system,'Segoe UI',Arial,sans-serif">

<table role="presentation" width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table role="presentation" width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%">

  <!-- 1. HEADER -->
  <tr><td style="background:#1A0F2E;padding:20px 28px;border-radius:12px 12px 0 0">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="vertical-align:middle">{logo_svg}</td>
        <td style="vertical-align:middle;text-align:right">
          <span style="font-family:monospace;font-size:13px;font-weight:700;color:#F6FF72;letter-spacing:.5px">{codigo_falla}</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- 2. STRIP -->
  <tr><td style="background:{strip_color};padding:10px 28px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td>
          <span style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#fff;opacity:.85">{strip_label}</span>
        </td>
        <td style="text-align:right">
          <span style="font-size:10px;font-weight:600;color:#fff;opacity:.9">{proyecto_nombre} &nbsp;·&nbsp; {fecha_hora}</span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- 3. NOMBRE DEL PROYECTO -->
  <tr><td style="background:#ffffff;padding:22px 28px 16px;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <div style="font-size:22px;font-weight:800;color:#1A0F2E;margin:0 0 14px">{proyecto_nombre}</div>
    <div style="height:1px;background:#EDE8F5"></div>
  </td></tr>

  <!-- 4. CAJA DE FALLA -->
  <tr><td style="background:#ffffff;padding:14px 28px;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <div style="background:#F7F4FD;border-left:3px solid #915BD8;border-radius:0 8px 8px 0;padding:12px 16px">
      <div style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#A89EC0;margin-bottom:4px">CÓDIGO DE FALLA</div>
      <div style="font-size:15px;font-weight:700;color:#1A0F2E;margin-bottom:6px;font-family:monospace">{codigo_falla}</div>
      {f'<div style="font-size:13px;font-weight:600;color:#7B6BA0;margin-bottom:10px">{tipo_nombre}</div>' if tipo_nombre else ''}
      <span style="display:inline-block;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;background:#915BD820;color:#915BD8;border:1px solid #915BD840;padding:2px 10px;border-radius:999px">{prioridad_etiqueta}</span>
    </div>
  </td></tr>

  <!-- 5. DESCRIPCIÓN -->
  <tr><td style="background:#ffffff;padding:14px 28px;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <div style="background:#F7F4FD;border-radius:8px;padding:14px 16px;border:1px solid #EDE8F5">
      <div style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#A89EC0;margin-bottom:8px">DESCRIPCIÓN</div>
      <div style="font-size:13px;font-weight:400;color:#1A0F2E;line-height:1.75">{descripcion}</div>
    </div>
  </td></tr>

  <!-- 6. METADATOS -->
  <tr><td style="background:#ffffff;padding:14px 28px;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="width:33%;vertical-align:top;padding-right:8px">
          <div style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#A89EC0;margin-bottom:4px">FECHA IDENTIFICACIÓN</div>
          <div style="font-size:12px;font-weight:700;color:#1A0F2E">{fecha_hora or "—"}</div>
        </td>
        <td style="width:33%;vertical-align:top;padding:0 8px">
          <div style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#A89EC0;margin-bottom:4px">ESTADO</div>
          <div style="display:flex;align-items:center;gap:6px">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{strip_color};flex-shrink:0"></span>
            <span style="font-size:12px;font-weight:700;color:#1A0F2E">{estado_etiqueta}</span>
          </div>
        </td>
        <td style="width:33%;vertical-align:top;padding-left:8px">
          <div style="font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#A89EC0;margin-bottom:4px">RESPONSABLE</div>
          <div style="font-size:12px;font-weight:700;color:#1A0F2E">{registrado_por}</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- 7. AVISO VERDE -->
  <tr><td style="background:#ffffff;padding:14px 28px;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <div style="border-left:3px solid #4ADE80;background:#F0FFF6;border-radius:0 8px 8px 0;padding:12px 16px">
      <div style="font-size:12px;font-weight:700;color:#1A3D2B;margin-bottom:4px">{aviso_titulo}</div>
      <div style="font-size:12px;color:#1A3D2B;opacity:.85;line-height:1.6">{aviso_texto}</div>
    </div>
  </td></tr>

  <!-- 8. CTA -->
  <tr><td style="background:#ffffff;padding:20px 28px;text-align:center;border-left:1px solid #EDE8F5;border-right:1px solid #EDE8F5">
    <span style="display:inline-block;background:#1A0F2E;color:#F6FF72;font-size:13px;font-weight:800;padding:13px 32px;border-radius:8px;letter-spacing:.3px">Plataforma Unergy Operaciones</span>
  </td></tr>

  <!-- 9. FOOTER -->
  <tr><td style="background:#F7F4FD;padding:14px 28px;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 12px 12px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="vertical-align:middle">
          <span style="font-size:11px;font-weight:600;color:#7B6BA0">Unergy · Operación y Mantenimiento Solar</span>
        </td>
        <td style="text-align:right;vertical-align:middle">
          <span style="font-size:11px;font-weight:600;color:#A89EC0">Notificación automática — no responder</span>
        </td>
      </tr>
    </table>
  </td></tr>

</table>
</td></tr>
</table>

</body>
</html>"""

    enviados = []
    errores  = []
    resultados = []  # un SMTP separado por persona -- cada uno puede fallar
                      # independiente, a diferencia de informe/alarma/reporte_cgm

    for to_email in to_emails:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = settings.SMTP_FROM
            msg["To"]      = to_email
            msg.attach(MIMEText(body_html, "html", "utf-8"))
            _smtp_send(msg, [to_email])
            resultados.append({"email": to_email, "tipo": "to", "exitoso": True})
            enviados.append(to_email)
            logger.info("[FALLA_EMAIL] Sent to %s for %s", to_email, codigo_falla)
        except Exception as exc:
            err_msg = str(exc)
            resultados.append({"email": to_email, "tipo": "to", "exitoso": False, "error": err_msg})
            errores.append(f"{to_email}: {err_msg}")
            logger.error("[FALLA_EMAIL] Failed to send to %s for %s: %s", to_email, codigo_falla, exc)

    _log_envio(
        destinatarios=resultados, subject=subject, tipo="falla",
        success=not errores, error_msg="; ".join(errores) if errores else None,
        proyecto_id=proyecto_id,
    )
    return {"ok": len(enviados) > 0, "enviados": enviados, "errores": errores}


def send_reporte_cgm_email(
    *,
    to_emails: list[str],
    excel_bytes: bytes,
    filename: str,
    fecha_str: str,
    destinatario_nombre: str,
    proyectos: list[str] | None = None,
    proyectos_total: int | None = None,
    excel_mensual_bytes: bytes | None = None,
    filename_mensual: str | None = None,
    mes_str: str | None = None,
    adjuntos_extra: list[tuple[bytes, str]] | None = None,
    cliente_id: int | None = None,
    operador_red_id: int | None = None,
) -> None:
    """
    Envía el reporte CGM (Excel adjunto) a un operador de red o cliente.

    cliente_id/operador_red_id: exactamente uno de los dos debería venir
    poblado (según el tipo de destinatario que resolvió el llamador) -- se
    pasan tal cual a _log_envio() para dar trazabilidad real en email_envios
    (auditoría 2026-08-26, antes no había ninguna FK, solo el nombre en texto
    libre de destinatario_nombre/proyectos).

    excel_mensual_bytes/filename_mensual/mes_str son opcionales -- se usan
    solo cuando el envío cubre el último día de un mes, para adjuntar
    ADEMÁS el consolidado de todo ese mes (mismo formato, más días). Ver
    reporte_cgm.dias_del_mes()/es_ultimo_dia_del_mes().

    adjuntos_extra: lista de (bytes, filename) adicionales -- para clientes
    puntuales que piden un Excel separado por proyecto en vez de uno
    combinado, todos en el mismo correo (ver CLIENTES_EXCEL_POR_PROYECTO en
    reporte_cgm.py).

    Lanza RuntimeError si SMTP no está configurado o falla el envío.
    """
    if not settings.SMTP_HOST:
        raise RuntimeError(
            "SMTP no configurado. Define SMTP_HOST, SMTP_PORT, SMTP_USER, "
            "SMTP_PASSWORD y SMTP_FROM en las variables de entorno."
        )

    subject = f"Reporte CGM — {fecha_str} — {destinatario_nombre}"
    fecha_frase = f"al periodo {fecha_str}" if " a " in fecha_str else f"al día {fecha_str}"
    nota_mensual_texto = f"Adjuntamos también el consolidado del mes de {mes_str}.\n\n" if excel_mensual_bytes else ""
    nota_mensual_html = (
        f'<p>Adjuntamos también el consolidado del mes de {mes_str}.</p>' if excel_mensual_bytes else ""
    )

    msg = MIMEMultipart("mixed")
    msg["From"] = settings.SMTP_FROM
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject

    cuerpo = MIMEMultipart("related")
    alternativa = MIMEMultipart("alternative")
    alternativa.attach(MIMEText(
        _REPORTE_CGM_TEXTO.format(fecha_frase=fecha_frase, nota_mensual=nota_mensual_texto), "plain", "utf-8",
    ))
    alternativa.attach(MIMEText(
        _REPORTE_CGM_HTML.format(fecha_frase=fecha_frase, nota_mensual=nota_mensual_html), "html", "utf-8",
    ))
    cuerpo.attach(alternativa)

    if _LOGO_UNERGY.exists():
        with open(_LOGO_UNERGY, "rb") as f:
            logo = MIMEImage(f.read())
        logo.add_header("Content-ID", "<logo_unergy>")
        logo.add_header("Content-Disposition", "inline", filename=_LOGO_UNERGY.name)
        cuerpo.attach(logo)

    msg.attach(cuerpo)

    # filename como parámetro aparte (no interpolado en el string) -- así
    # Python aplica la codificación RFC 2231 si el nombre tiene tildes/ñ
    # (ej. "COX ENERGY GENERACIÓN...", "CGM Ingeniería"). Interpolado a mano
    # como antes, Gmail no lo interpretaba y mostraba el adjunto sin nombre
    # ni ícono ("noname").
    adjunto = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    adjunto.set_payload(excel_bytes)
    encoders.encode_base64(adjunto)
    adjunto.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(adjunto)

    if excel_mensual_bytes:
        adjunto_mensual = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        adjunto_mensual.set_payload(excel_mensual_bytes)
        encoders.encode_base64(adjunto_mensual)
        adjunto_mensual.add_header("Content-Disposition", "attachment", filename=filename_mensual)
        msg.attach(adjunto_mensual)

    for extra_bytes, extra_filename in (adjuntos_extra or []):
        adjunto_extra = MIMEBase("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        adjunto_extra.set_payload(extra_bytes)
        encoders.encode_base64(adjunto_extra)
        adjunto_extra.add_header("Content-Disposition", "attachment", filename=extra_filename)
        msg.attach(adjunto_extra)

    # CCO real (no aparece en ningún header, solo en el sobre SMTP) -- lista de
    # seguimiento interno, igual que CORREO_SEGUIMIENTO en el script standalone.
    cco = [d.strip() for d in settings.CORREO_SEGUIMIENTO.split(",") if d.strip()]
    sobres = to_emails + [c for c in cco if c not in to_emails]

    destinatarios = [{"email": e, "tipo": "to"} for e in to_emails] + [{"email": e, "tipo": "cco"} for e in cco]

    try:
        _smtp_send(msg, sobres)
        _log_envio(
            destinatarios=destinatarios, subject=subject, tipo="reporte_cgm", success=True,
            proyectos=proyectos, proyectos_total=proyectos_total,
            cliente_id=cliente_id, operador_red_id=operador_red_id,
        )
    except Exception as exc:
        _log_envio(
            destinatarios=destinatarios, subject=subject, tipo="reporte_cgm", success=False,
            error_msg=str(exc), proyectos=proyectos, proyectos_total=proyectos_total,
            cliente_id=cliente_id, operador_red_id=operador_red_id,
        )
        raise RuntimeError(f"No se pudo enviar el reporte CGM: {exc}") from exc


def send_test_email(*, to_email: str, cliente_nombre: str) -> None:
    """
    Envía un correo de prueba para verificar la configuración del correo operacional.
    Lanza RuntimeError si SMTP no está configurado o falla el envío.
    """
    if not settings.SMTP_HOST:
        raise RuntimeError(
            "SMTP no configurado. Define SMTP_HOST, SMTP_PORT, SMTP_USER, "
            "SMTP_PASSWORD y SMTP_FROM en las variables de entorno."
        )

    subject = f"✓ Correo de prueba — {cliente_nombre} — Unergy"
    body_html = f"""
<html>
<body style="font-family:Arial,sans-serif;color:#1A0F2E;max-width:480px;margin:0 auto;padding:0">
  <div style="background:#1A0F2E;padding:24px 28px;border-radius:10px 10px 0 0">
    <div style="color:#F6FF72;font-size:20px;font-weight:800;letter-spacing:1px">UNERGY</div>
    <div style="color:#6B5F80;font-size:11px;letter-spacing:.8px;text-transform:uppercase;margin-top:2px">Correo de prueba</div>
  </div>
  <div style="background:#F7F4FD;padding:28px;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 10px 10px">
    <div style="background:#16a34a22;color:#16a34a;border:1px solid #16a34a44;border-radius:8px;padding:12px 16px;font-weight:700;font-size:14px;margin-bottom:16px">
      ✓ Configuración correcta
    </div>
    <p style="margin:0 0 12px">Este correo confirma que la dirección <strong>{to_email}</strong> está correctamente configurada como correo operacional para el cliente <strong>{cliente_nombre}</strong>.</p>
    <p style="margin:0 0 12px">Las notificaciones de fallas serán enviadas a esta dirección cuando se active la opción de notificación.</p>
    <p style="color:#6B5F80;font-size:12px;margin:0">
      <a href="mailto:operaciones@unergy.io" style="color:#915BD8">operaciones@unergy.io</a>
    </p>
  </div>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = settings.SMTP_FROM
    msg["To"]      = to_email
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        _smtp_send(msg, [to_email])
        _log_envio(destinatarios=[{"email": to_email, "tipo": "to"}], subject=subject, tipo="prueba", success=True)
        logger.info("[TEST_EMAIL] Sent to %s for cliente %s", to_email, cliente_nombre)
    except Exception as exc:
        _log_envio(destinatarios=[{"email": to_email, "tipo": "to"}], subject=subject, tipo="prueba", success=False, error_msg=str(exc))
        raise RuntimeError(f"No se pudo enviar el correo de prueba: {exc}") from exc
