"""ViewSet del panel de Arriendos."""

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.arriendos import models as ar_models
from apps.arriendos.services import cuentas_cobro_originabot
from apps.arriendos.services import documentos as documentos_service
from apps.comun.integraciones.originabot import OriginabotError
from apps.arriendos.services import panel as panel_service
from apps.contratos import models as ct_models

from . import serializers as arr_serializers

SERVICIO = "arriendo"


def _periodo(valor: str) -> str:
    try:
        _, mes = valor.split("-")
        assert 1 <= int(mes) <= 12
    except Exception:
        raise ValidationError("periodo debe tener formato YYYY-MM")
    return valor


@class_logger_wrapper(name="Operaciones | Arriendos")
class ArriendoViewSet(viewsets.GenericViewSet):
    """Panel de arriendos mensual — espejo del de O&M.

    GET    /api/v1/arriendos/calculo/{periodo}
    GET    /api/v1/arriendos/indexacion/{contrato_id}[?arrendador_id=]
    GET    /api/v1/arriendos/diagnostico-migracion
    GET|POST /api/v1/arriendos/proyectos   ·  PUT …/proyectos/{id}
    GET|POST /api/v1/arriendos/contratos/{contrato_id}/arrendadores
    PUT|DELETE /api/v1/arriendos/arrendadores/{id}
    GET|POST /api/v1/arriendos/seleccion/{periodo}
    PATCH  /api/v1/arriendos/seleccion/{periodo}/{arrendador_id}/facturado
    GET    /api/v1/arriendos/ipc  ·  PUT /api/v1/arriendos/ipc/{año}
    GET    /api/v1/arriendos/documentos/{periodo}
    POST   /api/v1/arriendos/documentos/upload
    POST   /api/v1/arriendos/documentos/upload-cuenta-cobro
    GET    /api/v1/arriendos/documentos/file/{doc_id}[?secundario=true]
    GET|POST /api/v1/arriendos/originabot/cuentas-cobro/{periodo}  (TSF)
    DELETE /api/v1/arriendos/documentos/{doc_id}

    **Un contrato puede tener varios arrendadores** y cada uno factura su parte
    con su propio IVA: por eso el panel lista una fila por arrendador y no por
    proyecto. El canon se congela al marcar como facturado.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    queryset = ar_models.ArrSeleccionMensual.objects.none()

    # ── Cálculo ───────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path=r"calculo/(?P<periodo>[\w-]+)")
    def calculo(self, request, periodo=None):
        return Response(panel_service.calculo(_periodo(periodo)))

    @action(
        detail=False, methods=["get"],
        url_path=r"indexacion/(?P<contrato_id>[0-9]+)",
    )
    def indexacion(self, request, contrato_id=None):
        contrato = ct_models.ContratoServicio.objects.filter(
            pk=contrato_id, servicio_aplica=SERVICIO
        ).first()
        if contrato is None:
            raise NotFound("Contrato de arriendo no encontrado")

        # `?arrendadores=todos`: además la serie de cada arrendador, en la misma
        # respuesta, en vez de una petición por arrendador.
        if request.query_params.get("arrendadores") == "todos":
            return Response(panel_service.indexacion_con_arrendadores(contrato))

        arrendador = None
        arrendador_id = request.query_params.get("arrendador_id")
        if arrendador_id:
            arrendador = ar_models.ArrArrendador.objects.filter(
                pk=arrendador_id, contrato=contrato
            ).first()
            if arrendador is None:
                raise NotFound("Arrendador no encontrado para este contrato")
        return Response(panel_service.indexacion(contrato, arrendador))

    @action(detail=False, methods=["get"], url_path="diagnostico-migracion")
    def diagnostico_migracion(self, request):
        """Solo lectura: dimensiona la migración `ArrProyecto` → contrato."""
        from apps.arriendos.services.migracion import diagnostico

        return Response(diagnostico())

    # ── Proyectos y arrendadores ──────────────────────────────────────────

    @action(detail=False, methods=["get", "post"], url_path="proyectos")
    def proyectos(self, request):
        if request.method == "GET":
            filas = ar_models.ArrProyecto.objects.order_by("id")
            return Response(
                arr_serializers.ArrProyectoSerializer(filas, many=True).data
            )
        entrada = arr_serializers.ArrProyectoEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(
            arr_serializers.ArrProyectoSerializer(entrada.save()).data
        )

    @action(
        detail=False, methods=["put"],
        url_path=r"proyectos/(?P<proyecto_id>[0-9]+)",
    )
    def editar_proyecto(self, request, proyecto_id=None):
        fila = ar_models.ArrProyecto.objects.filter(pk=proyecto_id).first()
        if fila is None:
            raise NotFound("proyecto no encontrado")
        entrada = arr_serializers.ArrProyectoEscrituraSerializer(
            fila, data=request.data
        )
        entrada.is_valid(raise_exception=True)
        return Response(
            arr_serializers.ArrProyectoSerializer(entrada.save()).data
        )

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"contratos/(?P<contrato_id>[0-9]+)/arrendadores",
    )
    def arrendadores(self, request, contrato_id=None):
        if request.method == "GET":
            filas = ar_models.ArrArrendador.objects.filter(
                contrato_id=contrato_id
            ).order_by("id")
            return Response(
                arr_serializers.ArrendadorSerializer(filas, many=True).data
            )

        contrato = ct_models.ContratoServicio.objects.filter(
            pk=contrato_id, servicio_aplica=SERVICIO
        ).first()
        if contrato is None:
            raise NotFound("Contrato de arriendo no encontrado")
        entrada = arr_serializers.ArrendadorEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(
            arr_serializers.ArrendadorSerializer(
                entrada.save(contrato=contrato)
            ).data
        )

    @action(
        detail=False, methods=["put", "delete"],
        url_path=r"arrendadores/(?P<arrendador_id>[0-9]+)",
    )
    @log_endpoint(name="Operaciones | Arriendos | Arrendador")
    def arrendador(self, request, arrendador_id=None):
        fila = ar_models.ArrArrendador.objects.filter(pk=arrendador_id).first()
        if fila is None:
            raise NotFound("Arrendador no encontrado")

        if request.method == "DELETE":
            # Un contrato sin arrendadores dejaría el canon sin a quién pagarlo.
            if ar_models.ArrArrendador.objects.filter(
                contrato_id=fila.contrato_id
            ).count() <= 1:
                raise ValidationError(
                    "El contrato debe tener al menos un arrendador"
                )
            fila.delete()
            return Response({"ok": True})

        entrada = arr_serializers.ArrendadorEscrituraSerializer(
            fila, data=request.data
        )
        entrada.is_valid(raise_exception=True)
        return Response(
            arr_serializers.ArrendadorSerializer(entrada.save()).data
        )

    # ── Selección ─────────────────────────────────────────────────────────

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"seleccion/(?P<periodo>[\w-]+)",
    )
    @log_endpoint(name="Operaciones | Arriendos | Selección")
    def seleccion(self, request, periodo=None):
        if request.method == "GET":
            filas = ar_models.ArrSeleccionMensual.objects.filter(periodo=periodo)
            return Response(
                arr_serializers.SeleccionSerializer(filas, many=True).data
            )

        periodo = _periodo(periodo)
        entrada = arr_serializers.GuardarSeleccionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)

        guardadas = []
        for item in entrada.validated_data["items"]:
            arrendador_id = (
                item.get("arr_arrendador_id")
                if item.get("arr_arrendador_id") is not None
                else item.get("proyecto_id")
            )
            fila, _ = ar_models.ArrSeleccionMensual.objects.update_or_create(
                arr_arrendador_id=arrendador_id, periodo=periodo,
                defaults={
                    "incluido": item["incluido"],
                    "motivo_exclusion": item.get("motivo_exclusion"),
                },
                create_defaults={
                    "incluido": item["incluido"],
                    "motivo_exclusion": item.get("motivo_exclusion"),
                    "arr_proyecto_id": None,
                    "facturado": False,
                },
            )
            guardadas.append(fila)
        return Response(
            arr_serializers.SeleccionSerializer(guardadas, many=True).data
        )

    @action(
        detail=False, methods=["patch"],
        url_path=r"seleccion/(?P<periodo>[\w-]+)/(?P<arrendador_id>[0-9]+)/facturado",
    )
    @log_endpoint(name="Operaciones | Arriendos | Facturado")
    def facturado(self, request, periodo=None, arrendador_id=None):
        fila = ar_models.ArrSeleccionMensual.objects.filter(
            arr_arrendador_id=arrendador_id, periodo=periodo
        ).first()

        if fila is None:
            fila = ar_models.ArrSeleccionMensual(
                arr_arrendador_id=arrendador_id, arr_proyecto_id=None,
                periodo=periodo, incluido=True, facturado=True,
            )
        else:
            fila.facturado = not fila.facturado
            # Al desmarcar se descongela: un canon congelado por error se
            # quedaría pegado para siempre.
            if not fila.facturado:
                fila.valor_facturado_congelado = None

        if fila.facturado and fila.valor_facturado_congelado is None:
            arrendador = ar_models.ArrArrendador.objects.filter(
                pk=arrendador_id
            ).first()
            if arrendador is not None:
                panel_service.congelar_canon(fila, arrendador)

        fila.save()
        return Response(arr_serializers.SeleccionSerializer(fila).data)

    # ── IPC ───────────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="ipc")
    def ipc(self, request):
        filas = ar_models.ArrIpcTasa.objects.order_by("año")
        return Response(arr_serializers.IpcSerializer(filas, many=True).data)

    @action(detail=False, methods=["put"], url_path=r"ipc/(?P<anio>[0-9]+)")
    @log_endpoint(name="Operaciones | Arriendos | IPC")
    def ipc_upsert(self, request, anio=None):
        entrada = arr_serializers.IpcUpsertSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        fila, _ = ar_models.ArrIpcTasa.objects.update_or_create(
            **{"año": int(anio)}, defaults=entrada.validated_data
        )
        return Response(arr_serializers.IpcSerializer(fila).data)

    # ── Documentos ────────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="documentos/upload")
    @log_endpoint(name="Operaciones | Arriendos | Subir documento")
    def documentos_upload(self, request):
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        datos = {
            **{c: request.data.get(c) for c in (
                "periodo", "codigo_contrato", "tipo_documento",
                "nombre_resultante",
            )},
            "arr_proyecto_id": int(request.data["arr_proyecto_id"]),
            "pago_id": int(request.data["pago_id"]),
            "proyecto_id": (
                int(request.data["proyecto_id"])
                if request.data.get("proyecto_id") else None
            ),
        }
        _periodo(datos["periodo"])
        documento = documentos_service.subir(
            datos, archivo, request.FILES.get("file_secundario")
        )
        return Response({
            "ok": True, "id": documento.id,
            "nombre_archivo": documento.nombre_archivo,
        })

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"originabot/cuentas-cobro/(?P<periodo>[\w-]+)",
    )
    @log_endpoint(name="Operaciones | Arriendos | Cuentas de cobro de TSF")
    def cuentas_cobro_originabot(self, request, periodo=None):
        """Las cuentas de cobro de TSF del período («Traer de TSF» en Costos).

        GET: lo que hay en TSF para cada planta con contrato de arriendo, con su
        valor y si tiene documento; no escribe. POST `{pagos: [uuid…]}`: descarga
        el documento de esos pagos y lo guarda. Ver
        `apps/arriendos/services/cuentas_cobro_originabot.py`. Con el usuario
        de Operaciones en originabot, sea quien sea el que pide.
        """
        _periodo(periodo)
        try:
            if request.method == "GET":
                return Response(cuentas_cobro_originabot.consultar(periodo))
            pagos = request.data.get("pagos")
            if not isinstance(pagos, list) or not all(isinstance(p, str) for p in pagos) or not pagos:
                raise ValidationError({"pagos": "Lista de ids de pago de TSF."})
            return Response(cuentas_cobro_originabot.importar(periodo, pagos))
        except OriginabotError as exc:
            raise NoProcesable(str(exc))

    @action(
        detail=False, methods=["post"],
        url_path="documentos/upload-cuenta-cobro",
    )
    @log_endpoint(name="Operaciones | Arriendos | Cuenta de cobro")
    def documentos_cuenta_cobro(self, request):
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        datos = {
            c: request.data.get(c) for c in (
                "periodo", "codigo_contrato", "tipo_documento", "predios",
                "numero_cuenta_cobro", "nombre_arrendatario",
            )
        }
        datos["pago_id"] = int(request.data["pago_id"])
        _periodo(datos["periodo"])
        try:
            return Response(documentos_service.subir_cuenta_cobro(
                datos, archivo, request.FILES.get("file_secundario")
            ))
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                str(exc) or "predios debe ser un JSON array no vacío"
            )

    @action(
        detail=False, methods=["get"],
        url_path=r"documentos/file/(?P<doc_id>[0-9]+)",
    )
    def documentos_file(self, request, doc_id=None):
        documento = get_object_or_404(ar_models.ArrDocumento, pk=doc_id)
        secundario = request.query_params.get("secundario", "").lower() in (
            "true", "1"
        )
        try:
            ruta = documentos_service.ruta_de_descarga(documento, secundario)
        except PermissionError as exc:
            raise PermissionDenied(str(exc))
        except FileNotFoundError as exc:
            raise NotFound(str(exc))

        return FileResponse(
            open(ruta, "rb"), as_attachment=True,
            filename=(
                documento.nombre_secundario if secundario
                else documento.nombre_archivo
            ),
            content_type="application/pdf",
        )

    # ¡El patrón NO puede ser `[\w-]+`! DRF ordena las @action ALFABÉTICAMENTE
    # (`get_extra_actions` hace `sorted(...)`), así que `documentos` se registra
    # antes que `documentos_upload` y un comodín se tragaría
    # `/documentos/upload`. Cada una lleva el patrón de lo que de verdad acepta:
    # el listado un período `YYYY-MM`, el borrado un id numérico.
    @action(
        detail=False, methods=["get"],
        url_path=r"documentos/(?P<periodo>\d{4}-\d{2})",
    )
    def documentos(self, request, periodo=None):
        filas = ar_models.ArrDocumento.objects.filter(
            periodo=periodo
        ).order_by("arr_proyecto_id", "pago_id")
        return Response(arr_serializers.DocumentoSerializer(
            [self._documento_a_dict(d) for d in filas], many=True
        ).data)

    @action(
        detail=False, methods=["delete"],
        url_path=r"documentos/(?P<doc_id>\d+)",
    )
    @log_endpoint(name="Operaciones | Arriendos | Eliminar documento")
    def documentos_eliminar(self, request, doc_id=None):
        documento = ar_models.ArrDocumento.objects.filter(pk=doc_id).first()
        if documento is None:
            raise NotFound("Documento no encontrado")
        # Solo se borra el registro; el archivo en disco permanece.
        documento.delete()
        return Response({"ok": True})

    @staticmethod
    def _documento_a_dict(documento) -> dict:
        return {
            "id": documento.id,
            "arr_proyecto_id": documento.arr_proyecto_id,
            "proyecto_id": documento.proyecto_id,
            "periodo": documento.periodo,
            "pago_id": documento.pago_id,
            "codigo_contrato": documento.codigo_contrato,
            "tipo_documento": documento.tipo_documento,
            "nombre_archivo": documento.nombre_archivo,
            "nombre_secundario": documento.nombre_secundario,
            "codigo_predio": documento.codigo_predio,
            "numero_cuenta_cobro": documento.numero_cuenta_cobro,
            "nombre_arrendatario": documento.nombre_arrendatario,
            "valor_individual": (
                float(documento.valor_individual)
                if documento.valor_individual is not None else None
            ),
            "fecha_subida": documento.fecha_subida,
        }
