"""ViewSets de contratos PPA, sus responsables y el IPP mensual."""

import logging
from datetime import datetime, timezone

from django.db import transaction
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.clientes.services import documentos as documentos_service
from apps.ppa import models as ppa_models
from apps.ppa.services import contratos as contratos_service
from apps.ppa.services import escritura as escritura_service
from apps.ppa.services import responsables as responsables_service

from . import queryset as ppa_queryset
from . import serializers as ppa_serializers

logger = logging.getLogger("operaciones.ppa")

NOMBRE_ENLACE = "Enlace Drive del contrato"


@class_logger_wrapper(name="Operaciones | PPA | Contratos")
class PpaViewSet(
    viewsets.GenericViewSet, mixins.ListModelMixin, mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
):
    """Contratos PPA.

    GET    /api/v1/ppa[?proyecto_id=&q=&tipo_contrato=&limit=]
    POST   /api/v1/ppa                          → 201
    GET    /api/v1/ppa/partes                   compradores y vendedores
    GET    /api/v1/ppa/resumen-global           cartera con visibilidad
    GET|PATCH|DELETE /api/v1/ppa/{id}           el DELETE es soft (409 si tiene datos)
    PUT    /api/v1/ppa/{id}/tarifas             reemplaza TODAS
    PUT    /api/v1/ppa/{id}/compromisos         reemplaza TODOS
    GET|PUT /api/v1/ppa/ipp/mensual
    GET|POST /api/v1/ppa/responsables · PATCH|DELETE /responsables/{rid}
    POST   /api/v1/ppa/responsables/asignar

    **`estado_cumplimiento` y compañía solo viajan en el detalle y en el
    resumen**, no en el listado: se calculan por contrato con dos consultas cada
    uno, y en una lista de 500 eso son mil consultas.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]
    queryset = ppa_models.PpaContrato.objects.filter(deleted_at__isnull=True)

    def get_serializer_class(self):
        if self.action == "create":
            return ppa_serializers.ContratoCreacionSerializer
        if self.action == "partial_update":
            return ppa_serializers.ContratoEscrituraSerializer
        return ppa_serializers.ContratoSerializer

    def get_queryset(self):
        return ppa_queryset.con_relaciones()

    # ── Listado y detalle ─────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        limite = request.query_params.get("limit", str(ppa_queryset.LIMITE_MAXIMO))
        if not limite.isdigit() or not 1 <= int(limite) <= ppa_queryset.LIMITE_MAXIMO:
            raise ValidationError(
                {"limit": f"Entero entre 1 y {ppa_queryset.LIMITE_MAXIMO}."}
            )
        contratos = ppa_queryset.listar(
            proyecto_id=request.query_params.get("proyecto_id"),
            q=request.query_params.get("q"),
            tipo_contrato=request.query_params.get("tipo_contrato"),
            limite=int(limite),
        )
        return Response(
            ppa_serializers.ContratoSerializer(contratos, many=True).data
        )

    def retrieve(self, request, *args, **kwargs):
        contrato = self._contrato(kwargs["pk"])
        datos = ppa_serializers.ContratoSerializer(contrato).data
        datos.update(contratos_service.visibilidad(contrato))
        return Response(datos)

    def _contrato(self, pk):
        contrato = ppa_queryset.con_relaciones().filter(pk=pk).first()
        if contrato is None:
            raise NotFound("Contrato PPA no encontrado")
        return contrato

    # ── Escritura ─────────────────────────────────────────────────────────

    def create(self, request, *args, **kwargs):
        """Crea el contrato COMPLETO: plantas, tarifas y compromisos incluidos.

        Toda la lógica está en `escritura.crear_ppa`, que es también la que usa
        `/comercial/ofertas/{id}/firmar`. Antes cada camino aplicaba sus propias
        reglas sobre la misma tabla (ver `docs/DIAGNOSTICO_PPA.md` §2).

        La respuesta trae `avisos`: lo que quedó cojo —sin plantas, sin tarifas,
        sin compromisos— para que la UI lo muestre. No son errores; el contrato
        existe.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        datos = dict(serializer.validated_data)

        # `tipo_contrato` sale de `datos` y viaja como parámetro propio: la
        # función no lo adivina. Ausente = "venta", que es la convención que ya
        # leen los seis módulos de Cumplimiento con `(tipo or "venta")`.
        tipo = datos.pop("tipo_contrato", None) or "venta"
        proyecto_ids = datos.pop("proyecto_ids", None) or []
        tarifas = datos.pop("tarifas", None) or []
        compromisos = datos.pop("compromisos", None) or []
        enlace = datos.pop("carpeta_link", None)

        try:
            resultado = escritura_service.crear_ppa(
                tipo_contrato=tipo, datos=datos, proyecto_ids=proyecto_ids,
                tarifas=tarifas, compromisos=compromisos, carpeta_link=enlace,
            )
        except contratos_service.ReglaPpa as exc:
            raise NoProcesable(str(exc))
        except ValueError as exc:
            raise NoProcesable(str(exc))

        cuerpo = ppa_serializers.ContratoSerializer(
            self._contrato(resultado.contrato.pk)
        ).data
        cuerpo["avisos"] = resultado.avisos
        return Response(cuerpo, status=201)

    def partial_update(self, request, *args, **kwargs):
        contrato = self._contrato(kwargs["pk"])
        serializer = self.get_serializer(contrato, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        datos = dict(serializer.validated_data)
        proyecto_ids = datos.pop("proyecto_ids", None)
        # Se distingue «no vino» de «vino vacío»: lo segundo BORRA el enlace.
        toca_enlace = "carpeta_link" in datos
        enlace = datos.pop("carpeta_link", None)

        with transaction.atomic():
            for campo, valor in datos.items():
                setattr(contrato, campo, valor)
            contrato.save()
            if toca_enlace:
                documentos_service.set_enlace(
                    ppa_contrato_id=contrato.id, url=enlace, nombre=NOMBRE_ENLACE
                )
            if proyecto_ids is not None:
                contratos_service.fijar_proyectos(contrato, proyecto_ids)
            contratos_service.sincronizar_partes(contrato)
            self._validar(contrato)

        return Response(
            ppa_serializers.ContratoSerializer(self._contrato(contrato.pk)).data
        )

    @staticmethod
    def _validar(contrato):
        try:
            contratos_service.validar_fecha_fin_vs_asic(contrato)
        except contratos_service.ReglaPpa as exc:
            raise NoProcesable(str(exc))

    def destroy(self, request, *args, **kwargs):
        """Borrado LÓGICO. 409 si de él cuelgan liquidaciones o registros GESCON."""
        contrato = self._contrato(kwargs["pk"])
        razones = contratos_service.razones_para_no_borrar(contrato)
        if razones:
            raise Conflict(f'No se puede eliminar: {"; ".join(razones)}.')

        # `datetime` de Python y no `Now()`: asignar una expresión SQL a la
        # columna hace que el hook de auditoría (`old != new`) reviente con
        # "Boolean value of this clause is not defined" y el borrado da 500.
        contrato.deleted_at = datetime.now(timezone.utc)
        contrato.save(update_fields=["deleted_at"])
        return Response(status=204)

    # ── Consultas auxiliares ──────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="partes")
    def partes(self, request):
        return Response(ppa_queryset.partes())

    @action(detail=False, methods=["get"], url_path="resumen-global")
    def resumen_global(self, request):
        return Response(ppa_queryset.build_resumen_global())

    # ── Tarifas y compromisos ─────────────────────────────────────────────

    @action(detail=True, methods=["put"], url_path="tarifas")
    @log_endpoint(name="Operaciones | PPA | Tarifas")
    def tarifas(self, request, pk=None):
        """Reemplaza TODAS las tarifas del contrato.

        Se borra y se reinserta en vez de hacer upsert por período: el cliente
        manda la tabla completa y así un período eliminado en pantalla también
        desaparece de la base.
        """
        contrato = self._contrato(pk)
        entrada = ppa_serializers.TarifaEntradaSerializer(
            data=request.data, many=True
        )
        entrada.is_valid(raise_exception=True)

        with transaction.atomic():
            ppa_models.PpaTarifa.objects.filter(contrato=contrato).delete()
            ppa_models.PpaTarifa.objects.bulk_create([
                ppa_models.PpaTarifa(contrato=contrato, **fila)
                for fila in entrada.validated_data
            ])
        filas = ppa_models.PpaTarifa.objects.filter(
            contrato=contrato
        ).order_by("año", "mes")
        return Response(ppa_serializers.TarifaSerializer(filas, many=True).data)

    @action(detail=True, methods=["put"], url_path="compromisos")
    @log_endpoint(name="Operaciones | PPA | Compromisos")
    def compromisos(self, request, pk=None):
        """Reemplaza TODOS los compromisos de energía del contrato."""
        contrato = self._contrato(pk)
        entrada = ppa_serializers.CompromisoEntradaSerializer(
            data=request.data, many=True
        )
        entrada.is_valid(raise_exception=True)

        with transaction.atomic():
            ppa_models.PpaCompromisoEnergia.objects.filter(
                contrato=contrato
            ).delete()
            ppa_models.PpaCompromisoEnergia.objects.bulk_create([
                ppa_models.PpaCompromisoEnergia(contrato=contrato, **fila)
                for fila in entrada.validated_data
            ])
        filas = ppa_models.PpaCompromisoEnergia.objects.filter(
            contrato=contrato
        ).order_by("año", "mes")
        return Response(
            ppa_serializers.CompromisoSerializer(filas, many=True).data
        )

    # ── IPP mensual global ────────────────────────────────────────────────

    @action(detail=False, methods=["get", "put"], url_path="ipp/mensual")
    @log_endpoint(name="Operaciones | PPA | IPP mensual")
    def ipp_mensual(self, request):
        """El IPP global, numerador de la indexación de energía.

        El PUT es un upsert por (año, mes): **no borra los demás períodos**, a
        diferencia de tarifas y compromisos. Es una tabla global, no del
        contrato, y el cliente manda solo lo que cambió.
        """
        if request.method == "PUT":
            entrada = ppa_serializers.IppMensualSerializer(
                data=request.data, many=True
            )
            entrada.is_valid(raise_exception=True)
            with transaction.atomic():
                for fila in entrada.validated_data:
                    ppa_models.IppMensual.objects.update_or_create(
                        **{"año": fila["año"], "mes": fila["mes"]},
                        defaults={"valor": fila["valor"]},
                    )

        filas = ppa_models.IppMensual.objects.order_by("año", "mes")
        return Response([
            {"año": f.año, "mes": f.mes, "valor": float(f.valor)}
            for f in filas
        ])

    # ── Responsables ──────────────────────────────────────────────────────

    @action(detail=False, methods=["get", "post"], url_path="responsables")
    @log_endpoint(name="Operaciones | PPA | Responsables")
    def responsables(self, request):
        if request.method == "GET":
            return Response(ppa_serializers.ResponsableSerializer(
                responsables_service.con_conteo(), many=True
            ).data)

        entrada = ppa_serializers.ResponsableEntradaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        try:
            nombre = responsables_service.validar_nombre_libre(
                entrada.validated_data["nombre"]
            )
        except responsables_service.NombreDuplicado as exc:
            raise Conflict(str(exc))
        except ValueError as exc:
            raise NoProcesable(str(exc))

        responsable = ppa_models.PpaResponsable.objects.create(
            nombre=nombre,
            incluir_en_cumplimiento=entrada.validated_data[
                "incluir_en_cumplimiento"
            ],
        )
        datos = ppa_serializers.ResponsableSerializer(responsable).data
        datos["n_contratos"] = 0
        return Response(datos, status=201)

    @action(
        detail=False, methods=["patch", "delete"],
        url_path=r"responsables/(?P<rid>[0-9]+)",
    )
    @log_endpoint(name="Operaciones | PPA | Responsable")
    def responsable(self, request, rid=None):
        responsable = ppa_models.PpaResponsable.objects.filter(pk=rid).first()
        if responsable is None:
            raise NotFound("Responsable no encontrado")

        if request.method == "DELETE":
            try:
                responsables_service.borrar(responsable)
            except responsables_service.TieneContratos as exc:
                raise Conflict(str(exc))
            return Response(status=204)

        entrada = ppa_serializers.ResponsableUpdateSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cambios = entrada.validated_data
        if cambios.get("nombre") is not None:
            try:
                responsable.nombre = responsables_service.validar_nombre_libre(
                    cambios["nombre"], excepto_id=responsable.pk
                )
            except responsables_service.NombreDuplicado as exc:
                raise Conflict(str(exc))
            except ValueError as exc:
                raise NoProcesable(str(exc))
        if "incluir_en_cumplimiento" in cambios:
            responsable.incluir_en_cumplimiento = cambios[
                "incluir_en_cumplimiento"
            ]
        responsable.save()

        datos = ppa_serializers.ResponsableSerializer(responsable).data
        datos["n_contratos"] = responsables_service.contratos_vivos(
            responsable.pk
        )
        return Response(datos)

    @action(detail=False, methods=["post"], url_path="responsables/asignar")
    @log_endpoint(name="Operaciones | PPA | Asignar responsable")
    def asignar_responsable(self, request):
        entrada = ppa_serializers.AsignarResponsableSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        responsable_id = datos.get("responsable_id")
        if responsable_id is not None and not ppa_models.PpaResponsable.objects.filter(
            pk=responsable_id
        ).exists():
            raise NotFound("Responsable no encontrado")

        return Response({
            "actualizados": responsables_service.asignar(
                datos["contrato_ids"], responsable_id
            )
        })
