"""ViewSet de liquidaciones y del resumen espejo del Panel Contable."""

import logging
from datetime import datetime, timezone

from django.db import IntegrityError, transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.liquidaciones import models as lq_models
from apps.liquidaciones.services import excel as excel_service
from apps.liquidaciones.services import impuestos, panel, resumen_panel
from apps.mandatos import models as md_models

from . import serializers as lq_serializers

logger = logging.getLogger("operaciones.liquidaciones")

TIPOS_PANEL = ("preliquidacion", "oficial")
ROLES_ESCRITURA = ["admin", "liquidaciones"]

# Catálogos de enums, tal como los expone el contrato.
CATALOGOS = {
    "tipo_costo": [
        "energia", "comercializacion", "operacion", "mantenimiento",
        "arriendo", "administracion", "otro",
    ],
    "tipo_mandato": ["ingreso", "costo"],
    "tipo_linea_mandato": ["ingreso", "costo", "retencion", "iva"],
    "tipo_factura_servicio": ["representacion", "cgm", "administracion", "om"],
    "estado_liquidacion": [
        "iniciada", "en_proceso", "revisada", "firmada", "cerrada",
    ],
    "estado_mandato": [
        "pendiente", "generado", "enviado_revisoria", "firmado",
    ],
    "estado_factura": ["pendiente", "emitida", "pagada", "anulada"],
}


