"""Búsqueda global (⌘K). Ver `apps/plataforma/services/busqueda.py`.

    GET /api/v1/busqueda?q=   proyectos, clientes, contratos y fronteras
"""
from rest_framework import viewsets
from rest_framework.response import Response

from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.plataforma.services import busqueda


@class_logger_wrapper(name="Operaciones | Búsqueda")
class BusquedaViewSet(viewsets.GenericViewSet):
    # Sin `required_role`: devuelve lo mismo que las listas, que ve cualquier
    # usuario autenticado.
    permission_classes = [RolePermission]
    pagination_class = None

    def list(self, request):
        return Response(busqueda.buscar(request.query_params.get("q", "")))
