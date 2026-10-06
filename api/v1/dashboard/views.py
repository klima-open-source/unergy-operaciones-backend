"""ViewSet del dashboard."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.proyectos import models as py_models

from . import queryset as dashboard_queryset


@class_logger_wrapper(name="Operaciones | Dashboard")
class DashboardViewSet(viewsets.GenericViewSet):
    """GET /api/v1/dashboard/kpis — todas las métricas de la portada.

    Una sola llamada a propósito: la pantalla necesita todas las cifras juntas y
    partirlas en varios endpoints multiplicaría los viajes sin ahorrar consultas.
    """

    permission_classes = [RolePermission]
    queryset = py_models.Proyecto.objects.none()

    @action(detail=False, methods=["get"], url_path="kpis")
    def kpis(self, request):
        return Response(dashboard_queryset.kpis())