@class_logger_wrapper(name="Operaciones | Liquidaciones")
class LiquidacionViewSet(viewsets.GenericViewSet):
    """Liquidaciones y el resumen espejo del Panel Contable.

    GET|POST /api/v1/liquidaciones[?page=&size=&proyecto_id=&periodo_desde=
              &periodo_hasta=&estado=]
    GET  /api/v1/liquidaciones/resumen-panel?periodo=YYYY-MM&tipo=
    GET  /api/v1/liquidaciones/resumen-panel-rango?periodo_desde=&periodo_hasta=
    GET|PATCH|DELETE /api/v1/liquidaciones/{id}     el DELETE es lógico
    GET|PUT /api/v1/liquidaciones/{id}/informe      HTML del PDF
    DELETE /api/v1/liquidaciones/{id}/limpiar       borra el detalle operativo
    GET  /api/v1/liquidaciones/catalogos/tipos
    POST /api/v1/liquidaciones/cargar-excel

    **Escribir exige rol `admin` o `liquidaciones`**; leer, solo estar
    autenticado.

    El resumen es un ESPEJO del Panel: los tres tabs de la pantalla salen de
    ahí, así que cuadran siempre. La tabla `liquidaciones` guarda el detalle
    operativo, y por eso el resumen incluye el `liquidacion_id` de cada
    proyecto para poder navegar al detalle.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "put", "delete", "head", "options"]
    queryset = lq_models.Liquidacion.objects.filter(deleted_at__isnull=True)

    ESCRITURAS = (
        "create", "partial_update", "destroy", "limpiar", "informe",
        "cargar_excel",
    )

    def get_permissions(self):
        if self.action in self.ESCRITURAS:
            self.required_role = ROLES_ESCRITURA
        return super().get_permissions()

    def _liquidacion(self, pk):
        fila = lq_models.Liquidacion.objects.filter(
            pk=pk, deleted_at__isnull=True
        ).select_related("proyecto").first()
        if fila is None:
            raise NotFound("Liquidación no encontrada")
        return fila

    @staticmethod
    def _entero(request, nombre, defecto, minimo, maximo):
        crudo = request.query_params.get(nombre)
        if crudo in (None, ""):
            return defecto
        if not crudo.isdigit() or not minimo <= int(crudo) <= maximo:
            raise ValidationError({nombre: f"Entero entre {minimo} y {maximo}."})
        return int(crudo)

    @staticmethod
    def _tipo(request) -> str:
        tipo = request.query_params.get("tipo", "preliquidacion")
        if tipo not in TIPOS_PANEL:
            raise ValidationError(
                {"tipo": f'Uno de: {", ".join(TIPOS_PANEL)}.'}
            )
        return tipo

    # ── CRUD ──────────────────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        consulta = lq_models.Liquidacion.objects.filter(
            deleted_at__isnull=True
        ).select_related("proyecto")
        for parametro, filtro in (
            ("proyecto_id", "proyecto_id"),
            ("periodo_desde", "periodo__gte"),
            ("periodo_hasta", "periodo__lte"),
            ("estado", "estado"),
        ):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{filtro: valor})

        pagina = self._entero(request, "page", 1, 1, 10**6)
        tamano = self._entero(request, "size", 20, 1, 200)
        total = consulta.count()
        inicio = (pagina - 1) * tamano
        items = consulta.order_by("-periodo")[inicio:inicio + tamano]

        return Response({
            "items": lq_serializers.LiquidacionSerializer(items, many=True).data,
            "total": total,
            "page": pagina,
            "size": tamano,
            "pages": (total + tamano - 1) // tamano,
        })

    def create(self, request, *args, **kwargs):
        entrada = lq_serializers.CrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        try:
            liquidacion = lq_models.Liquidacion.objects.create(
                proyecto_id=datos["proyecto_id"],
                generado_por_id=request.user.id,
                periodo=datos["periodo"],
                tipo_venta=datos["tipo_venta"],
                estado="iniciada",
                observaciones_resultados=datos.get("observaciones_resultados"),
            )
        except IntegrityError:
            raise Conflict(
                "Ya existe una liquidación para este proyecto y período"
            )
        return Response(
            {"id": liquidacion.id, "msg": "Liquidación creada"}, status=201
        )

    def retrieve(self, request, *args, **kwargs):
        return Response(lq_serializers.LiquidacionSerializer(
            self._liquidacion(kwargs["pk"])
        ).data)

    def partial_update(self, request, *args, **kwargs):
        liquidacion = self._liquidacion(kwargs["pk"])
        entrada = lq_serializers.ActualizarSerializer(
            liquidacion, data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        entrada.save()
        return Response({"msg": "Actualizada"})

    def destroy(self, request, *args, **kwargs):
        """Borrado LÓGICO: el detalle operativo se conserva."""
        liquidacion = self._liquidacion(kwargs["pk"])
        liquidacion.deleted_at = datetime.now(timezone.utc)
        liquidacion.save(update_fields=["deleted_at"])
        return Response(status=204)

    # ── Resumen espejo ────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="resumen-panel")
    def resumen_panel(self, request):
        periodo = request.query_params.get("periodo")
        tipo = self._tipo(request)
        try:
            periodo_norm, periodo_fecha = panel.normalizar_periodo(periodo or "")
        except Exception:
            raise NoProcesable(
                "El período debe tener formato YYYY-MM"
            )

        paneles = list(panel.paneles_de(periodo_norm, tipo))
        proyecto_ids = [p.proyecto_id for p in paneles]
        nombres, tipos = panel.nombres_y_tipos(proyecto_ids)
        clientes = panel.clientes_por_inversionista(
            panel.ids_de_inversionista(paneles)
        )

        resultado = resumen_panel.construir(
            paneles, periodo_norm, tipo, nombres, tipos,
            panel.liquidacion_por_proyecto(proyecto_ids, periodo_fecha),
            clientes,
            impuestos.overrides_tasa_servicio(
                {c.get("cliente_id") for c in clientes.values()}
            ),
        )
        # La alerta de «falta cargar el ER» solo aplica a la preliquidación:
        # en la oficial ya no hay nada que cargar.
        resultado["sin_panel"] = (
            panel.proyectos_sin_panel(proyecto_ids)
            if tipo == "preliquidacion" else []
        )
        return Response(resultado)

    @action(detail=False, methods=["get"], url_path="resumen-panel-rango")
    def resumen_panel_rango(self, request):
        """Un resumen-panel por mes, para las gráficas de tendencia."""
        tipo = self._tipo(request)
        try:
            desde, desde_fecha = panel.normalizar_periodo(
                request.query_params.get("periodo_desde") or ""
            )
            hasta, hasta_fecha = panel.normalizar_periodo(
                request.query_params.get("periodo_hasta") or ""
            )
        except Exception:
            raise NoProcesable(
                "Los períodos deben tener formato YYYY-MM"
            )

        paneles = list(panel.paneles_en_rango(desde, hasta, tipo))
        por_periodo: dict[str, list] = {}
        for fila in paneles:
            por_periodo.setdefault(fila.periodo, []).append(fila)

        proyecto_ids = {p.proyecto_id for p in paneles}
        nombres, tipos = panel.nombres_y_tipos(proyecto_ids)
        clientes = panel.clientes_por_inversionista(
            panel.ids_de_inversionista(paneles)
        )
        overrides = impuestos.overrides_tasa_servicio(
            {c.get("cliente_id") for c in clientes.values()}
        )
        liquidaciones = panel.liquidaciones_en_rango(
            proyecto_ids, desde_fecha, hasta_fecha
        )

        periodos = []
        for periodo in sorted(por_periodo):
            del_periodo = por_periodo[periodo]
            periodos.append(resumen_panel.construir(
                del_periodo, periodo, tipo, nombres, tipos,
                {
                    p.proyecto_id: liquidaciones.get((p.proyecto_id, periodo))
                    for p in del_periodo
                },
                clientes, overrides,
            ))
        return Response({"tipo": tipo, "periodos": periodos})

    # ── Informe y limpieza ────────────────────────────────────────────────

    @action(detail=True, methods=["get", "put"], url_path="informe")
    @log_endpoint(name="Operaciones | Liquidaciones | Informe")
    def informe(self, request, pk=None):
        """El HTML editable del PDF de la liquidación."""
        liquidacion = self._liquidacion(pk)
        if request.method == "PUT":
            entrada = lq_serializers.InformeSerializer(data=request.data)
            entrada.is_valid(raise_exception=True)
            liquidacion.informe_html = entrada.validated_data.get("html_content")
            liquidacion.informe_actualizado_en = datetime.now(timezone.utc)
            liquidacion.save(update_fields=[
                "informe_html", "informe_actualizado_en",
            ])
            return Response({
                "msg": "Informe guardado",
                "actualizado_en": liquidacion.informe_actualizado_en.isoformat(),
            })

        return Response({
            "id": liquidacion.id,
            "html_content": liquidacion.informe_html,
            "actualizado_en": (
                liquidacion.informe_actualizado_en.isoformat()
                if liquidacion.informe_actualizado_en else None
            ),
        })

    @action(detail=True, methods=["delete"], url_path="limpiar")
    @log_endpoint(name="Operaciones | Liquidaciones | Limpiar")
    def limpiar(self, request, pk=None):
        """Borra el detalle operativo (mandatos, costos, facturas).

        La liquidación NO se borra: queda vacía para volver a importarla.
        """
        liquidacion = self._liquidacion(pk)
        with transaction.atomic():
            mandatos = md_models.LiquidacionMandato.objects.filter(
                liquidacion=liquidacion
            )
            md_models.LiquidacionMandatoLinea.objects.filter(
                mandato__in=mandatos
            ).delete()
            mandatos.delete()
            lq_models.LiquidacionCosto.objects.filter(
                liquidacion=liquidacion
            ).delete()
            lq_models.LiquidacionFactura.objects.filter(
                liquidacion=liquidacion
            ).delete()
        return Response(status=204)

    # ── Catálogos y carga ─────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="catalogos/tipos")
    def catalogos(self, request):
        return Response(CATALOGOS)

    @action(detail=False, methods=["post"], url_path="cargar-excel")
    @log_endpoint(name="Operaciones | Liquidaciones | Cargar Excel")
    def cargar_excel(self, request):
        """Carga el panel de seguimiento contable desde un Excel.

        `dry_run=true` devuelve la vista previa sin escribir; `limpiar=true`
        borra el detalle existente antes de reimportar.
        """
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        hoja = request.data.get("hoja")
        if not hoja:
            raise ValidationError({"hoja": "Requerido."})

        try:
            _, periodo_fecha = panel.normalizar_periodo(
                request.data.get("periodo") or ""
            )
        except Exception:
            raise NoProcesable(
                "El período debe tener formato YYYY-MM"
            )

        try:
            return Response(excel_service.cargar(
                archivo.read(), hoja, periodo_fecha.isoformat(),
                tipo_venta=request.data.get("tipo_venta", "ppa"),
                limpiar=request.data.get("limpiar", "false"),
                dry_run=request.data.get("dry_run", "false"),
                usuario_id=request.user.id,
            ))
        except excel_service.TipoVentaInvalido as exc:
            raise NoProcesable(str(exc))
        except ValueError as exc:
            raise NoProcesable(str(exc))
        except Exception as exc:
            logger.exception("Error procesando Excel de liquidaciones")
            return Response(
                {"detail": f"Error procesando el archivo: {exc}"}, status=500
            )
