"""ViewSet de Monitoreo: resumen de flota y el puente `_legacy`."""

import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.energia.services import unergy_api
from apps.proyectos import models as py_models

from . import queryset as monitoreo_queryset

logger = logging.getLogger("operaciones.monitoreo")

EXTENSION_POR_MIME = {
    "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}
ROLES_SYNC = ("admin", "operaciones")


def _fecha(valor: str | None):
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError("Formato de fecha inválido (YYYY-MM-DD)")


@class_logger_wrapper(name="Operaciones | Monitoreo")
class MonitoreoViewSet(viewsets.GenericViewSet):
    """Generación en vivo y el puente que reemplazó al Google Apps Script.

    GET  /api/v1/monitoreo/resumen-generacion?date_from=&date_to=
    GET  /api/v1/monitoreo/_legacy?action=<...>
    POST /api/v1/monitoreo/_legacy            {"action": "savePhoto", …}
    POST /api/v1/monitoreo/admin/sync-proyectos

    **`_legacy` no es REST y se deja tal cual.** Un endpoint con `?action=` es
    lo que consume hoy el frontend de Fallas; convertirlo en recursos es un
    trabajo aparte, no un efecto colateral de cambiar de framework.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = py_models.Proyecto.objects.none()

    @action(detail=False, methods=["get"], url_path="resumen-generacion")
    def resumen_generacion(self, request):
        """Generación real de todos los proyectos activos, por fecha y proyecto."""
        desde = _fecha(request.query_params.get("date_from"))
        hasta = _fecha(request.query_params.get("date_to"))
        proyectos = list(monitoreo_queryset.proyectos_en_operacion())

        # Sin proyectos o sin fechas válidas se devuelve la forma vacía, no un
        # error: la gráfica se dibuja igual y el frontend no maneja el 4xx.
        if not proyectos or desde is None or hasta is None:
            return Response(
                {"projects_count": len(proyectos), "dates": [], "by_project": []}
            )
        return Response(
            unergy_api.generacion_de_la_flota(proyectos, desde, hasta)
        )

    @action(detail=False, methods=["get", "post"], url_path="_legacy")
    @log_endpoint(name="Operaciones | Monitoreo | Legacy")
    def legacy(self, request):
        if request.method == "POST":
            return self._legacy_post(request)

        accion = request.query_params.get("action")
        sub_project = request.query_params.get("sub_project")
        desde = _fecha(request.query_params.get("date_from"))
        hasta = _fecha(request.query_params.get("date_to"))

        if accion == "getProjects":
            return Response(monitoreo_queryset.build_projects())
        if accion == "getPortfolios":
            return Response(monitoreo_queryset.build_portfolios())
        if accion == "getAllContratos":
            return Response(monitoreo_queryset.build_all_contratos())
        if accion == "getGeneration":
            if not sub_project:
                return Response({"ok": False, "error": "sub_project requerido"})
            hoy = datetime.now(unergy_api.TZ_COL).date()
            return Response(monitoreo_queryset.build_generation(
                sub_project, desde or hoy.replace(day=1), hasta or hoy
            ))
        if accion == "getMonthlyGeneration":
            # Varias plantas, totales por mes: ver `build_generacion_mensual`.
            plantas = [s for s in (request.query_params.get("sub_projects") or "").split(",") if s]
            if not plantas or desde is None or hasta is None:
                raise ValidationError("sub_projects, date_from y date_to son requeridos")
            if len(plantas) > monitoreo_queryset.MAX_PLANTAS_MENSUAL:
                raise ValidationError(
                    f"Como máximo {monitoreo_queryset.MAX_PLANTAS_MENSUAL} plantas por consulta"
                )
            return Response(monitoreo_queryset.build_generacion_mensual(plantas, desde, hasta))
        if accion == "getFMOData":
            if not sub_project:
                return Response({"ok": False, "error": "sub_project requerido"})
            return Response(
                monitoreo_queryset.build_fmo(sub_project, desde, hasta)
            )
        if accion == "sendCode":
            # Resto del portal de clientes que se retiró. Se responde ok para no
            # romper a un cliente viejo que siga llamándolo.
            return Response({"ok": True})
        raise ValidationError(f"Acción no reconocida: {accion}")

    def _legacy_post(self, request):
        accion = request.data.get("action", "")
        if accion == "savePhoto":
            return self._guardar_foto(request.data)
        if accion == "sendCode":
            return Response({"ok": True})
        raise ValidationError(f"Acción POST no reconocida: {accion}")

    @staticmethod
    def _guardar_foto(datos: dict):
        """Guarda una foto de falla en disco y devuelve su URL.

        Los dos nombres se sanean antes de tocar el sistema de archivos: llegan
        del cliente y sin limpiarlos un `../` escaparía del directorio.
        """
        falla_id = re.sub(r"[^\w\-]", "_", str(datos.get("faultId") or "unknown"))
        nombre = re.sub(r"[^\w\-\.]", "_", str(datos.get("photoName") or "foto.jpg"))
        mime = datos.get("mimeType") or "image/jpeg"

        extension = EXTENSION_POR_MIME.get(mime, ".jpg")
        if not nombre.lower().endswith(extension):
            nombre = nombre.rsplit(".", 1)[0] + extension

        try:
            contenido = base64.b64decode(datos.get("b64") or "")
        except Exception:
            return Response({"ok": False, "error": "Base64 inválido"})

        carpeta = Path("uploads/fotos") / falla_id
        carpeta.mkdir(parents=True, exist_ok=True)
        marca = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archivo = f"{marca}_{nombre}"
        (carpeta / archivo).write_bytes(contenido)

        url = f"/static/uploads/fotos/{falla_id}/{archivo}"
        return Response({"ok": True, "folderUrl": url, "photoUrl": url})

    @action(detail=False, methods=["post"], url_path="admin/sync-proyectos")
    @log_endpoint(name="Operaciones | Monitoreo | Sync proyectos")
    def sync_proyectos(self, request):
        """Rellena campos faltantes de proyectos desde un JSON del repo.

        `ponytail: sigue siendo un endpoint, no un management command`. Según
        `CLAUDE.md` un backfill de datos va en un comando o en una tarea, no en
        la API — pero moverlo cambiaría el contrato, y esta migración no cambia
        contratos. Al terminar de portar, esto es lo primero que debería
        convertirse en `manage.py sync_proyectos`.
        """
        if request.user.usuario.rol not in ROLES_SYNC:
            raise PermissionDenied("Sin permisos")

        from apps.proyectos.services import sync_desde_json

        ruta = Path(settings.BASE_DIR) / "data" / "proyectos_solares_completo.json"
        actualizados, saltados = sync_desde_json.sincronizar(ruta)
        return Response({
            "ok": True,
            "json_actualizados": actualizados,
            "json_saltados": saltados,
        })
