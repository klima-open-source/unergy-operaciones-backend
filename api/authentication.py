"""Autenticación por JWT o por API key — el equivalente del middleware de Origina.

Origina delega la validación del token a un servicio gRPC; este backend firma
sus propios JWT con `SECRET_KEY` (HS256). Por eso acá se valida en proceso en
vez de llamar a nadie.

**Los tokens ya emitidos siguen siendo válidos**: mismo algoritmo, misma clave y
mismos claims. Un usuario con sesión abierta no nota la migración.

Además, mientras dura la migración a Unergy Auth, un `Bearer` que no es nuestro
se valida contra authentication-system por gRPC (`UnergyAuthAuthentication`).

Dos formas de identificarse, y el ORDEN importa: si viene `X-API-Key` se usa
esa y no se mira el `Authorization`. Es el mismo orden que en FastAPI — una
integración que mande las dos por descuido no debe cambiar de identidad según
qué clase de DRF corra primero.

Igual que en Origina, lo que llega a la vista es un objeto con `.roles`, para
que `api/permissions.py` valga sin cambios en los dos repos.
"""

import hashlib

from django.utils import timezone
from rest_framework import authentication, exceptions

from apps.comun.integraciones.unergy_auth import cliente as unergy_auth
from apps.plataforma.models import ApiKey, Usuario
from apps.plataforma.services import seguridad


class UsuarioAutenticado:
    """El usuario tal como lo ven las vistas.

    `Usuario` es el `AUTH_USER_MODEL`, pero eso es para /admin/: el API lo
    envuelve acá para exponer `.roles` como lista. DRF solo exige
    `.is_authenticated`.
    """

    is_authenticated = True

    def __init__(self, usuario: Usuario):
        self.usuario = usuario
        self.id = usuario.id
        # `usuarios.rol` es UN enum, no una lista — a diferencia de Origina,
        # que recibe `groups` del servicio de auth. Se normaliza a lista acá y
        # solo acá, para que `api/permissions.py` sea idéntico en los dos repos
        # y ninguna vista tenga que decidir si `.roles` es str o list.
        self.roles: list[str] = [usuario.rol] if usuario.rol else []

    def __str__(self) -> str:
        return self.usuario.email


def _usuario_activo(usuario) -> UsuarioAutenticado:
    if usuario is None or not usuario.activo:
        raise exceptions.AuthenticationFailed(
            "Usuario inactivo o no encontrado"
        )
    return UsuarioAutenticado(usuario)


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Cabecera `X-API-Key`. La clave viaja en claro y se compara por su hash."""

    def authenticate_header(self, request):
        """Devolver algo acá es lo que hace que un no autenticado sea 401.

        DRF degrada `NotAuthenticated` a **403** cuando el PRIMER autenticador de
        la vista no ofrece cabecera `WWW-Authenticate` (ver
        `APIView.handle_exception`). FastAPI devuelve 401, y el frontend
        distingue los dos: con 403 mostraría "no tienes permiso" en vez de
        mandar a iniciar sesión. Verificado contra los dos backends el
        2026-09-04; `tests/test_codigos_de_auth.py` lo vigila.

        Se declara acá y no en `JWTAuthentication` porque el orden de
        `DEFAULT_AUTHENTICATION_CLASSES` pone esta primera, y DRF solo mira la
        primera. `Bearer` y no `Basic`: `Basic` haría que el navegador abra su
        propio diálogo de usuario y contraseña.
        """
        return "Bearer"

    def authenticate(self, request):
        clave = request.headers.get("X-API-Key")
        if not clave:
            return None

        hash_ = hashlib.sha256(clave.encode()).hexdigest()
        api_key = ApiKey.objects.filter(
            key_hash=hash_, activo=True
        ).select_related("usuario").first()
        if api_key is None:
            raise exceptions.AuthenticationFailed("API Key inválida")

        # Sirve para saber qué integraciones siguen vivas antes de rotar o
        # desactivar una clave.
        ApiKey.objects.filter(pk=api_key.pk).update(
            ultimo_uso=timezone.now()
        )
        return (_usuario_activo(api_key.usuario), None)


class JWTAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate_header(self, request):
        # Ver el docstring de ApiKeyAuthentication.authenticate_header: DRF solo
        # consulta el primero de la lista, pero declararlo en los dos evita que
        # reordenarlos convierta todos los 401 en 403 sin que nadie lo note.
        return self.keyword

    def authenticate(self, request):
        cabecera = authentication.get_authorization_header(request).split()
        if not cabecera or cabecera[0].lower() != self.keyword.lower().encode():
            return None
        if len(cabecera) != 2:
            raise exceptions.AuthenticationFailed(
                "Encabezado Authorization mal formado."
            )

        payload = seguridad.decodificar(cabecera[1].decode())
        if payload is None:
            # No es un token nuestro (o ya venció): puede ser de Unergy Auth, y
            # esa la valida la clase siguiente. Con Unergy Auth apagada no hay
            # siguiente que lo acepte, y se responde como siempre.
            if unergy_auth.habilitado():
                return None
            raise exceptions.AuthenticationFailed("Token inválido o expirado.")

        usuario = Usuario.objects.filter(pk=payload.get("sub")).first()
        return (_usuario_activo(usuario), None)


class AuthNoDisponible(exceptions.APIException):
    """503 propio y no `api.exceptions.ServicioNoDisponible`: importar
    `api.exceptions` desde aquí es circular (importa `rest_framework.views`, que
    carga `DEFAULT_AUTHENTICATION_CLASSES`)."""

    status_code = 503
    default_detail = "El servicio de autenticación no está disponible."


class UnergyAuthAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer <token>` emitido por Unergy Auth (authentication-system).

    Convive con `JWTAuthentication` durante la migración: esa acepta los tokens
    propios y le cede a esta los que no puede decodificar.

    El token lo valida authentication-system por gRPC; quién es el usuario en
    Operaciones se decide aquí, **por email contra `usuarios`**, y su rol sigue
    siendo el local. Por eso esta clase no usa el `GRPCAuthenticationMiddleware`
    de la skill `add-auth`: ese pone en `request.user` un `auth.User` sin `rol`,
    y `api/permissions.py` y cada FK a `usuarios` dejarían de funcionar.

    - token rechazado por el servicio → 401 (el frontend manda al login);
    - token válido pero el email no está en `usuarios` → 403: la persona existe
      en Unergy pero no tiene acceso a Operaciones, y mandarla al login sería un
      bucle;
    - servicio caído → 503, no 401: no es culpa de la sesión.
    """

    keyword = "Bearer"

    def authenticate_header(self, request):
        # Ver el docstring de ApiKeyAuthentication.authenticate_header.
        return self.keyword

    def authenticate(self, request):
        if not unergy_auth.habilitado():
            return None

        cabecera = authentication.get_authorization_header(request).split()
        if not cabecera or cabecera[0].lower() != self.keyword.lower().encode():
            return None
        if len(cabecera) != 2:
            raise exceptions.AuthenticationFailed(
                "Encabezado Authorization mal formado."
            )

        try:
            identidad = unergy_auth.validar_token(cabecera[1].decode())
        except unergy_auth.TokenInvalido:
            raise exceptions.AuthenticationFailed("Token inválido o expirado.")
        except unergy_auth.ServicioNoDisponible:
            raise AuthNoDisponible()

        usuario = (
            Usuario.objects.filter(email__iexact=identidad.email).first()
            if identidad.email else None
        )
        if usuario is None:
            raise exceptions.PermissionDenied(
                "Tu cuenta de Unergy no tiene acceso a Operaciones."
            )
        return (_usuario_activo(usuario), None)
