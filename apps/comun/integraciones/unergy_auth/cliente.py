"""Validación de tokens de Unergy Auth (authentication-system) por gRPC.

Es la mitad de red de `api.authentication.UnergyAuthAuthentication`: aquí solo
se pregunta al servicio si un token es válido y quién es su dueño. Qué usuario
de Operaciones le corresponde lo decide la clase de autenticación.

Se apaga con `GRPC_HOST` vacío, que es el default: un despliegue sin la variable
se comporta exactamente como antes de la integración.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import grpc
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
    grupos: tuple[str, ...]


def habilitado() -> bool:
    return bool(settings.GRPC_HOST)


def validar_token(token: str) -> IdentidadUnergy:
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
        grupos=tuple(respuesta.groups),
    )
