"""Catálogo de grupos de servicio: PPA, Representación y CGM, Operación.

Solo lectura, y solo el catálogo. Existe para que el front no mantenga su propia
lista de qué subservicios cubre cada pestaña -- esa definición vive una vez, en
`apps/contratos/services/grupos.py`, y `ServiciosUnificadoView.vue` la consume.

**Hubo también un `GET /api/v1/servicios` que devolvía los tres grupos armados,
con sus conteos y su semáforo. Se quitó antes de desplegarlo porque no le quedó
ningún consumidor**: el front sigue pidiendo `/contratos-servicio` y los de PPA,
y el panel de clientes agrupa en su propio servicio. Publicar un endpoint que
nadie llama es lo que la revisión 118 de Alembic documentó con
`servicio_representacion` -- una estructura que nada llena, que termina borrada.

La lógica que lo alimentaba NO se fue: `apps/contratos/services/unificado.py` y
`consulta.py` siguen ahí, probados, y el endpoint se recupera del historial de
git (commit "Servicios: los tres grupos en una sola consulta") el día que la
vista se migre.
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.contratos.models import ContratoServicio
from apps.contratos.services import grupos as grupos_service


@class_logger_wrapper(name="Operaciones | Servicios")
class ServiciosViewSet(viewsets.GenericViewSet):
    """Catálogo de grupos de servicio.

    GET /api/v1/servicios/catalogo
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "head", "options"]
    queryset = ContratoServicio.objects.none()

    @action(detail=False, methods=["get"], url_path="catalogo")
    def catalogo(self, request):
        """Los tres grupos y los subservicios de cada uno.

        Casi nunca cambia: el front lo pide una vez al abrir la vista.
        """
        return Response(grupos_service.catalogo())
