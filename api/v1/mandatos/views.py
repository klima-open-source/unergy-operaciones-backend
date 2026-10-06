"""ViewSets de mandatos y de la tabla maestra de inversionistas."""

import threading
from datetime import datetime, timezone

from django.db import close_old_connections
from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.mandatos import models as md_models
from apps.mandatos.services import panel as panel_service
from apps.mandatos.services import pdfs as pdfs_service
from apps.mandatos.services.reglas import (
    calcular_resumen, extraer_cmu_de_nombre, mandato_to_dict,
)

from . import serializers as md_serializers

LIMITE_BITACORA = 500
DIAS_MAXIMOS_INGESTA = 1825


def _periodo(request) -> str:
    valor = request.query_params.get("periodo") or request.data.get("periodo")
    if not valor:
        raise ValidationError({"periodo": "Requerido, formato YYYY-MM"})
    try:
        panel_service.periodo_a_fecha(valor)
    except ValueError as exc:
        raise ValidationError(str(exc))
    return valor


@class_logger_wrapper(name="Operaciones | Mandatos")
class MandatoViewSet(viewsets.GenericViewSet):
    """Mandatos de costos: carga, firma y su bitácora de correos.

    GET    /api/v1/mandatos?periodo=YYYY-MM   ·  /resumen  ·  /periodos
    POST   /api/v1/mandatos                   → 201
    PATCH  /api/v1/mandatos/{id}  ·  DELETE /api/v1/mandatos/{id}
    POST   /api/v1/mandatos/upload-firmado    PDF suelto, asocia por CMU
    POST   /api/v1/mandatos/{id}/asociar-pdf  asocia un PDF ya subido
    POST   /api/v1/mandatos/upload-zip        crea los mandatos del período
    GET    /api/v1/mandatos/{id}/pdf
    POST   /api/v1/mandatos/ejecutar-ingesta  solo admin; ESCRIBE
    GET    /api/v1/mandatos/diagnostico-imap  solo lee
    GET    /api/v1/mandatos/correos  ·  POST /correos/{id}/revertir

    El estado sigue una máquina de transiciones (`reglas.transicion_valida`): no
    es un campo libre.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = md_models.Mandato.objects.all()

    def get_permissions(self):
        # Ejecutar la ingesta no es una consulta: es una acción que escribe.
        if self.action == "ejecutar_ingesta":
            self.required_role = ["admin"]
        return super().get_permissions()

    def _mandato(self, pk):
        mandato = md_models.Mandato.objects.filter(pk=pk).first()
        if mandato is None:
            raise NotFound("Mandato no encontrado.")
        return mandato

    # ── Listados ──────────────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        fecha = panel_service.periodo_a_fecha(_periodo(request))
        filas = md_models.Mandato.objects.filter(periodo=fecha).order_by("cmu")
        return Response([mandato_to_dict(f) for f in filas])

    @action(detail=False, methods=["get"], url_path="resumen")
    def resumen(self, request):
        fecha = panel_service.periodo_a_fecha(_periodo(request))
        return Response(calcular_resumen(
            md_models.Mandato.objects.filter(periodo=fecha)
        ))

    @action(detail=False, methods=["get"], url_path="periodos")
    def periodos(self, request):
        return Response(panel_service.periodos_con_badge())

    # ── CRUD ──────────────────────────────────────────────────────────────

    def create(self, request, *args, **kwargs):
        entrada = md_serializers.MandatoCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        if md_models.Mandato.objects.filter(
            cmu=datos["cmu"], periodo=datos["periodo"]
        ).exists():
            raise Conflict(
                f'Ya existe el mandato {datos["cmu"]} en ese período.'
            )
        return Response(mandato_to_dict(entrada.save()), status=201)

    def partial_update(self, request, *args, **kwargs):
        mandato = self._mandato(kwargs["pk"])
        entrada = md_serializers.MandatoActualizarSerializer(
            mandato, data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        try:
            panel_service.actualizar(mandato, entrada.validated_data)
        except panel_service.EstadoInvalido as exc:
            raise NoProcesable(str(exc))
        return Response(mandato_to_dict(mandato))

    def destroy(self, request, *args, **kwargs):
        self._mandato(kwargs["pk"]).delete()
        return Response(status=204)

    # ── PDF ───────────────────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="upload-firmado")
    @log_endpoint(name="Operaciones | Mandatos | Subir firmado")
    def upload_firmado(self, request):
        """Sube un PDF firmado y lo asocia por el CMU que trae en el nombre.

        Si no se puede identificar el CMU o no existe el mandato, el archivo SÍ
        queda guardado y se responde `asociado: false`: perder la subida
        obligaría a repetirla, y el usuario puede asociarlo a mano.
        """
        periodo = _periodo(request)
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        if not archivo.name.lower().endswith(".pdf"):
            raise ValidationError("El archivo debe ser un PDF.")
        contenido = archivo.read()
        if len(contenido) > pdfs_service.MAX_PDF:
            return Response(
                {"detail": "Archivo demasiado grande (máx. 20 MB)."}, status=413
            )

        ruta = pdfs_service.guardar_pdf(archivo.name, contenido)
        cmu = extraer_cmu_de_nombre(archivo.name)
        if not cmu:
            return Response({
                "asociado": False, "ruta": str(ruta), "nombre": archivo.name,
                "mensaje": (
                    "No se pudo identificar el CMU del nombre. Asocia el PDF "
                    "manualmente."
                ),
            })

        fecha = panel_service.periodo_a_fecha(periodo)
        mandato = md_models.Mandato.objects.filter(
            cmu=cmu, periodo=fecha
        ).first()
        if mandato is None:
            return Response({
                "asociado": False, "ruta": str(ruta), "nombre": archivo.name,
                "cmu": cmu,
                "mensaje": (
                    f"No existe el mandato {cmu} en {periodo}. Asócialo "
                    "manualmente."
                ),
            })

        panel_service.marcar_firmado(mandato, ruta, archivo.name)
        return Response({"asociado": True, "mandato": mandato_to_dict(mandato)})

    @action(detail=True, methods=["post"], url_path="asociar-pdf")
    @log_endpoint(name="Operaciones | Mandatos | Asociar PDF")
    def asociar_pdf(self, request, pk=None):
        mandato = self._mandato(pk)
        entrada = md_serializers.AsociarPdfSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        try:
            ruta = pdfs_service.ruta_de_nombre(entrada.validated_data["nombre"])
        except pdfs_service.NombreInvalido as exc:
            raise ValidationError(str(exc))
        except pdfs_service.SinPdf as exc:
            raise NotFound(str(exc))

        panel_service.marcar_firmado(mandato, ruta, ruta.name)
        return Response(mandato_to_dict(mandato))

    @action(detail=False, methods=["post"], url_path="upload-zip")
    @log_endpoint(name="Operaciones | Mandatos | Subir ZIP")
    def upload_zip(self, request):
        periodo = _periodo(request)
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        if not archivo.name.lower().endswith(".zip"):
            raise ValidationError("El archivo debe ser un .zip")
        contenido = archivo.read()
        if len(contenido) > pdfs_service.MAX_ZIP:
            return Response(
                {"detail": "Archivo demasiado grande (máx. 100 MB)."},
                status=413,
            )
        try:
            return Response(panel_service.cargar_zip(periodo, contenido))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=422)

    @action(detail=True, methods=["get"], url_path="pdf")
    def pdf(self, request, pk=None):
        mandato = self._mandato(pk)
        try:
            contenido, nombre = pdfs_service.contenido_del_mandato(mandato)
        except pdfs_service.SinPdf as exc:
            raise NotFound(str(exc))

        respuesta = HttpResponse(contenido, content_type="application/pdf")
        respuesta["Content-Disposition"] = f'inline; filename="{nombre}"'
        return respuesta

    # ── Ingesta por correo ────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="ejecutar-ingesta")
    @log_endpoint(name="Operaciones | Mandatos | Ejecutar ingesta")
    def ejecutar_ingesta(self, request):
        """Corre la lectura del buzón AHORA, sin esperar al cron de las :05.

        **Esto escribe**: hace lo mismo que el cron. Es POST y pide admin a
        propósito — un GET se dispara con abrir la URL y un prefetch del
        navegador podría ejecutarlo sin que nadie lo pidiera.

        Volver a correrlo es inofensivo: la deduplicación va por `Message-ID`.

        `reprocesar_desde` **destruye información que no se reconstruye**: borra
        las filas de bitácora desde esa fecha, y con ellas el `estado_previo`
        del que sale el botón de revertir. Pide fecha explícita en vez de un
        `todo=true` para que sea imposible vaciar la tabla por descuido.
        """
        from apps.mandatos.services.correo_sync import (
            ingesta_en_curso, revisar_correos_async,
        )

        if ingesta_en_curso():
            raise Conflict(
                "Ya hay una corrida en curso. Espera a que termine antes de "
                "lanzar otra."
            )

        dias = request.query_params.get("dias", "30")
        if not dias.isdigit() or not 1 <= int(dias) <= DIAS_MAXIMOS_INGESTA:
            raise ValidationError(
                {"dias": f"Entero entre 1 y {DIAS_MAXIMOS_INGESTA}."}
            )

        borradas = 0
        reprocesar = request.query_params.get("reprocesar_desde")
        if reprocesar:
            try:
                desde = datetime.strptime(
                    reprocesar.strip()[:10], "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                raise NoProcesable(
                    "reprocesar_desde debe ser YYYY-MM-DD"
                )
            borradas, _ = md_models.MandatoCorreo.objects.filter(
                fecha__gte=desde
            ).delete()

        # En segundo plano a propósito: leer 90 días son MINUTOS —cada PDF se
        # abre para revisar firmas— y el proxy corta la conexión mucho antes,
        # devolviendo un 502 que parece un fallo del backend cuando el proceso
        # sigue corriendo.
        def corriendo():
            try:
                revisar_correos_async(int(dias))
            finally:
                close_old_connections()

        threading.Thread(target=corriendo, daemon=True).start()
        return Response({
            "ok": True, "estado": "arrancó en segundo plano", "dias": int(dias),
            "bitacora_borrada": borradas if reprocesar else 0,
            "reprocesado_desde": reprocesar,
            "como_ver_avance": (
                "GET /api/v1/mandatos/correos?limite=500 -- el conteo va "
                "subiendo mientras procesa"
            ),
        })

    @action(detail=False, methods=["get"], url_path="diagnostico-imap")
    def diagnostico_imap(self, request):
        """Prueba la conexión IMAP a demanda. Solo lee: no toca base ni buzón."""
        from apps.mandatos.services.diagnostico import diagnostico_imap

        return Response(diagnostico_imap())

    @action(detail=False, methods=["get"], url_path="correos")
    def correos(self, request):
        """Bitácora de correos leídos, del más reciente al más viejo."""
        limite = request.query_params.get("limite", "100")
        if not limite.isdigit():
            raise ValidationError({"limite": "Debe ser entero."})
        consulta = md_models.MandatoCorreo.objects.order_by("-fecha")
        if request.query_params.get("solo_revision", "").lower() in ("true", "1"):
            consulta = consulta.filter(requiere_revision=True)

        return Response([
            {
                "id": f.id, "fecha": f.fecha.isoformat(),
                "remitente": f.remitente, "asunto": f.asunto,
                "fuente": f.fuente, "clasificacion": f.clasificacion,
                "resultado": f.resultado,
                "requiere_revision": f.requiere_revision,
                "revertido": f.revertido, "detalle": f.detalle,
            }
            for f in consulta[:min(int(limite), LIMITE_BITACORA)]
        ])

    @action(
        detail=False, methods=["post"],
        url_path=r"correos/(?P<correo_id>[0-9]+)/revertir",
    )
    @log_endpoint(name="Operaciones | Mandatos | Revertir correo")
    def revertir_correo(self, request, correo_id=None):
        correo = md_models.MandatoCorreo.objects.filter(pk=correo_id).first()
        if correo is None:
            raise NotFound("Correo no encontrado.")
        if correo.revertido:
            raise Conflict("Este correo ya fue revertido.")
        revertidos = panel_service.revertir_correo(correo)
        return Response({"revertidos": revertidos, "total": len(revertidos)})


@class_logger_wrapper(name="Operaciones | Mandatos | Inversionistas")
class MandatoInversionistaViewSet(viewsets.GenericViewSet):
    """GET /api/v1/mandato-inversionistas — la tabla maestra."""

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "head", "options"]
    queryset = md_models.MandatoInversionista.objects.all()

    def list(self, request, *args, **kwargs):
        filas = md_models.MandatoInversionista.objects.order_by("nombre")
        return Response(
            md_serializers.InversionistaSerializer(filas, many=True).data
        )
