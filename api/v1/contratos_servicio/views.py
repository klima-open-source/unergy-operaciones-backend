"""ViewSet de contratos de servicio, con sus facturas y sus pagos."""

from django.db import transaction
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.clientes.services import documentos as documentos_service
from apps.contratos import models as ct_models
from apps.contratos.services import dedup as dedup_service
from apps.contratos.services import fronteras as fronteras_service
from apps.contratos.services import indexacion as indexacion_service
from apps.contratos.services import partes as partes_service
from apps.facturacion import models as fa_models

from . import queryset as cs_queryset
from . import serializers as cs_serializers

NOMBRE_ENLACE = "Enlace Drive del contrato"


@class_logger_wrapper(name="Operaciones | Contratos | Contratos de servicio")
class ContratoServicioViewSet(
    viewsets.GenericViewSet, mixins.ListModelMixin, mixins.CreateModelMixin,
    mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin,
):
    """Contratos de servicio (O&M, arriendo, representación, CGM…).

    GET|POST /api/v1/contratos-servicio[?tipo=&proyecto_id=&codigo_tsf=&limit=]
    GET|PATCH|DELETE /api/v1/contratos-servicio/{id}
    POST /api/v1/contratos-servicio/importar-indexacion?tipo=anual|mensual
    GET  /api/v1/contratos-servicio/duplicados-representacion   solo lee
    POST /api/v1/contratos-servicio/fusionar-representacion
    GET|POST /api/v1/contratos-servicio/{id}/facturas[?tipo=]
    PATCH|DELETE /api/v1/contratos-servicio/{id}/facturas/{factura_id}

    **En el PATCH, `frontera_ids` ausente y `frontera_ids: []` NO son lo mismo**:
    el primero deja las fronteras como estaban, el segundo las desvincula todas.
    Igual con `enlace_drive`: vacío lo borra.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = ct_models.ContratoServicio.objects.all()

    def get_queryset(self):
        return cs_queryset.con_relaciones()

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return cs_serializers.ContratoEscrituraSerializer
        return cs_serializers.ContratoSerializer

    def _contrato(self, pk):
        contrato = self.get_queryset().filter(pk=pk).first()
        if contrato is None:
            raise NotFound("Contrato no encontrado")
        return contrato

    # ── Listado y escritura ───────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        limite = request.query_params.get("limit", str(cs_queryset.LIMITE_MAXIMO))
        if not limite.isdigit() or not 1 <= int(limite) <= cs_queryset.LIMITE_MAXIMO:
            raise ValidationError(
                {"limit": f"Entero entre 1 y {cs_queryset.LIMITE_MAXIMO}."}
            )
        contratos = cs_queryset.listar(
            tipo=request.query_params.get("tipo"),
            proyecto_id=request.query_params.get("proyecto_id"),
            codigo_tsf=request.query_params.get("codigo_tsf"),
            limite=int(limite),
        )
        return Response(
            cs_serializers.ContratoSerializer(contratos, many=True).data
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        datos = dict(serializer.validated_data)
        frontera_ids = datos.pop("frontera_ids", None) or []
        enlace = datos.pop("enlace_drive", None)

        with transaction.atomic():
            contrato = ct_models.ContratoServicio.objects.create(**datos)
            partes_service.sincronizar(contrato)
            self._fronteras(contrato, frontera_ids)
            if enlace:
                documentos_service.set_enlace(
                    contrato_servicio_id=contrato.id, url=enlace,
                    nombre=NOMBRE_ENLACE,
                )
        return Response(
            cs_serializers.ContratoSerializer(self._contrato(contrato.pk)).data,
            status=201,
        )

    def partial_update(self, request, *args, **kwargs):
        contrato = self._contrato(kwargs["pk"])
        serializer = self.get_serializer(contrato, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        datos = dict(serializer.validated_data)
        frontera_ids = datos.pop("frontera_ids", None)
        toca_enlace = "enlace_drive" in datos
        enlace = datos.pop("enlace_drive", None)

        with transaction.atomic():
            for campo, valor in datos.items():
                setattr(contrato, campo, valor)
            contrato.save()
            if toca_enlace:
                documentos_service.set_enlace(
                    contrato_servicio_id=contrato.id, url=enlace,
                    nombre=NOMBRE_ENLACE,
                )
            partes_service.sincronizar(contrato)
            if frontera_ids is not None:
                self._fronteras(contrato, frontera_ids)

        return Response(
            cs_serializers.ContratoSerializer(self._contrato(contrato.pk)).data
        )

    @staticmethod
    def _fronteras(contrato, frontera_ids):
        try:
            fronteras_service.sincronizar(contrato, frontera_ids)
        except fronteras_service.FronteraInvalida as exc:
            raise ValidationError(str(exc))

    # ── Indexación y deduplicación ────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="importar-indexacion")
    @log_endpoint(name="Operaciones | Contratos | Importar indexación")
    def importar_indexacion(self, request):
        tipo = request.query_params.get("tipo")
        if tipo not in indexacion_service.CAMPO_POR_TIPO:
            raise ValidationError("tipo debe ser 'anual' o 'mensual'")
        entrada = cs_serializers.ImportarIndexacionSerializer(
            data=request.data, many=True
        )
        entrada.is_valid(raise_exception=True)
        return Response(
            indexacion_service.importar(tipo, entrada.validated_data)
        )

    @action(detail=False, methods=["get"], url_path="duplicados-representacion")
    def duplicados_representacion(self, request):
        """Informe de contratos de representación repetidos. Solo lee.

        Separa los grupos que se pueden fusionar sin perder nada de los que se
        contradicen y necesitan que alguien decida.
        """
        from apps.contratos.services.representacion_dedup import revisar

        return Response(
            revisar(dedup_service.contratos_de_representacion())
        )

    @action(detail=False, methods=["post"], url_path="fusionar-representacion")
    @log_endpoint(name="Operaciones | Contratos | Fusionar representación")
    def fusionar_representacion(self, request):
        entrada = cs_serializers.FusionarSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(
            dedup_service.fusionar(entrada.validated_data.get("ids"))
        )

    # ── Facturas ──────────────────────────────────────────────────────────

    @action(detail=True, methods=["get", "post"], url_path="facturas")
    @log_endpoint(name="Operaciones | Contratos | Facturas")
    def facturas(self, request, pk=None):
        contrato = self._contrato(pk)
        if request.method == "GET":
            filas = fa_models.ContratoFactura.objects.filter(contrato=contrato)
            tipo = request.query_params.get("tipo")
            if tipo:
                filas = filas.filter(tipo=tipo)
            return Response(cs_serializers.FacturaSerializer(
                filas.order_by("-fecha"), many=True
            ).data)

        entrada = cs_serializers.FacturaEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        factura = entrada.save(contrato=contrato)
        return Response(
            cs_serializers.FacturaSerializer(factura).data, status=201
        )

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"facturas/(?P<factura_id>[0-9]+)",
    )
    @log_endpoint(name="Operaciones | Contratos | Factura")
    def factura(self, request, pk=None, factura_id=None):
        factura = fa_models.ContratoFactura.objects.filter(
            pk=factura_id, contrato_id=pk
        ).first()
        if factura is None:
            raise NotFound("Factura no encontrada")
        if request.method == "DELETE":
            factura.delete()
            return Response(status=204)

        entrada = cs_serializers.FacturaEscrituraSerializer(
            factura, data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        return Response(cs_serializers.FacturaSerializer(entrada.save()).data)
