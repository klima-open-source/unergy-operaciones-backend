"""ViewSet de notificaciones del usuario autenticado."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.pagination import recortar
from apps.plataforma import models as pl_models

from . import serializers as notif_serializers


@class_logger_wrapper(name="Operaciones | Plataforma | Notificaciones")
class NotificacionViewSet(viewsets.GenericViewSet):
    """Notificaciones del usuario autenticado.

    GET   /api/v1/notificaciones[?leida=&page=&size=]
    GET   /api/v1/notificaciones/count          → {"no_leidas": n}
    PATCH /api/v1/notificaciones/{id}/leer
    PATCH /api/v1/notificaciones/leer-todas     → {"actualizadas": n}

    Toda consulta va acotada al usuario del token: una notificación es privada y
    el id de la ruta no basta para pedirla. Sin ese filtro, `PATCH /{id}/leer`
    dejaría marcar como leída la notificación de cualquier otro.
    """

    # Sin `required_role`: cada usuario ve lo suyo, no hay rol que otorgar.
    pagination_class = None                 # paginación manual, ver `list`
    http_method_names = ["get", "patch", "head", "options"]
    serializer_class = notif_serializers.NotificacionSerializer
    queryset = pl_models.Notificacion.objects.none()

    def get_queryset(self):
        return pl_models.Notificacion.objects.filter(usuario_id=self.request.user.id)

    def list(self, request, *args, **kwargs):
        """Devuelve una LISTA plana, no la envoltura paginada.

        El contrato actual pagina con `page`/`size` pero responde el array
        directo; usar `BasePagination` cambiaría la forma de la respuesta.
        """
        consulta = self.get_queryset()
        leida = request.query_params.get("leida")
        if leida is not None:
            consulta = consulta.filter(leida=leida.lower() in ("true", "1"))

        pagina, tamano = self._paginacion(request)
        inicio = (pagina - 1) * tamano
        items = consulta.order_by("-created_at")[inicio:inicio + tamano]
        return Response(self.get_serializer(items, many=True).data)

    @staticmethod
    def _paginacion(request) -> tuple[int, int]:
        try:
            pagina = int(request.query_params.get("page", 1))
            tamano = int(request.query_params.get("size", 20))
        except ValueError:
            raise ValidationError("`page` y `size` deben ser enteros.")
        if pagina < 1 or tamano < 1:
            raise ValidationError("`page` >= 1 y `size` >= 1.")
        # Pedir mas del tope recorta, no falla. Ver `api.pagination.recortar`.
        return pagina, recortar(tamano)

    @action(detail=False, methods=["get"], url_path="count")
    def count(self, request):
        return Response({"no_leidas": self.get_queryset().filter(leida=False).count()})

    @action(detail=True, methods=["patch"], url_path="leer")
    @log_endpoint(name="Operaciones | Plataforma | Notificaciones | Leer")
    def leer(self, request, pk=None):
        notificacion = self.get_queryset().filter(pk=pk).first()
        if notificacion is None:
            raise NotFound("Notificacion no encontrada")
        notificacion.leida = True
        notificacion.save(update_fields=["leida"])
        return Response(self.get_serializer(notificacion).data)

    @action(detail=False, methods=["patch"], url_path="leer-todas")
    @log_endpoint(name="Operaciones | Plataforma | Notificaciones | Leer todas")
    def leer_todas(self, request):
        actualizadas = self.get_queryset().filter(leida=False).update(leida=True)
        return Response({"actualizadas": actualizadas})
