"""ViewSet de generación diaria."""

from django.db.models import Count, Max, Min, Sum
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.pagination import recortar
from api.permissions import RolePermission
from apps.proyectos import models as py_models
from apps.proyectos.services import generacion as generacion_service

from . import serializers as gen_serializers


@class_logger_wrapper(name="Operaciones | Proyectos | Generación diaria")
class GeneracionViewSet(viewsets.GenericViewSet):
    """Generación diaria por proyecto.

    GET    /api/v1/generacion[?proyecto_id=&fecha_inicio=&fecha_fin=&page=&size=]
    POST   /api/v1/generacion                → 201; 409 si ya existe el día
    PUT    /api/v1/generacion/{id}
    DELETE /api/v1/generacion/{id}           → 204
    POST   /api/v1/generacion/bulk           importación masiva
    GET    /api/v1/generacion/resumen/por-proyecto[?fecha_inicio=&fecha_fin=]

    La clave natural es (proyecto, fecha): el POST no pisa un día ya cargado
    —responde 409 y sugiere el PUT—, y el `bulk` decide con `overwrite`.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "put", "delete", "head", "options"]
    queryset = py_models.GeneracionDiaria.objects.all()

    def _fila(self, pk):
        fila = py_models.GeneracionDiaria.objects.filter(
            pk=pk
        ).select_related("proyecto").first()
        if fila is None:
            raise NotFound("Registro no encontrado")
        return fila

    @staticmethod
    def _entero(request, nombre, defecto, minimo, maximo):
        crudo = request.query_params.get(nombre)
        if crudo in (None, ""):
            return defecto
        if not crudo.isdigit() or not minimo <= int(crudo) <= maximo:
            raise ValidationError({nombre: f"Entero entre {minimo} y {maximo}."})
        return int(crudo)

    def list(self, request, *args, **kwargs):
        consulta = py_models.GeneracionDiaria.objects.select_related("proyecto")
        for parametro, filtro in (
            ("proyecto_id", "proyecto_id"),
            ("fecha_inicio", "fecha__gte"),
            ("fecha_fin", "fecha__lte"),
        ):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{filtro: valor})

        pagina = self._entero(request, "page", 1, 1, 10**6)
        tamano = recortar(self._entero(request, "size", 90, 1, 10**6))
        total = consulta.count()
        inicio = (pagina - 1) * tamano
        items = consulta.order_by("-fecha")[inicio:inicio + tamano]

        return Response({
            "items": gen_serializers.GeneracionSerializer(items, many=True).data,
            "total": total,
            "page": pagina,
            "size": tamano,
            "pages": -(-total // tamano),
        })

    def create(self, request, *args, **kwargs):
        entrada = gen_serializers.GeneracionEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        if py_models.GeneracionDiaria.objects.filter(
            proyecto=datos["proyecto"], fecha=datos["fecha"]
        ).exists():
            raise Conflict(
                f'Ya existe un registro para proyecto {datos["proyecto"].id} '
                f'en {datos["fecha"]}. Usa PUT para actualizar.'
            )
        fila = entrada.save()
        return Response(
            gen_serializers.GeneracionSerializer(self._fila(fila.pk)).data,
            status=201,
        )

    def update(self, request, *args, **kwargs):
        fila = self._fila(kwargs["pk"])
        entrada = gen_serializers.GeneracionEscrituraSerializer(
            fila, data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        entrada.save()
        return Response(
            gen_serializers.GeneracionSerializer(self._fila(fila.pk)).data
        )

    def destroy(self, request, *args, **kwargs):
        self._fila(kwargs["pk"]).delete()
        return Response(status=204)

    @action(detail=False, methods=["post"], url_path="bulk")
    @log_endpoint(name="Operaciones | Proyectos | Generación | Bulk")
    def bulk(self, request):
        entrada = gen_serializers.BulkSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        return Response(generacion_service.importar(
            datos["items"], datos["overwrite"]
        ))

    @action(detail=False, methods=["get"], url_path="resumen/por-proyecto")
    def resumen_por_proyecto(self, request):
        consulta = py_models.GeneracionDiaria.objects.filter(
            proyecto__isnull=False
        )
        for parametro, filtro in (
            ("fecha_inicio", "fecha__gte"),
            ("fecha_fin", "fecha__lte"),
        ):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{filtro: valor})

        filas = (
            consulta.values("proyecto_id", "proyecto__nombre_comercial")
            .annotate(
                total_kwh_real=Sum("kwh_real"),
                total_kwh_p90=Sum("kwh_p90"),
                dias_con_dato=Count("id"),
                desde=Min("fecha"),
                hasta=Max("fecha"),
            )
        )
        return Response([
            {
                "proyecto_id": f["proyecto_id"],
                "nombre_comercial": f["proyecto__nombre_comercial"],
                # `is not None` y no un `or 0`: una generación real de
                # exactamente 0.0 —planta caída todo el período— es un dato
                # válido y debe mostrarse como 0, no como null.
                "total_kwh_real": (
                    float(f["total_kwh_real"])
                    if f["total_kwh_real"] is not None else None
                ),
                "total_kwh_p90": (
                    float(f["total_kwh_p90"])
                    if f["total_kwh_p90"] is not None else None
                ),
                "dias_con_dato": f["dias_con_dato"],
                "fecha_inicio": f["desde"],
                "fecha_fin": f["hasta"],
            }
            for f in filas
        ])
