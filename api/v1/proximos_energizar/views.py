"""ViewSet de "Próximos a energizarse".

Tres rutas sobre el pipeline TSF ya sincronizado en `proyectos`: el listado, el
botón de sincronizar y un diagnóstico crudo contra Sun Factory.
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from api.exceptions import ServicioNoDisponible
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.proyectos.models import Proyecto
from apps.proyectos.services import proximos_energizar as pe_service
from apps.proyectos.services.tsf_sync import (
    _pick_energization_milestone, _sunfactory_all_projects,
    _sunfactory_milestones_raw, _sunfactory_token, sync_tsf_projects,
)


@class_logger_wrapper(name="Operaciones | Próximos a energizar")
class ProximosEnergizarViewSet(viewsets.GenericViewSet):
    """Proyectos próximos a energizarse.

    GET  /api/v1/proximos-energizar
    POST /api/v1/proximos-energizar/sync
    GET  /api/v1/proximos-energizar/{proyecto_id}/debug-sunfactory

    **Todos los campos son de solo lectura.** Vienen de Sun Factory/TSF vía el
    job de sincronización; no hay edición manual en esta vista.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = Proyecto.objects.none()
    lookup_value_regex = r"\d+"

    def list(self, request, *args, **kwargs):
        return Response(pe_service.listar())

    @action(detail=False, methods=["post"], url_path="sync")
    def sync(self, request):
        """Dispara la sincronización TSF → proyectos on-demand (el botón de la vista).

        Corre con `enrich_dates=False` a propósito: consultar los hitos son ~99
        llamadas HTTP que harían timeout el request. El job programado de 6 h sí
        los pide y trae la fecha de energización precisa (RETIE).
        """
        try:
            return Response(sync_tsf_projects(enrich_dates=False))
        except Exception as exc:
            raise ServicioNoDisponible(f"No se pudo sincronizar con Solenium/TSF: {exc}")

    @action(
        detail=False, methods=["get"],
        url_path=r"(?P<proyecto_id>\d+)/debug-sunfactory",
    )
    def debug_sunfactory(self, request, proyecto_id=None):
        """Qué trae Sun Factory CRUDO para este proyecto: los hitos sin filtrar y
        su registro del listado con el `next_milestone`.

        Responde "¿por qué no tiene fecha o avance?" sin adivinar: muestra si Sun
        Factory de verdad no tiene el dato, o si el botón on-demand —que no
        consulta hitos— simplemente no lo ha traído todavía.
        """
        p = Proyecto.objects.filter(pk=proyecto_id, deleted_at__isnull=True).first()
        if p is None:
            raise NotFound("Proyecto no encontrado")
        if not p.sunfactory_project_id:
            return Response({
                "vinculado": False,
                "detalle": "Este proyecto no tiene sunfactory_project_id.",
            })

        token = _sunfactory_token()
        if not token:
            raise ServicioNoDisponible("Credenciales de Sun Factory no configuradas.")
        try:
            milestones = _sunfactory_milestones_raw(token, p.sunfactory_project_id)
        except Exception as exc:
            raise ServicioNoDisponible(f"No se pudo consultar milestones: {exc}")

        listado = next(
            (row for row in _sunfactory_all_projects(token)
             if row.get("id") == p.sunfactory_project_id),
            None,
        )
        return Response({
            "vinculado": True,
            "sunfactory_project_id": p.sunfactory_project_id,
            "fecha_actual_bd": p.fecha_estimada_energizacion.isoformat()
            if p.fecha_estimada_energizacion else None,
            "milestones_total": len(milestones),
            "milestones_con_fecha": [
                {"name": m.get("name"), "date": m.get("date"),
                 "planned_date": m.get("planned_date")}
                for m in milestones if m.get("date") or m.get("planned_date")
            ],
            "milestone_energizacion_elegido": _pick_energization_milestone(milestones),
            "sunfactory_state": listado.get("state") if listado else None,
            "next_milestone_del_listado": listado.get("next_milestone") if listado else None,
        })
