"""Validación de tokens de Unergy Auth (authentication-system) por gRPC.

Es la mitad de red de `api.authentication.UnergyAuthAuthentication`: aquí solo
se pregunta al servicio si un token es válido y quién es su dueño. Qué usuario
de Operaciones le corresponde lo decide la clase de autenticación.

Se apaga con `GRPC_HOST` vacío, que es el default: un despliegue sin la variable
se comporta exactamente como antes de la integración.

En desarrollo local, sin acceso a la red del gRPC, `UNERGY_AUTH_HTTP_URL` valida
contra `GET <url>/auth/user/` (dj-rest-auth). `config/settings.py` impide usarlo
fuera de development. Si están las dos, gana gRPC.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import grpc
import httpx
from django.conf import settings

from . import auth_pb2, auth_pb2_grpc

logger = logging.getLogger(__name__)


class TokenInvalido(Exception):
    """El servicio respondió y el token no es válido (vencido, revocado, ajeno)."""


class ServicioNoDisponible(Exception):
    """No se pudo preguntar: el servicio no respondió o respondió con error gRPC."""


@dataclass(frozen=True)
class IdentidadUnergy:
    """Lo que authentication-system dice del dueño del token."""

    user_id: int
    email: str
    username: str
    nombre: str
    grupos: tuple[str, ...]


def habilitado() -> bool:
    return bool(settings.GRPC_HOST or settings.UNERGY_AUTH_HTTP_URL)


def validar_token(token: str) -> IdentidadUnergy:
    if settings.GRPC_HOST:
        return _validar_por_grpc(token)
    return _validar_por_http(token)


def _grupos_de(valor) -> tuple[str, ...]:
    """`groups` de dj-rest-auth: nombres sueltos o `{"name": ...}`, según el serializer."""
    if not isinstance(valor, list):
        return ()
    nombres = (g.get("name") if isinstance(g, dict) else g for g in valor)
    return tuple(n for n in nombres if isinstance(n, str) and n)


def _validar_por_http(token: str) -> IdentidadUnergy:
    """Solo desarrollo (ver el docstring del módulo). Mismo contrato que gRPC."""
    url = f"{settings.UNERGY_AUTH_HTTP_URL.rstrip('/')}/auth/user/"
    try:
        respuesta = httpx.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=settings.GRPC_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.error("Unergy Auth (HTTP) no respondió en %s: %s", url, exc)
        raise ServicioNoDisponible from exc

    if respuesta.status_code in (401, 403):
        raise TokenInvalido("Token inválido")
    if respuesta.status_code != 200:
        logger.error("Unergy Auth (HTTP) respondió %s en %s", respuesta.status_code, url)
        raise ServicioNoDisponible

    datos = respuesta.json()
    return IdentidadUnergy(
        user_id=int(datos.get("pk") or datos.get("id") or 0),
        email=datos.get("email") or "",
        username=datos.get("username") or "",
        nombre=f"{datos.get('first_name') or ''} {datos.get('last_name') or ''}".strip(),
        grupos=_grupos_de(datos.get("groups")),
    )


def _validar_por_grpc(token: str) -> IdentidadUnergy:
    """Pregunta a authentication-system por `token`.

    Lanza `TokenInvalido` si el servicio lo rechaza y `ServicioNoDisponible` si
    no se le pudo preguntar. La distinción importa: lo primero es un 401 para
    quien llama; lo segundo es una caída nuestra y no debe mandarlo al login.
    """
    destino = f"{settings.GRPC_HOST}:{settings.GRPC_PORT}"
    try:
        with grpc.insecure_channel(destino) as canal:
            respuesta = auth_pb2_grpc.AuthServiceStub(canal).ValidateToken(
                auth_pb2.TokenRequest(token=token),
                # Sin timeout, un servicio colgado deja colgada cada petición
                # del API, porque todas pasan por aquí.
                timeout=settings.GRPC_TIMEOUT,
            )
    except grpc.RpcError as exc:
        logger.error("Unergy Auth no respondió en %s: %s", destino, exc)
        raise ServicioNoDisponible from exc

    if not respuesta.is_valid:
        motivo = respuesta.error.message if respuesta.HasField("error") else ""
        raise TokenInvalido(motivo or "Token inválido")

    return IdentidadUnergy(
        user_id=respuesta.user_id,
        email=respuesta.email,
        username=respuesta.username,
        nombre=f"{respuesta.first_name} {respuesta.last_name}".strip(),
        grupos=tuple(respuesta.groups),
    )
