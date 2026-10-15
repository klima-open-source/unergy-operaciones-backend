"""ViewSet de portafolios (capas de proyectos)."""

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
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
    PATCH  /api/v1/portafolios/asignar      mete o saca una planta de un portafolio
    PATCH  /api/v1/portafolios/{id}[?forzar=]   renombrar
    DELETE /api/v1/portafolios/{id}         → 204; las plantas no se borran

    Una planta puede estar en varios portafolios (`portafolio_proyectos`).

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
        pertenencias: dict[int, list[int]] = {}
        for portafolio_id, proyecto_id in py_models.PortafolioProyecto.objects.values_list(
            "portafolio_id", "proyecto_id"
        ):
            pertenencias.setdefault(portafolio_id, []).append(proyecto_id)
        con_portafolio = {pid for ids in pertenencias.values() for pid in ids}

        proyectos = {
            proyecto.id: portafolios_serializers.ProyectoEnPortafolioSerializer(proyecto).data
            for proyecto in py_models.Proyecto.objects.filter(deleted_at__isnull=True)
            .order_by("nombre_comercial")
        }
        operativos = portafolios_service.ids_operativos()
        # Al pool solo van los OPERATIVOS sin ningún portafolio: son los que
        # importan para los informes.
        sin_capa = [
            item for pid, item in proyectos.items()
            if pid not in con_portafolio and pid in operativos
        ]
        capas = [
            {
                "id": capa.id, "nombre": capa.nombre,
                "proyectos": sorted(
                    (proyectos[pid] for pid in pertenencias.get(capa.id, []) if pid in proyectos),
                    key=lambda item: item["nombre"] or "",
                ),
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

        try:
            capa.save()
        except IntegrityError:
            raise Conflict("Ya existe un portafolio con ese nombre")
        return Response(self._salida(capa))

    def destroy(self, request, *args, **kwargs):
        capa = get_object_or_404(py_models.Portafolio, pk=kwargs["pk"])
        # Las plantas NO se borran: solo sus pertenencias a esta capa (CASCADE).
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

        if "en_portafolio" not in datos:
            # Cuerpo del frontend anterior (`{proyecto_id, portafolio_id}`):
            # mover la planta a esa capa, como cuando solo podía estar en una.
            portafolios_service.mover(proyecto.id, capa_id)
        elif capa_id is None:
            raise ValidationError({"portafolio_id": "Indica el portafolio."})
        elif datos["en_portafolio"]:
            portafolios_service.poner(proyecto.id, capa_id)
        else:
            portafolios_service.quitar(proyecto.id, capa_id)
        return Response({
            "ok": True,
            "proyecto_id": proyecto.id,
            "portafolio_ids": sorted(
                proyecto.pertenencias_portafolio.values_list("portafolio_id", flat=True)
            ),
        })

    @staticmethod
    def _salida(capa) -> dict:
        """El POST y el PATCH devuelven la capa SIN sus proyectos, como hoy."""
        return {
            "id": capa.id, "nombre": capa.nombre, "proyectos": [],
        }
