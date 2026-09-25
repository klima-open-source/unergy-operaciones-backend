"""Login, recuperación de contraseña y alta de usuarios."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from apps.plataforma import models as pl_models
from apps.plataforma.services import seguridad

logger = logging.getLogger("operaciones.auth")

HORAS_VALIDEZ_RESET = 1

# Se responde lo mismo exista o no el correo: si el mensaje cambiara, cualquiera
# podría averiguar qué direcciones están registradas.
MENSAJE_RESET = (
    "Si el correo existe, recibirás instrucciones para restablecer tu "
    "contraseña."
)


class CredencialesIncorrectas(Exception):
    pass


class UsuarioInactivo(Exception):
    pass


class TokenInvalido(ValueError):
    pass


def autenticar(email: str, contrasena: str):
    """Devuelve el usuario, o levanta. Distingue credenciales de inactivo.

    Son códigos distintos a propósito (401 contra 403): a alguien con la
    contraseña correcta pero la cuenta desactivada hay que decirle que hable con
    un administrador, no que reintente.
    """
    usuario = pl_models.Usuario.objects.filter(email=email).first()
    if (
        usuario is None or not usuario.password
        or not seguridad.verificar_contrasena(contrasena, usuario.password)
    ):
        raise CredencialesIncorrectas("Credenciales incorrectas")
    if not usuario.activo:
        raise UsuarioInactivo("Usuario inactivo")

    usuario.ultimo_acceso = datetime.now(timezone.utc)
    usuario.save(update_fields=["ultimo_acceso"])
    return usuario


def token_de(usuario, movil: bool = False) -> str:
    minutos = seguridad.minutos_movil() if movil else None
    return seguridad.crear_token(seguridad.claims_de(usuario), minutos)


def pedir_reset(email: str) -> None:
    """Genera el token de una hora y manda el correo. Nunca revela si existe.

    El fallo del envío se registra pero no se propaga: si el correo no sale, el
    token igual quedó guardado y decirle al cliente que hubo un error revelaría
    que la cuenta existe.
    """
    usuario = pl_models.Usuario.objects.filter(email=email).first()
    if usuario is None:
        return

    usuario.password_reset_token = uuid.uuid4().hex
    usuario.password_reset_expires = datetime.now(timezone.utc) + timedelta(
        hours=HORAS_VALIDEZ_RESET
    )
    usuario.save(
        update_fields=["password_reset_token", "password_reset_expires"]
    )

    try:
        from app.services.email_service import send_reset_password_email

        send_reset_password_email(
            to_email=usuario.email, token=usuario.password_reset_token
        )
    except Exception as exc:
        logger.error("Error enviando email de reset: %s", exc)


def restablecer(token: str, contrasena_nueva: str) -> None:
    """Valida el token y cambia la contraseña.

    El token vencido y el inexistente dan el MISMO mensaje: distinguirlos diría
    a un atacante que acertó un token, solo que tarde.
    """
    usuario = pl_models.Usuario.objects.filter(
        password_reset_token=token
    ).first()
    if usuario is None or not usuario.password_reset_expires:
        raise TokenInvalido("Token inválido o expirado")
    if usuario.password_reset_expires < datetime.now(timezone.utc):
        raise TokenInvalido("Token inválido o expirado")
    if len(contrasena_nueva) < seguridad.LARGO_MINIMO_CONTRASENA:
        raise ValueError(
            f"La contraseña debe tener al menos "
            f"{seguridad.LARGO_MINIMO_CONTRASENA} caracteres"
        )

    usuario.password = seguridad.hash_contrasena(contrasena_nueva)
    # El token se quema al usarlo: un enlace de reset sirve una sola vez.
    usuario.password_reset_token = None
    usuario.password_reset_expires = None
    usuario.save(update_fields=[
        "password", "password_reset_token", "password_reset_expires",
    ])
