"""ViewSets de autenticación y de usuarios."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import (
    AuthenticationFailed, NotFound, PermissionDenied, ValidationError,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.plataforma import models as pl_models
from apps.plataforma.services import cuentas, seguridad

from . import serializers as auth_serializers

LIMITE_USUARIOS = 500


@class_logger_wrapper(name="Operaciones | Plataforma | Auth")
class AuthViewSet(viewsets.GenericViewSet):
    """Login y recuperación de contraseña.

    POST /api/v1/auth/token           form-encoded: username + password
    POST /api/v1/auth/token/mobile    igual, pero token de larga duración
    GET  /api/v1/auth/me
    POST /api/v1/auth/forgot-password
    POST /api/v1/auth/reset-password

    **Los cuatro POST son públicos**: pedir un token no puede exigir un token.
    `me` es el único que requiere estar autenticado.

    El login llega **form-encoded**, no JSON: es el formato de
    `OAuth2PasswordRequestForm` y el frontend ya lo manda así.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    parser_classes = [FormParser, MultiPartParser, JSONParser]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = pl_models.Usuario.objects.none()

    def _login(self, request, movil: bool):
        usuario_email = request.data.get("username")
        contrasena = request.data.get("password")
        if not usuario_email or not contrasena:
            raise ValidationError(
                {"username": "Requerido", "password": "Requerido"}
            )
        try:
            usuario = cuentas.autenticar(usuario_email, contrasena)
        except cuentas.CredencialesIncorrectas as exc:
            raise AuthenticationFailed(str(exc))
        except cuentas.UsuarioInactivo as exc:
            raise PermissionDenied(str(exc))

        return Response({
            "access_token": cuentas.token_de(usuario, movil=movil),
            "token_type": "bearer",
        })

    @action(detail=False, methods=["post"], url_path="token")
    @log_endpoint(name="Operaciones | Auth | Login")
    def token(self, request):
        return self._login(request, movil=False)

    @action(detail=False, methods=["post"], url_path="token/mobile")
    @log_endpoint(name="Operaciones | Auth | Login móvil")
    def token_mobile(self, request):
        """Mismas credenciales, token de larga duración para la PWA.

        Expira en `MOBILE_JWT_EXPIRE_MINUTES` (30 días por defecto) para que
        nadie tenga que entrar a diario. Se revoca cambiando la contraseña.
        """
        return self._login(request, movil=True)

    @action(
        detail=False, methods=["get"], url_path="me",
        permission_classes=[RolePermission], authentication_classes=None,
    )
    def me(self, request):
        return Response(
            auth_serializers.UsuarioSerializer(request.user.usuario).data
        )

    @action(detail=False, methods=["post"], url_path="forgot-password")
    def forgot_password(self, request):
        """Manda el enlace de restablecimiento.

        Responde SIEMPRE lo mismo, exista o no el correo: si el mensaje
        cambiara, cualquiera podría averiguar qué direcciones están
        registradas.
        """
        entrada = auth_serializers.ForgotPasswordSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cuentas.pedir_reset(entrada.validated_data["email"])
        return Response({"msg": cuentas.MENSAJE_RESET})

    @action(detail=False, methods=["post"], url_path="reset-password")
    @log_endpoint(name="Operaciones | Auth | Reset")
    def reset_password(self, request):
        entrada = auth_serializers.ResetPasswordSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        try:
            cuentas.restablecer(
                entrada.validated_data["token"],
                entrada.validated_data["new_password"],
            )
        except cuentas.TokenInvalido as exc:
            raise ValidationError(str(exc))
        except ValueError as exc:
            raise ValidationError(str(exc))
        return Response({"msg": "Contraseña actualizada exitosamente"})


@class_logger_wrapper(name="Operaciones | Plataforma | Usuarios")
class UsuarioViewSet(viewsets.GenericViewSet):
    """Usuarios de la plataforma.

    GET   /api/v1/usuarios[?size=200]   cualquiera autenticado
    POST  /api/v1/usuarios              solo admin  → 201
    PATCH /api/v1/usuarios/{id}         solo admin

    Crear y editar exigen admin: cambiar un rol o una contraseña es dar o
    quitar acceso.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = pl_models.Usuario.objects.all()

    def get_permissions(self):
        if self.action in ("create", "partial_update"):
            self.required_role = ["admin"]
        return super().get_permissions()

    def list(self, request, *args, **kwargs):
        tamano = request.query_params.get("size", "200")
        if not tamano.isdigit() or not 1 <= int(tamano) <= LIMITE_USUARIOS:
            raise ValidationError(
                {"size": f"Entero entre 1 y {LIMITE_USUARIOS}."}
            )
        usuarios = pl_models.Usuario.objects.order_by("nombre")[:int(tamano)]
        return Response({
            "items": [
                {
                    "id": u.id, "nombre": u.nombre, "email": u.email,
                    "rol": u.rol, "activo": u.activo,
                }
                for u in usuarios
            ],
            "total": pl_models.Usuario.objects.count(),
        })

    def create(self, request, *args, **kwargs):
        entrada = auth_serializers.UsuarioCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        if pl_models.Usuario.objects.filter(email=datos["email"]).exists():
            raise Conflict("Ya existe un usuario con ese email")

        usuario = pl_models.Usuario.objects.create(
            email=datos["email"], nombre=datos["nombre"], rol=datos["rol"],
            activo=datos["activo"],
            password=seguridad.hash_contrasena(datos["password"]),
        )
        return Response(
            auth_serializers.UsuarioSerializer(usuario).data, status=201
        )

    def partial_update(self, request, *args, **kwargs):
        usuario = pl_models.Usuario.objects.filter(pk=kwargs["pk"]).first()
        if usuario is None:
            raise NotFound("Usuario no encontrado")

        entrada = auth_serializers.UsuarioActualizarSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cambios = entrada.validated_data

        campos = []
        for campo in ("nombre", "rol", "activo"):
            if campo in cambios:
                setattr(usuario, campo, cambios[campo])
                campos.append(campo)
        if "password" in cambios:
            usuario.password = seguridad.hash_contrasena(
                cambios["password"]
            )
            campos.append("password")

        if campos:
            usuario.save(update_fields=campos)
        return Response(auth_serializers.UsuarioSerializer(usuario).data)
