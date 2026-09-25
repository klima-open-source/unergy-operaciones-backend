"""Crea (o reactiva) un usuario de la plataforma.

`django.contrib.auth` NO está instalado: los usuarios viven en la tabla propia
`usuarios` (`apps.plataforma.models.Usuario`), con la contraseña en bcrypt y el
rol tomado del enum `Rol`. Por eso `createsuperuser` no aplica y hace falta este
comando.

La contraseña se pide de forma interactiva por defecto para que no quede en el
historial de la shell. Uso:

    # Local, contraseña interactiva:
    uv run python manage.py crear_usuario --email camilo@unergy.io --nombre "Camilo" --rol admin

    # En el servidor (producción no se escribe desde local, ver CLAUDE.md):
    docker compose exec operaciones python manage.py crear_usuario \\
        --email camilo@unergy.io --nombre "Camilo" --rol admin

Si el correo ya existe, no se duplica: el comando actualiza nombre/rol, resetea
la contraseña y reactiva la cuenta (útil para recuperar acceso). Los roles
válidos son los del enum `Rol`.
"""
import getpass

from django.core.management.base import BaseCommand, CommandError

from apps.plataforma.models import Rol, Usuario
from apps.plataforma.services import seguridad


class Command(BaseCommand):
    help = "Crea o reactiva un usuario de la plataforma (tabla usuarios)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Correo (identifica la cuenta).")
        parser.add_argument("--nombre", required=True, help="Nombre para mostrar.")
        parser.add_argument(
            "--rol", required=True, choices=[r.value for r in Rol],
            help="Rol del enum Rol: " + ", ".join(r.value for r in Rol),
        )
        parser.add_argument(
            "--password",
            help="Contraseña. Omítela para que se pida interactivamente "
            "(recomendado: así no queda en el historial de la shell).",
        )

    def handle(self, *args, **opciones):
        email = opciones["email"].strip().lower()
        nombre = opciones["nombre"].strip()
        rol = opciones["rol"]

        contrasena = opciones.get("password")
        if not contrasena:
            contrasena = getpass.getpass("Contraseña: ")
            if contrasena != getpass.getpass("Confirmar contraseña: "):
                raise CommandError("Las contraseñas no coinciden.")

        if len(contrasena) < seguridad.LARGO_MINIMO_CONTRASENA:
            raise CommandError(
                f"La contraseña debe tener al menos "
                f"{seguridad.LARGO_MINIMO_CONTRASENA} caracteres."
            )

        usuario, creado = Usuario.objects.get_or_create(
            email=email, defaults={"nombre": nombre, "rol": rol},
        )
        usuario.nombre = nombre
        usuario.rol = rol
        usuario.activo = True
        usuario.password_hash = seguridad.hash_contrasena(contrasena)
        usuario.save()

        verbo = "creado" if creado else "actualizado (ya existía)"
        self.stdout.write(self.style.SUCCESS(
            f"Usuario {verbo}: {email} (rol={rol}, id={usuario.id})."
        ))
