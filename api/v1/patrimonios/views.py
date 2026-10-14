"""ViewSet de patrimonios autónomos."""

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.proyectos import models as py_models
from apps.proyectos.services import patrimonios as patrimonios_service

from . import serializers as patrimonios_serializers


@class_logger_wrapper(name="Operaciones | Proyectos | Patrimonios")
class PatrimonioViewSet(viewsets.GenericViewSet):
    """Patrimonios autónomos (fiducias) por los que se tienen participaciones.

    GET    /api/v1/patrimonios        los generales, con sus subpatrimonios y las
                                      plantas de cada uno
    POST   /api/v1/patrimonios        → 201
    PATCH  /api/v1/patrimonios/{id}
    DELETE /api/v1/patrimonios/{id}   → 204; 409 si tiene participaciones o
                                      subpatrimonios

    Un código repetido responde 409. La jerarquía es de dos niveles: un
    subpatrimonio cuelga de un general (`padre_id`), nunca de otro subpatrimonio.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = py_models.Patrimonio.objects.all()

    def list(self, request):
        return Response(patrimonios_serializers.PatrimonioSerializer(
            patrimonios_service.arbol(), many=True).data)

    def create(self, request):
        entrada = patrimonios_serializers.PatrimonioEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        if "nombre" not in entrada.validated_data:
            raise ValidationError({"nombre": ["Este campo es requerido."]})
        return Response(self._guardar(py_models.Patrimonio(), entrada.validated_data),
                        status=201)

    def partial_update(self, request, pk=None):
        patrimonio = get_object_or_404(py_models.Patrimonio, pk=pk)
        entrada = patrimonios_serializers.PatrimonioEscrituraSerializer(
            data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        return Response(self._guardar(patrimonio, entrada.validated_data))

    def destroy(self, request, pk=None):
        patrimonio = get_object_or_404(py_models.Patrimonio, pk=pk)
        try:
            patrimonios_service.validar_borrado(patrimonio)
        except patrimonios_service.ReglaPatrimonio as exc:
            raise Conflict(str(exc))
        patrimonio.delete()
        return Response(status=204)

    @staticmethod
    def _guardar(patrimonio, datos) -> dict:
        try:
            patrimonios_service.validar_padre(
                patrimonio, datos.get("padre", patrimonio.padre if patrimonio.pk else None))
        except patrimonios_service.ReglaPatrimonio as exc:
            raise Conflict(str(exc))
        for campo, valor in datos.items():
            setattr(patrimonio, campo, valor)
        try:
            with transaction.atomic():
                patrimonio.save()
        except IntegrityError:
            raise Conflict(f"Ya existe un patrimonio con el código {patrimonio.codigo}")
        return patrimonios_serializers.SubpatrimonioSerializer(patrimonio).data
