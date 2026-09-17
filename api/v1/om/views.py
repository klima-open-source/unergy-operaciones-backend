"""ViewSet del panel O&M mensual."""

from pathlib import Path

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.comun.periodo import ANIO_MAX, ANIO_MIN, anio_valido, periodo_valido
from apps.contratos import models as ct_models
from apps.om import models as om_models
from apps.om.services import factura as factura_service
from apps.om.services import panel as panel_service
from apps.proyectos import models as py_models

from . import serializers as om_serializers

SERVICIO_OM = "mantenimiento"


def _periodo(valor: str) -> str:
    if not periodo_valido(valor):
        raise ValidationError("periodo debe tener formato YYYY-MM (mes 01-12)")
    return valor


@class_logger_wrapper(name="Operaciones | O&M | Panel mensual")
class OmViewSet(viewsets.GenericViewSet):
    """Panel O&M mensual: cálculo, selección, IPC y factura del proveedor.

    GET   /api/v1/om/proyectos                    contratos de mantenimiento
    GET   /api/v1/om/calculo/{periodo}            filas y total del mes
    GET   /api/v1/om/indexacion/{contrato_id}     serie anual y mensual
    GET   /api/v1/om/seleccion/{periodo}
    POST  /api/v1/om/seleccion/{periodo}          upsert de la selección
    PATCH /api/v1/om/seleccion/{periodo}/{contrato_id}/facturado
    GET   /api/v1/om/ipc                          ·  PUT /api/v1/om/ipc/{año}
    GET   /api/v1/om/ipc/pendiente
    GET   /api/v1/om/factura/{periodo}            ·  POST …/upload
    PATCH /api/v1/om/factura/{periodo}/sin-match/{id}/asignar
    PUT   /api/v1/om/factura/{periodo}/enlace
    GET   /api/v1/om/documento/{periodo}/{contrato_id}   descarga el PDF
    GET   /api/v1/om/factura/{periodo}/file             descarga el consolidado

    **Nada del cálculo se persiste**: se rehace en cada petición desde el IPC y
    las fechas del contrato. Lo único guardado es la selección del mes, y el
    valor se CONGELA al marcar como facturado — si no, un arreglo posterior en
    la indexación cambiaría un mes ya facturado.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "put", "patch", "head", "options"]
    queryset = om_models.OmSeleccionMensual.objects.none()

    # ── Contratos y cálculo ───────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="proyectos")
    def proyectos(self, request):
        contratos = (
            ct_models.ContratoServicio.objects
            .filter(servicio_aplica=SERVICIO_OM, proyecto__estado="en_operacion")
            .select_related("proyecto").order_by("id")
        )
        filas = [
            {
                "contrato_id": c.id,
                "proyecto_id": c.proyecto_id,
                "nombre_proyecto": factura_service.nombre_proyecto_de(c),
                "fecha_inicio": c.fecha_inicio,
                "valor_base_anual": (
                    float(c.tarifa_base) if c.tarifa_base else None
                ),
                "estado": c.estado or "firmado",
            }
            for c in contratos
        ]
        return Response(
            om_serializers.ContratoOmSerializer(filas, many=True).data
        )

    @action(detail=False, methods=["get"], url_path=r"calculo/(?P<periodo>[\w-]+)")
    def calculo(self, request, periodo=None):
        return Response(panel_service.calculo(_periodo(periodo)))

    @action(
        detail=False, methods=["get"],
        url_path=r"indexacion/(?P<contrato_id>[0-9]+)",
    )
    def indexacion(self, request, contrato_id=None):
        contrato = ct_models.ContratoServicio.objects.filter(
            pk=contrato_id, servicio_aplica=SERVICIO_OM
        ).first()
        if contrato is None:
            raise NotFound("Contrato de mantenimiento no encontrado")
        return Response(panel_service.indexacion(contrato))

    # ── Selección mensual ─────────────────────────────────────────────────

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"seleccion/(?P<periodo>[\w-]+)",
    )
    @log_endpoint(name="Operaciones | O&M | Selección")
    def seleccion(self, request, periodo=None):
        periodo = _periodo(periodo)
        if request.method == "GET":
            filas = om_models.OmSeleccionMensual.objects.filter(periodo=periodo)
            return Response(
                om_serializers.SeleccionSerializer(filas, many=True).data
            )

        entrada = om_serializers.GuardarSeleccionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        guardadas = []
        for item in entrada.validated_data["items"]:
            fila, _ = om_models.OmSeleccionMensual.objects.update_or_create(
                contrato_id=item["contrato_id"], periodo=periodo,
                defaults={
                    "incluido": item["incluido"],
                    "valor_manual": item.get("valor_manual"),
                    "motivo_exclusion": item.get("motivo_exclusion"),
                },
                create_defaults={
                    "incluido": item["incluido"],
                    "facturado": False,
                    "valor_manual": item.get("valor_manual"),
                    "motivo_exclusion": item.get("motivo_exclusion"),
                },
            )
            guardadas.append(fila)
        return Response(
            om_serializers.SeleccionSerializer(guardadas, many=True).data
        )

    @action(
        detail=False, methods=["patch"],
        url_path=r"seleccion/(?P<periodo>[\w-]+)/(?P<contrato_id>[0-9]+)/facturado",
    )
    @log_endpoint(name="Operaciones | O&M | Facturado")
    def facturado(self, request, periodo=None, contrato_id=None):
        periodo = _periodo(periodo)
        fila = om_models.OmSeleccionMensual.objects.filter(
            contrato_id=contrato_id, periodo=periodo
        ).first()

        if fila is None:
            fila = om_models.OmSeleccionMensual(
                contrato_id=contrato_id, periodo=periodo,
                incluido=True, facturado=True,
            )
        else:
            fila.facturado = not fila.facturado
            # Al DESMARCAR se descongela: si no, un valor congelado por error
            # (p. ej. capturado antes de un arreglo de indexación) quedaría
            # pegado para siempre.
            if not fila.facturado:
                fila.valor_facturado_congelado = None

        if fila.facturado and fila.valor_facturado_congelado is None:
            contrato = ct_models.ContratoServicio.objects.filter(
                pk=contrato_id
            ).select_related("proyecto").first()
            if contrato is not None:
                panel_service.congelar_valor(fila, contrato)

        fila.save()
        return Response(om_serializers.SeleccionSerializer(fila).data)

    # ── IPC ───────────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="ipc")
    def ipc(self, request):
        filas = om_models.OmIpcTasa.objects.order_by("año")
        return Response(om_serializers.IpcTasaSerializer(filas, many=True).data)

    @action(detail=False, methods=["get"], url_path="ipc/pendiente")
    def ipc_pendiente(self, request):
        """Tasa sugerida del año anterior.

        Devuelve `None`: la integración con el Banco de la República queda
        pendiente y el valor se carga a mano.
        """
        from datetime import datetime

        return Response({
            "año": datetime.now().year - 1,
            "tasa_sugerida": None,
            "fuente": "manual",
        })

    @action(detail=False, methods=["put"], url_path=r"ipc/(?P<anio>[0-9]+)")
    @log_endpoint(name="Operaciones | O&M | IPC")
    def ipc_upsert(self, request, anio=None):
        anio = int(anio)
        if not anio_valido(anio):
            raise ValidationError(
                f"año fuera de rango permitido ({ANIO_MIN}-{ANIO_MAX})"
            )
        entrada = om_serializers.IpcUpsertSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        fila, _ = om_models.OmIpcTasa.objects.update_or_create(
            **{"año": anio}, defaults=entrada.validated_data
        )
        return Response(om_serializers.IpcTasaSerializer(fila).data)

    # ── Factura consolidada ───────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path=r"factura/(?P<periodo>[\w-]+)")
    def factura(self, request, periodo=None):
        return Response(factura_service.info(_periodo(periodo)))

    @action(
        detail=False, methods=["post"],
        url_path=r"factura/(?P<periodo>[\w-]+)/upload",
    )
    @log_endpoint(name="Operaciones | O&M | Subir factura")
    def factura_upload(self, request, periodo=None):
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        return Response(factura_service.subir(_periodo(periodo), archivo))

    @action(
        detail=False, methods=["patch"],
        url_path=(
            r"factura/(?P<periodo>[\w-]+)/sin-match/"
            r"(?P<sin_match_id>[0-9]+)/asignar"
        ),
    )
    @log_endpoint(name="Operaciones | O&M | Asignar página")
    def asignar_sin_match(self, request, periodo=None, sin_match_id=None):
        periodo = _periodo(periodo)
        entrada = om_serializers.SinMatchAsignarSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)

        sin_match = om_models.OmPaginaSinMatch.objects.filter(
            pk=sin_match_id, periodo=periodo
        ).first()
        if sin_match is None:
            raise NotFound("No existe esa página sin match para este período")

        contrato = ct_models.ContratoServicio.objects.filter(
            pk=entrada.validated_data["contrato_id"], servicio_aplica=SERVICIO_OM
        ).select_related("proyecto").first()
        if contrato is None:
            raise NotFound(
                "El contrato indicado no es un contrato de mantenimiento válido"
            )

        try:
            documento = factura_service.asignar_sin_match(sin_match, contrato)
        except factura_service.YaAsignada as exc:
            raise ValidationError(str(exc))
        except factura_service.SinPdfOriginal as exc:
            raise NotFound(str(exc))

        return Response({
            "ok": True,
            "contrato_id": contrato.id,
            "nombre_proyecto": factura_service.nombre_proyecto_de(contrato),
            "documento_nombre": documento.nombre_archivo,
        })

    @action(
        detail=False, methods=["put"],
        url_path=r"factura/(?P<periodo>[\w-]+)/enlace",
    )
    @log_endpoint(name="Operaciones | O&M | Enlace de factura")
    def factura_enlace(self, request, periodo=None):
        entrada = om_serializers.EnlaceSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        factura_service.guardar_enlace(
            _periodo(periodo),
            entrada.validated_data.get("enlace_pdf"),
            entrada.validated_data.get("nombre_archivo"),
        )
        return Response({"ok": True})

    # ── Descargas ─────────────────────────────────────────────────────────

    @action(
        detail=False, methods=["get"],
        url_path=r"documento/(?P<periodo>[\w-]+)/(?P<contrato_id>[0-9]+)",
    )
    def documento(self, request, periodo=None, contrato_id=None):
        documento = om_models.OmDocumentoProyecto.objects.filter(
            periodo=_periodo(periodo), contrato_id=contrato_id
        ).first()
        if documento is None:
            raise NotFound("No hay documento para este proyecto y período")

        ruta = Path(documento.ruta_local).resolve()
        # La ruta viene de la base, pero se comprueba que siga DENTRO del
        # directorio de subidas: un valor manipulado no debe servir cualquier
        # archivo del servidor.
        if not str(ruta).startswith(str(factura_service.directorio().resolve())):
            raise PermissionDenied("Acceso denegado")
        if not ruta.exists():
            raise NotFound("Archivo no encontrado en el servidor")
        return FileResponse(
            open(ruta, "rb"), as_attachment=True,
            filename=documento.nombre_archivo, content_type="application/pdf",
        )

    @action(
        detail=False, methods=["get"],
        url_path=r"factura/(?P<periodo>[\w-]+)/file",
    )
    def factura_file(self, request, periodo=None):
        periodo = _periodo(periodo)
        registro = om_models.OmFacturaMensual.objects.filter(
            periodo=periodo
        ).first()
        if registro is None or not registro.ruta_local:
            raise NotFound("No hay archivo subido para este período")
        ruta = Path(registro.ruta_local)
        if not ruta.exists():
            raise NotFound("Archivo no encontrado en el servidor")
        return FileResponse(
            open(ruta, "rb"), as_attachment=True,
            filename=registro.nombre_archivo or f"factura-{periodo}.pdf",
            content_type="application/octet-stream",
        )
