"""ViewSet de portafolios (capas de proyectos)."""

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.proyectos import models as py_models
from apps.proyectos.services import portafolios as portafolios_service

from . import serializers as portafolios_serializers


def _forzar(request) -> bool:
    return request.query_params.get("forzar", "").lower() in ("true", "1")


@class_logger_wrapper(name="Operaciones | Proyectos | Portafolios")
class PortafolioViewSet(
    viewsets.GenericViewSet, mixins.ListModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, mixins.DestroyModelMixin,
):
    """Capas con las que se agrupan los proyectos.

    GET    /api/v1/portafolios              capas con sus proyectos + pool «sin portafolio»
    POST   /api/v1/portafolios[?forzar=]    → 201
    PATCH  /api/v1/portafolios/asignar      asigna o quita un proyecto
    PATCH  /api/v1/portafolios/{id}[?forzar=]   renombrar / activar
    DELETE /api/v1/portafolios/{id}         → 204; sus proyectos quedan sin capa

    Un nombre parecido a otro responde 409 con el candidato, y el cliente
    reintenta con `?forzar=true`. El UNIQUE de la base queda como red final.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = py_models.Portafolio.objects.all()

    def get_serializer_class(self):
        if self.action == "create":
            return portafolios_serializers.PortafolioCreateSerializer
        if self.action == "partial_update":
            return portafolios_serializers.PortafolioUpdateSerializer
        return portafolios_serializers.PortafolioSerializer

    def list(self, request, *args, **kwargs):
        try:
            portafolios_service.sembrar_si_esta_vacio()
        except portafolios_service.SiembraConcurrente as exc:
            raise Conflict(str(exc))

        proyectos = list(
            py_models.Proyecto.objects.filter(deleted_at__isnull=True)
            .order_by("nombre_comercial")
        )
        operativos = portafolios_service.ids_operativos()
        por_capa: dict[int, list] = {}
        sin_capa: list = []
        for proyecto in proyectos:
            item = portafolios_serializers.ProyectoEnPortafolioSerializer(proyecto).data
            if proyecto.portafolio_id:
                por_capa.setdefault(proyecto.portafolio_id, []).append(item)
            elif proyecto.id in operativos:
                # Al pool solo van los OPERATIVOS sin capa: son los que importan
                # para los informes.
                sin_capa.append(item)

        capas = [
            {
                "id": capa.id, "nombre": capa.nombre, "activo": capa.activo,
                "proyectos": por_capa.get(capa.id, []),
            }
            for capa in py_models.Portafolio.objects.order_by("nombre")
        ]
        return Response({"portafolios": capas, "sin_portafolio": sin_capa})

    def create(self, request, *args, **kwargs):
        entrada = self.get_serializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        nombre = entrada.validated_data["nombre"]

        if not _forzar(request):
            # Un nombre idéntico puntúa 1.0 en el matching difuso, muy por
            # encima del umbral: este aviso ya cubre el caso exacto y no hace
            # falta una comparación aparte.
            candidato = portafolios_service.parecido(nombre)
            if candidato:
                raise Conflict(portafolios_service.aviso_de_parecido(candidato))

        try:
            capa = py_models.Portafolio.objects.create(nombre=nombre)
        except IntegrityError:
            raise Conflict(f"Ya existe un portafolio llamado '{nombre}'")
        return Response(self._salida(capa), status=201)

    def partial_update(self, request, *args, **kwargs):
        capa = get_object_or_404(py_models.Portafolio, pk=kwargs["pk"])
        entrada = self.get_serializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cambios = entrada.validated_data

        if "nombre" in cambios:
            if not _forzar(request):
                candidato = portafolios_service.parecido(
                    cambios["nombre"], excluir_id=capa.pk
                )
                if candidato:
                    raise Conflict(
                        portafolios_service.aviso_de_parecido(candidato)
                    )
            capa.nombre = cambios["nombre"]
        if "activo" in cambios:
            capa.activo = cambios["activo"]

        try:
            capa.save()
        except IntegrityError:
            raise Conflict("Ya existe un portafolio con ese nombre")
        return Response(self._salida(capa))

    def destroy(self, request, *args, **kwargs):
        capa = get_object_or_404(py_models.Portafolio, pk=kwargs["pk"])
        # Los proyectos NO se borran: vuelven al pool sin capa.
        py_models.Proyecto.objects.filter(portafolio=capa).update(portafolio=None)
        capa.delete()
        return Response(status=204)

    @action(detail=False, methods=["patch"], url_path="asignar")
    @log_endpoint(name="Operaciones | Proyectos | Portafolios | Asignar")
    def asignar(self, request):
        entrada = portafolios_serializers.AsignarSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        proyecto = py_models.Proyecto.objects.filter(
            pk=datos["proyecto_id"]
        ).first()
        if proyecto is None:
            raise NotFound("Proyecto no encontrado")

        capa_id = datos.get("portafolio_id")
        if capa_id is not None and not py_models.Portafolio.objects.filter(
            pk=capa_id
        ).exists():
            raise NotFound("Portafolio no encontrado")

        proyecto.portafolio_id = capa_id
        proyecto.save(update_fields=["portafolio"])
        return Response({
            "ok": True,
            "proyecto_id": proyecto.id,
            "portafolio_id": proyecto.portafolio_id,
        })

    @staticmethod
    def _salida(capa) -> dict:
        """El POST y el PATCH devuelven la capa SIN sus proyectos, como hoy."""
        return {
            "id": capa.id, "nombre": capa.nombre, "activo": capa.activo,
            "proyectos": [],
        }
