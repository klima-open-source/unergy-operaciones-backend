"""ViewSet de generación solar: inversores de SolarView + medidores de Gaia.

Migrado de Solenium a SolarView el 2026-09-03, en el mismo movimiento que borró
los 14 endpoints que no consumía nadie. Quedan seis. Toda la lógica vive en
`apps.energia.services.solarview_monitoreo`; acá solo se validan los parámetros
de la query.
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.energia.services import solarview_monitoreo as sv
from apps.proyectos import models as py_models

GRANULARIDADES = ("day", "hour")

_SI = ("1", "true", "yes", "si", "sí")
_NO = ("0", "false", "no")


def _bandera(request, nombre: str, *, por_defecto: bool) -> bool:
    """Un flag de la query string, con el default explicito de cada uno.

    Se lee en los dos sentidos (`=1` y `=0`) porque los flags de este ViewSet no
    apuntan todos para el mismo lado: `incluir_snapshot` viene apagado y se
    prende, `incluir_30d` viene prendido y se apaga. Un valor que no sea ninguno
    de los dos deja el default -- son optimizaciones, no argumentos de negocio;
    un typo no vale un 400 en una pantalla de monitoreo.
    """
    valor = (request.query_params.get(nombre) or "").strip().lower()
    if valor in _SI:
        return True
    if valor in _NO:
        return False
    return por_defecto


@class_logger_wrapper(name="Operaciones | Energía | Generación Solar")
class GeneracionSolarViewSet(viewsets.GenericViewSet):
    """Generación en tiempo real de las plantas, y el estado de la flota.

    La resolución al proveedor va SIEMPRE por `project_id_solarview`, nunca por
    nombre: emparejar por nombre era una adivinanza que se recalculaba en cada
    request y se persistía como efecto secundario de un GET. Un proyecto sin ese
    id no tiene datos de inversores — y el hueco queda visible para que lo
    resuelva el backfill— pero SÍ tiene medidores, que se resuelven por
    `fronteras.proyecto_id` sin pasar por ningún proveedor.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    queryset = py_models.Proyecto.objects.none()

    @staticmethod
    def _fecha(request, nombre: str) -> str:
        valor = (request.query_params.get(nombre) or "").strip()
        if not valor:
            raise ValidationError({nombre: "Requerido (YYYY-MM-DD)."})
        return valor

    @action(
        detail=False, methods=["get"],
        url_path=r"proyecto/(?P<proyecto_id>[0-9]+)/historial",
    )
    def historial(self, request, proyecto_id=None):
        granularidad = request.query_params.get("granularidad") or "day"
        if granularidad not in GRANULARIDADES:
            raise ValidationError(
                {"granularidad": f"Uno de {', '.join(GRANULARIDADES)}."})
        return Response(sv.historial(
            int(proyecto_id),
            self._fecha(request, "fecha_inicio"),
            self._fecha(request, "fecha_fin"),
            granularidad,
        ))

    @action(detail=False, methods=["get"], url_path="generacion-hoy")
    def generacion_hoy(self, request):
        return Response(sv.generacion_hoy())

    @action(detail=False, methods=["get"], url_path="resumen-dia")
    def resumen_dia(self, request):
        return Response(sv.resumen_dia())

    @action(detail=False, methods=["get"], url_path="monitoring")
    def monitoring(self, request):
        return Response(sv.monitoreo_flota())

    @action(
        detail=False, methods=["get"],
        url_path=r"monitoring/(?P<proyecto_id>[0-9]+)",
    )
    def monitoring_detalle(self, request, proyecto_id=None):
        # `incluir_snapshot` agrega el snapshot electrico del medidor (voltaje,
        # corriente y potencia por fase), que necesita el diagrama fasorial.
        # Cuesta una llamada por nodo, asi que las tarjetas no lo piden.
        #
        # `incluir_30d=0` apaga la serie diaria del ultimo mes: otra llamada
        # externa por tarjeta, que la vista web no dibuja. Se apaga pidiendolo,
        # no se prende pidiendolo, para no romper a quien hoy la recibe (ver el
        # docstring de monitoreo_detalle).
        return Response(sv.monitoreo_detalle(
            int(proyecto_id),
            incluir_snapshot=_bandera(request, "incluir_snapshot", por_defecto=False),
            incluir_30d=_bandera(request, "incluir_30d", por_defecto=True),
        ))

    @action(
        detail=False, methods=["get"],
        url_path=r"monitoring/(?P<proyecto_id>[0-9]+)/inverters-power",
    )
    def monitoring_inverters_power(self, request, proyecto_id=None):
        return Response(sv.potencia_inversores(
            int(proyecto_id),
            request.query_params.get("date_from"),
            request.query_params.get("date_to"),
        ))
