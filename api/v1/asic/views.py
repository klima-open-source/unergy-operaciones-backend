"""ViewSet de GESCON/ASIC."""

from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.mercado_xm import models as mx_models
from apps.mercado_xm.services import (
    asic_backfill, asic_borrado, asic_modificacion, asic_terminacion,
    asic_vigencia,
)
from apps.mercado_xm.services.asic_errores import (
    Bloqueado, NoEncontrado, ReglaAsic,
)
from apps.mercado_xm.services.asic_reglas import (
    auto_terminar, normalizar_modalidad_pago, validar_fecha_fin_vs_ppa,
    validar_flags_exclusivos,
)

from . import serializers as asic_serializers


def _traducir(funcion, *args, **kwargs):
    """Convierte los errores del dominio en los códigos HTTP de siempre."""
    try:
        return funcion(*args, **kwargs)
    except ReglaAsic as exc:
        raise NoProcesable(str(exc))
    except NoEncontrado as exc:
        raise NotFound(str(exc))
    except Bloqueado as exc:
        raise Conflict(str(exc))


def _dry_run(request) -> bool:
    """Por defecto TRUE: un backfill no debe escribir por accidente."""
    return request.query_params.get("dry_run", "true").lower() not in ("false", "0")


@class_logger_wrapper(name="Operaciones | Mercado XM | GESCON")
class AsicViewSet(
    viewsets.GenericViewSet, mixins.ListModelMixin, mixins.CreateModelMixin,
    mixins.UpdateModelMixin, mixins.DestroyModelMixin,
):
    """Registros GESCON ante el ASIC.

    GET    /api/v1/asic[?codigo_sic_contrato=&contrato_interno=&proyecto_id=&contrato_ppa_id=]
    POST   /api/v1/asic                        → 201
    PATCH  /api/v1/asic/{id}
    DELETE /api/v1/asic/{id}                   → 204; 409 si alimenta Cumplimiento
    POST   /api/v1/asic/modificacion           → 201, hereda del SIC
    POST   /api/v1/asic/terminacion            → 201, cierra los registros del SIC
    POST   /api/v1/asic/cambios                → 201
    GET    /api/v1/asic/gescon/diccionario
    POST   /api/v1/asic/gescon/diccionario     upsert por código de contrato
    POST   /api/v1/asic/backfill-nombre-interno[?dry_run=true]
    POST   /api/v1/asic/backfill-terminaciones[?dry_run=true]

    **La vigencia efectiva no se guarda: se deriva.** `fecha_fin_efectiva` y
    `es_version_vigente` salen de recorrer todas las publicadas por
    `fecha_inicio`; ver `apps/mercado_xm/services/asic_vigencia.py`.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = mx_models.AsicSolicitud.objects.all()

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return asic_serializers.SolicitudEscrituraSerializer
        return asic_serializers.SolicitudSerializer

    def list(self, request, *args, **kwargs):
        """Los registros GESCON, filtrables.

        **`contrato_ppa_id` filtra por la LLAVE; `contrato_interno`, por texto.**
        Los dos existen porque el emparejamiento por código de contrato es el
        respaldo histórico, pero la llave es la fuente de verdad: si alguien
        edita `numero_codigo_contrato`, el filtro por texto deja de encontrar los
        registros y la llave no. Es el filtro que usa el detalle del PPA para
        mostrar sus registros reales en vez de la copia que vivía en
        `ppa_contratos` (ver `docs/DIAGNOSTICO_PPA.md` §4).
        """
        consulta = mx_models.AsicSolicitud.objects.select_related("proyecto")
        for parametro, campo in (
            ("codigo_sic_contrato", "codigo_sic_contrato"),
            ("contrato_interno", "contrato_interno"),
            ("proyecto_id", "proyecto_id"),
            ("contrato_ppa_id", "contrato_ppa_id"),
        ):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{campo: valor})
        filas = list(consulta.order_by("-fecha_solicitud", "-id"))
        return Response(self._salida(filas, many=True))

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        solicitud = self._guardar(serializer.save())
        return Response(self._salida([solicitud])[0], status=201)

    def partial_update(self, request, *args, **kwargs):
        solicitud = self.get_object()
        serializer = self.get_serializer(solicitud, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return Response(self._salida([self._guardar(serializer.save())])[0])

    def _guardar(self, solicitud):
        """Las tres validaciones y el efecto de terminación, en un solo sitio."""
        _traducir(
            validar_flags_exclusivos,
            bool(solicitud.es_duplicado), bool(solicitud.uso_del_recurso),
        )
        solicitud.modalidad_pago = _traducir(
            normalizar_modalidad_pago, solicitud.modalidad_pago
        )
        solicitud.save(update_fields=["modalidad_pago"])
        _traducir(validar_fecha_fin_vs_ppa, solicitud)
        auto_terminar(solicitud)
        solicitud.refresh_from_db()
        return solicitud

    def destroy(self, request, *args, **kwargs):
        _traducir(asic_borrado.borrar, self.get_object())
        return Response(status=204)

    # ── Modificación y terminación ────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="modificacion")
    @log_endpoint(name="Operaciones | Mercado XM | GESCON | Modificación")
    def modificacion(self, request):
        entrada = asic_serializers.ModificacionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        resultado = _traducir(asic_modificacion.crear, entrada.validated_data)

        filas = [resultado["modificacion"]]
        if resultado["saliente"] is not None:
            filas.append(resultado["saliente"])
        salida = self._salida(filas)
        return Response({
            "modificacion": salida[0],
            "saliente": salida[1] if resultado["saliente"] is not None else None,
            "resumen": resultado["resumen"],
        }, status=201)

    @action(detail=False, methods=["post"], url_path="terminacion")
    @log_endpoint(name="Operaciones | Mercado XM | GESCON | Terminación")
    def terminacion(self, request):
        entrada = asic_serializers.TerminacionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        resultado = _traducir(asic_terminacion.crear, entrada.validated_data)

        salida = self._salida(
            [resultado["terminacion"], *resultado["cerrados"]]
        )
        return Response({
            "terminacion": salida[0],
            "cerrados": salida[1:],
            "resumen": resultado["resumen"],
        }, status=201)

    # ── Cambios de contrato y diccionario GESCON ──────────────────────────

    @action(detail=False, methods=["post"], url_path="cambios")
    def cambios(self, request):
        serializer = asic_serializers.CambioSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=201)

    @action(detail=False, methods=["get", "post"], url_path="gescon/diccionario")
    def diccionario(self, request):
        if request.method == "GET":
            filas = mx_models.GesconDiccionarioContrato.objects.order_by(
                "codigo_contrato"
            )
            return Response(
                asic_serializers.DiccionarioSerializer(filas, many=True).data
            )

        serializer = asic_serializers.DiccionarioSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        datos = serializer.validated_data
        entrada, _ = mx_models.GesconDiccionarioContrato.objects.update_or_create(
            codigo_contrato=datos["codigo_contrato"],
            defaults={"nombre": datos.get("nombre")},
        )
        return Response(
            asic_serializers.DiccionarioSerializer(entrada).data, status=201
        )

    # ── Backfills ─────────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="backfill-nombre-interno")
    @log_endpoint(name="Operaciones | Mercado XM | GESCON | Backfill nombre")
    def backfill_nombre_interno(self, request):
        return Response(asic_backfill.nombre_interno(_dry_run(request)))

    @action(detail=False, methods=["post"], url_path="backfill-terminaciones")
    @log_endpoint(name="Operaciones | Mercado XM | GESCON | Backfill terminaciones")
    def backfill_terminaciones(self, request):
        return Response(asic_backfill.terminaciones(_dry_run(request)))

    @staticmethod
    def _salida(filas: list, many: bool = False):
        datos = asic_serializers.SolicitudSerializer(
            asic_vigencia.preparar(filas), many=True
        ).data
        return datos
