"""ViewSets de operadores de red y sus contactos."""

from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.fronteras import models as fr_models
from apps.fronteras.services import operadores as operadores_service
from apps.fronteras.services import operadores_originabot

from . import queryset as operadores_queryset
from . import serializers as operadores_serializers


def _forzar(request) -> bool:
    return request.query_params.get("forzar", "").lower() in ("true", "1")


@class_logger_wrapper(name="Operaciones | Fronteras | Operadores de red")
class OperadorRedViewSet(
    viewsets.GenericViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin,
    mixins.CreateModelMixin, mixins.UpdateModelMixin,
):
    """Catálogo de operadores de red.

    GET   /api/v1/operadores-red                    con contactos y nº de fronteras
    GET   /api/v1/operadores-red/{id}
    POST  /api/v1/operadores-red[?forzar=true]
    PATCH /api/v1/operadores-red/{id}[?forzar=true]
    POST  /api/v1/operadores-red/{id}/contactos     → 201
    POST  /api/v1/operadores-red/originabot         {codigo, nombre} → el operador
                                                    vinculado al de TSF

    **Dos niveles de duplicado, y solo uno bloquea.** Un `nombre_legal` repetido
    es un 409 definitivo (la columna es única). Un nombre PARECIDO también
    responde 409, pero es un aviso: el cliente reintenta con `?forzar=true` y se
    guarda. El cuerpo del aviso lleva el candidato para que la pantalla lo
    ofrezca.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return operadores_queryset.con_conteo_de_fronteras()

    def get_serializer_class(self):
        if self.action == "create":
            return operadores_serializers.OperadorRedEscrituraSerializer
        if self.action == "partial_update":
            return operadores_serializers.OperadorRedUpdateSerializer
        return operadores_serializers.OperadorRedSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        datos = serializer.validated_data

        existente = operadores_service.duplicado_exacto(datos["nombre_legal"])
        if existente:
            raise Conflict(
                f"Ya existe un operador de red con ese nombre legal (ID {existente.id})"
            )
        if not _forzar(request):
            parecido = operadores_service.duplicado_parecido(
                datos.get("nombre_comercial") or datos["nombre_legal"]
            )
            if parecido:
                raise Conflict(operadores_service.aviso_de_parecido(parecido))

        operador = serializer.save()
        return Response(self._leer(operador.pk), status=201)

    def partial_update(self, request, *args, **kwargs):
        operador = get_object_or_404(fr_models.OperadorRed, pk=kwargs["pk"])
        serializer = self.get_serializer(operador, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        cambios = serializer.validated_data

        if cambios.get("nombre_legal"):
            duplicado = operadores_service.duplicado_exacto(
                cambios["nombre_legal"], excluir_id=operador.pk
            )
            if duplicado:
                raise Conflict(
                    "Ya existe otro operador de red con ese nombre legal "
                    f"(ID {duplicado.id})"
                )

        nombre = cambios.get("nombre_comercial") or cambios.get("nombre_legal")
        if not _forzar(request) and nombre:
            parecido = operadores_service.duplicado_parecido(
                nombre, excluir_id=operador.pk
            )
            if parecido:
                raise Conflict(operadores_service.aviso_de_parecido(parecido))

        serializer.save()
        return Response(self._leer(operador.pk))

    @action(detail=True, methods=["post"], url_path="contactos")
    @log_endpoint(name="Operaciones | Fronteras | Operadores de red | Contacto")
    def contactos(self, request, pk=None):
        operador = get_object_or_404(fr_models.OperadorRed, pk=pk)
        serializer = operadores_serializers.ContactoEscrituraSerializer(
            data=request.data
        )
        serializer.is_valid(raise_exception=True)
        contacto = serializer.save(operador_red=operador)
        return Response(
            operadores_serializers.ContactoSerializer(contacto).data, status=201
        )

    @action(detail=False, methods=["post"], url_path="originabot")
    @log_endpoint(name="Operaciones | Operadores de red | Vincular originabot")
    def vincular_originabot(self, request):
        """El operador de Operaciones para un `code` de originabot (TSF).

        Lo usa «importar desde TSF»: si el operador del proyecto no está aún en
        nuestro catálogo, lo vincula por nombre o lo crea, para que el mapeo sea
        siempre por llave y no por nombre. Idempotente. Ver
        `apps/fronteras/services/operadores_originabot.py`.
        """
        datos = operadores_serializers.VincularOriginabotSerializer(data=request.data)
        datos.is_valid(raise_exception=True)
        try:
            operador, estado = operadores_originabot.vincular(
                datos.validated_data["codigo"], datos.validated_data["nombre"],
            )
        except operadores_originabot.VinculoImposible as exc:
            raise Conflict(str(exc))
        return Response({**self._leer(operador.pk), "estado": estado})

    def _leer(self, pk) -> dict:
        """Relee con el conteo anotado, que el serializer de salida expone."""
        return operadores_serializers.OperadorRedSerializer(
            self.get_queryset().get(pk=pk)
        ).data


@class_logger_wrapper(name="Operaciones | Fronteras | Contactos de operador")
class OperadorRedContactoViewSet(
    viewsets.GenericViewSet, mixins.UpdateModelMixin, mixins.DestroyModelMixin
):
    """Contactos de un operador, editables por su propio id.

    PATCH  /api/v1/operadores-red/contactos/{id}
    DELETE /api/v1/operadores-red/contactos/{id}   → 204
    """

    permission_classes = [RolePermission]
    http_method_names = ["patch", "delete", "head", "options"]
    serializer_class = operadores_serializers.ContactoUpdateSerializer
    queryset = fr_models.OperadorRedContacto.objects.all()

    def partial_update(self, request, *args, **kwargs):
        contacto = self.get_object()
        serializer = self.get_serializer(contacto, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(operadores_serializers.ContactoSerializer(contacto).data)
