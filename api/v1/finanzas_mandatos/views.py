"""ViewSet de los mandatos de Finanzas (Costos)."""

from datetime import datetime

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import NoProcesable, ServicioNoDisponible
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.mandatos import models as md_models
from apps.mandatos.services import finanzas, finanzas_upsert, reconciliacion

TIPOS = ("ingreso", "costo")


def _periodo(valor: str | None):
    """`"2025-05"` → `date(2025, 5, 1)`."""
    try:
        return datetime.strptime((valor or "").strip()[:7], "%Y-%m").date()
    except ValueError:
        raise NoProcesable("periodo debe ser YYYY-MM")


def _a_dict(mandato) -> dict:
    return {
        "id": mandato.id,
        "proyecto": mandato.proyecto,
        "tercero": mandato.tercero,
        "periodo": (
            mandato.periodo.strftime("%Y-%m") if mandato.periodo else None
        ),
        "tipo": mandato.tipo,
        "cmu": mandato.cmu,
        "cmu_anterior": mandato.cmu_anterior,
        "estado": mandato.estado,
        "comentario": mandato.comentario,
        "fecha_envio": (
            mandato.fecha_envio.isoformat() if mandato.fecha_envio else None
        ),
        "fecha_firma": (
            mandato.fecha_firma.isoformat() if mandato.fecha_firma else None
        ),
        "drive_url": mandato.drive_url,
    }


@class_logger_wrapper(name="Operaciones | Mandatos | Finanzas")
class FinanzasMandatoViewSet(viewsets.GenericViewSet):
    """Mandatos de Finanzas: ingesta desde el script y lecturas del período.

    POST /api/v1/finanzas/mandatos/ingest        multipart, del script local
    GET  /api/v1/finanzas/mandatos?periodo=YYYY-MM[&tipo=]
    GET  /api/v1/finanzas/mandatos/resumen?periodo=
    GET  /api/v1/finanzas/mandatos/reconciliacion?periodo=[&tipo=]

    La **reconciliación** responde la pregunta que hoy solo contesta el script
    local: «envié 32 mandatos de julio, ¿volvieron los 32?». Poder contestarla
    acá es la condición para dejar de correr ese script.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "head", "options"]
    queryset = md_models.FinanzasMandato.objects.none()

    def _del_periodo(self, request):
        consulta = md_models.FinanzasMandato.objects.filter(
            periodo=_periodo(request.query_params.get("periodo"))
        )
        tipo = request.query_params.get("tipo")
        return consulta.filter(tipo=tipo) if tipo else consulta

    def list(self, request, *args, **kwargs):
        filas = self._del_periodo(request).order_by("proyecto", "tercero")
        return Response({
            "periodo": request.query_params.get("periodo"),
            "mandatos": [_a_dict(m) for m in filas],
        })

    @action(detail=False, methods=["get"], url_path="resumen")
    def resumen(self, request):
        """Conteos por tipo: total, firmados, sin firma y con comentarios."""
        filas = list(md_models.FinanzasMandato.objects.filter(
            periodo=_periodo(request.query_params.get("periodo"))
        ))

        def conteo(tipo: str) -> dict:
            del_tipo = [m for m in filas if m.tipo == tipo]
            return {
                "total": len(del_tipo),
                "firmados": sum(1 for m in del_tipo if m.estado == "firmado"),
                "falta_firma": sum(
                    1 for m in del_tipo if m.estado == "sin_firma"
                ),
                "con_comentarios": sum(
                    1 for m in del_tipo if m.estado == "con_comentarios"
                ),
            }

        return Response({
            "periodo": request.query_params.get("periodo"),
            **{tipo: conteo(tipo) for tipo in TIPOS},
        })

    @action(detail=False, methods=["get"], url_path="reconciliacion")
    def reconciliacion(self, request):
        """De los mandatos enviados en el período, cuáles no han vuelto."""
        return Response({
            "periodo": request.query_params.get("periodo"),
            "tipo": request.query_params.get("tipo"),
            **reconciliacion.reconciliar(list(self._del_periodo(request))),
        })

    @action(detail=False, methods=["post"], url_path="ingest")
    @log_endpoint(name="Operaciones | Mandatos | Finanzas | Ingesta")
    def ingest(self, request):
        """Alta o actualización desde el script local, con su PDF opcional.

        El PDF **solo se sube a Drive si el mandato viene firmado**: es el único
        estado en que el documento es definitivo, y subir borradores llenaría la
        carpeta de versiones que nadie va a mirar.
        """
        datos = request.data
        periodo = _periodo(datos.get("periodo"))
        fecha_crudo = datos.get("fecha")
        try:
            fecha = (
                datetime.strptime(fecha_crudo[:10], "%Y-%m-%d").date()
                if fecha_crudo else None
            )
        except ValueError:
            raise NoProcesable({"fecha": "Debe ser YYYY-MM-DD"})

        estado = datos.get("estado")
        archivo = request.FILES.get("file")
        drive_id = drive_url = None
        if archivo is not None and estado == "firmado":
            from apps.comun.drive_evidencia import DriveNoConfigurado
            from apps.mandatos.services.drive import subir_pdf

            cmu = datos.get("cmu")
            try:
                subido = subir_pdf(
                    archivo.read(),
                    archivo.name or f'{cmu or "mandato"}.pdf',
                    f'{periodo.strftime("%Y-%m")}-{datos.get("tipo")}',
                )
            except DriveNoConfigurado as exc:
                raise ServicioNoDisponible(str(exc))
            drive_id, drive_url = subido["id"], subido["url"]

        cmu_crudo = datos.get("cmu")
        mandato, creado = finanzas_upsert.upsert(
            proyecto=(datos.get("proyecto") or "").strip(),
            tercero=(datos.get("tercero") or "").strip(),
            periodo=periodo,
            tipo=datos.get("tipo"),
            cmu=finanzas.extraer_cmu(cmu_crudo) if cmu_crudo else None,
            estado=estado,
            comentario=datos.get("comentario"),
            fecha=fecha,
            correo_ref=datos.get("correo_ref"),
            drive_file_id=drive_id,
            drive_url=drive_url,
        )
        return Response({
            "ok": True, "creado": creado, "mandato": _a_dict(mandato),
        })
