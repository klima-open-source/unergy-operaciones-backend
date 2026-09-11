"""ViewSet de facturación de energía."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.facturacion import models as fa_models
from apps.facturacion.services import ajustes, calculo, cumplimiento, despacho
from apps.facturacion.services import despacho_xm, vs_despachos
from apps.liquidaciones.services import api_externa as api_liquidaciones
from apps.liquidaciones.services import proxy
from apps.mercado_xm import models as mx_models
from apps.mercado_xm.services import simem

from . import serializers as fa_serializers

# El cálculo nuestro está bien: lo que falló es la API de Liquidaciones.
HTTP_API_EXTERNA = 502


def _periodo(request, requerido=True) -> str:
    crudo = request.query_params.get("periodo")
    if not crudo:
        if not requerido:
            return ""
        raise ValidationError({"periodo": "Requerido, formato YYYY-MM"})
    try:
        return calculo.periodo_valido(crudo)
    except ValueError as exc:
        # 422 y no 400: es el código que devuelve el endpoint hoy.
        raise NoProcesable({"periodo": str(exc)})


@class_logger_wrapper(name="Operaciones | Facturación")
class FacturacionViewSet(viewsets.GenericViewSet):
    """Facturación de energía del mes: despacho, cálculo y ajustes.

    POST   /api/v1/facturacion/despacho?periodo=YYYY-MM   sube el Excel de XM
    GET    /api/v1/facturacion/despacho?periodo=
    GET    /api/v1/facturacion/despacho/dias?periodo=&contrato=
    GET    /api/v1/facturacion?periodo=                   el cálculo completo
    GET    /api/v1/facturacion/cumplimiento?periodo=
    GET    /api/v1/facturacion/agrupaciones
    PUT    /api/v1/facturacion/agrupaciones
    GET    /api/v1/facturacion/bolsa?periodo=
    PUT    /api/v1/facturacion/bolsa
    PUT    /api/v1/facturacion/orden
    DELETE /api/v1/facturacion/orden
    PUT    /api/v1/facturacion/emitida

    El cálculo se rehace en cada petición: nada de lo derivado se persiste, así
    que corregir una tarifa o un IPP se refleja de inmediato. Lo único guardado
    son los tres ajustes manuales (agrupación, precio de bolsa y orden) y la
    marca de emitida.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "put", "delete", "head", "options"]
    queryset = fa_models.FacturaAgrupacion.objects.none()

    def list(self, request, *args, **kwargs):
        return Response(calculo.periodo(_periodo(request)))

    # ── Despacho XM ───────────────────────────────────────────────────────

    @action(detail=False, methods=["get", "post"], url_path="despacho")
    @log_endpoint(name="Operaciones | Facturación | Despacho")
    def despacho(self, request):
        periodo = _periodo(request)
        if request.method == "GET":
            return self._listar_despacho(periodo)

        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError({"archivo": "Falta el archivo."})
        try:
            por_contrato, por_dia = despacho_xm.leer(archivo.read())
        except despacho_xm.ExcelInvalido as exc:
            return Response({"detail": str(exc)}, status=400)

        return Response(
            despacho.guardar(periodo, por_contrato, por_dia, archivo.name)
        )

    @staticmethod
    def _listar_despacho(periodo: str):
        filas = list(
            mx_models.DespachoContratoMensual.objects
            .filter(periodo=periodo).order_by("codigo_sic_contrato")
        )
        return Response({
            "periodo": periodo,
            "kwh_total": round(sum(float(f.kwh) for f in filas), 2),
            "contratos": [
                {
                    "contrato": f.codigo_sic_contrato, "vendedor": f.vendedor,
                    "comprador": f.comprador, "tipo": f.tipo,
                    "kwh": float(f.kwh), "dias": f.dias,
                }
                for f in filas
            ],
            "archivo": filas[0].archivo if filas else None,
        })

    @action(detail=False, methods=["get"], url_path="despacho/dias")
    def despacho_dias(self, request):
        """Día a día del despacho de un contrato en el período."""
        periodo = _periodo(request)
        contrato = (request.query_params.get("contrato") or "").strip()
        if not contrato:
            raise ValidationError({"contrato": "Requerido."})

        filas = list(
            mx_models.DespachoContratoDia.objects
            .filter(periodo=periodo, codigo_sic_contrato=contrato)
            .order_by("fecha")
        )
        return Response({
            "periodo": periodo, "contrato": contrato,
            "kwh_total": round(sum(float(f.kwh) for f in filas), 2),
            "dias": [
                {"fecha": f.fecha.isoformat(), "kwh": float(f.kwh)}
                for f in filas
            ],
        })

    # ── Cumplimiento ──────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="cumplimiento")
    def cumplimiento(self, request):
        periodo = _periodo(request)
        anio, mes = int(periodo[:4]), int(periodo[5:7])
        # Precio de bolsa del mes (COP/kWh) para valorar la energía incumplida:
        # SIMEM 709b84 horario (versión TXF, redondeo 2dec, promedio de todas las
        # horas), igual que el Excel de Compensación de la usuaria.
        precio_bolsa = simem.bolsa_mensual(anio, mes)["precio_bolsa"]
        return Response(cumplimiento.build(
            calculo.periodo(periodo), anio, mes, precio_bolsa=precio_bolsa,
        ))

    # ── Ingresos vs. despachos liquidados ─────────────────────────────────

    @action(detail=False, methods=["get"], url_path="vs-despachos")
    def vs_despachos(self, request):
        """Lo que DEBE entrar por proyecto contra lo que ya se liquidó.

        Lo liquidado sale de `market_settlements` de la API de Liquidaciones:
        `dispatch` (contrato) + `dispatch_fazni` (venta en bolsa), sumado de
        todos los contratos del proyecto y **sin restar las compras en bolsa**
        —esas se devuelven aparte, en `compras_bolsa`.
        """
        periodo = _periodo(request)
        anio, mes = int(periodo[:4]), int(periodo[5:7])
        resultado = calculo.periodo(periodo)
        try:
            despachos = api_liquidaciones.listar_liquidaciones_mercado(
                year=anio, month=mes,
                version=request.query_params.get("version", "txf"),
            )
        except api_liquidaciones.LiquidacionesAPIError as exc:
            # 502: el cálculo nuestro está bien, lo que falló es el otro lado.
            return Response({"detail": str(exc)}, status=HTTP_API_EXTERNA)

        filas = vs_despachos.comparar(
            resultado["lineas"], despachos,
            bolsa=resultado.get("bolsa_precio"),
            proyectos_por_topico=proxy.proyectos_por_topico(),
        )
        return Response({
            "periodo": periodo,
            "bolsa_precio": resultado.get("bolsa_precio"),
            "resumen": vs_despachos.totales(filas),
            "results": filas,
        })

    # ── Ajustes manuales ──────────────────────────────────────────────────

    @action(detail=False, methods=["get", "put"], url_path="agrupaciones")
    @log_endpoint(name="Operaciones | Facturación | Agrupaciones")
    def agrupaciones(self, request):
        if request.method == "PUT":
            entrada = fa_serializers.AgrupacionSerializer(
                data=request.data, many=True
            )
            entrada.is_valid(raise_exception=True)
            ajustes.guardar_agrupaciones(entrada.validated_data)
        return Response(ajustes.listar_agrupaciones())

    @action(detail=False, methods=["get", "put"], url_path="bolsa")
    @log_endpoint(name="Operaciones | Facturación | Precio de bolsa")
    def bolsa(self, request):
        if request.method == "PUT":
            entrada = fa_serializers.BolsaSerializer(data=request.data)
            entrada.is_valid(raise_exception=True)
            periodo = calculo.periodo_valido(entrada.validated_data["periodo"])
            ajustes.guardar_bolsa(
                int(periodo[:4]), int(periodo[5:7]),
                entrada.validated_data.get("valor"),
            )
        else:
            periodo = _periodo(request)

        manual = ajustes.leer_bolsa(int(periodo[:4]), int(periodo[5:7]))
        # `vigente` == `manual`: no hay sugerido automático a propósito, la
        # usuaria calcula el promedio horario→diario a mano cada mes.
        return Response({"periodo": periodo, "manual": manual, "vigente": manual})

    @action(detail=False, methods=["put", "delete"], url_path="orden")
    @log_endpoint(name="Operaciones | Facturación | Orden")
    def orden(self, request):
        if request.method == "DELETE":
            ajustes.limpiar_orden()
            return Response({"ok": True})

        entrada = fa_serializers.OrdenSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response({
            "guardadas": ajustes.guardar_orden(entrada.validated_data["nombres"])
        })

    @action(detail=False, methods=["put"], url_path="emitida")
    @log_endpoint(name="Operaciones | Facturación | Emitida")
    def emitida(self, request):
        entrada = fa_serializers.EmitidaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        periodo = calculo.periodo_valido(datos["periodo"])
        numero = (datos.get("numero_factura") or "").strip() or None

        usuario = getattr(request.user, "usuario", None)
        ajustes.marcar_emitida(
            datos["nombre"], periodo, datos["emitida"], numero,
            getattr(usuario, "nombre", None) or getattr(usuario, "email", None),
        )
        return Response({
            "nombre": datos["nombre"], "periodo": periodo,
            "emitida": datos["emitida"], "numero_factura": numero,
        })


