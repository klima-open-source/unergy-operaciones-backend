"""Servicios agrupados: PPA, Representación y CGM, Operación.

Vista de solo lectura sobre lo que ya existe. **No reemplaza a
`/contratos-servicio` ni a los endpoints de PPA**: se agrega al lado, y esos
siguen siendo los que escriben. Ver `docs/SERVICIOS_AGRUPACION.md`.
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.contratos.models import ContratoServicio
from apps.contratos.services import consulta as consulta_service
from apps.contratos.services import grupos as grupos_service


@class_logger_wrapper(name="Operaciones | Servicios")
class ServiciosViewSet(viewsets.GenericViewSet):
    """Servicios de la plataforma, agrupados.

    GET /api/v1/servicios[?proyecto_id=&cliente_id=&limit=]
    GET /api/v1/servicios/catalogo

    Los tres grupos salen SIEMPRE, aunque vengan vacíos: la vista tiene tres
    pestañas fijas y una que desaparece se lee como un error de carga.

    `proyecto_id` y `cliente_id` son opcionales y se excluyen entre sí. Sin
    ninguno devuelve todo con tope, que es lo que necesita la vista Servicios
    general; los filtros de la interfaz (estado, inversionista, portafolio)
    siguen aplicándose en el navegador y NO son parámetros de este endpoint --
    moverlos acá obligaría a recargar en cada clic.

    **Los conteos de cada grupo dicen qué cuentan.** `contratos` y `plantas` no
    son la misma cifra: un PPA de 5 plantas cuenta 1 contrato y 5 plantas, y
    mantenimiento + arriendo + internet de una misma planta cuentan 3 contratos
    y 1 planta.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "head", "options"]
    queryset = ContratoServicio.objects.none()

    def _entero(self, request, nombre):
        valor = request.query_params.get(nombre)
        if valor is None:
            return None
        if not valor.isdigit() or int(valor) < 1:
            raise ValidationError({nombre: "Debe ser un entero positivo."})
        return int(valor)

    def list(self, request, *args, **kwargs):
        proyecto_id = self._entero(request, "proyecto_id")
        cliente_id = self._entero(request, "cliente_id")
        if proyecto_id and cliente_id:
            raise ValidationError(
                "proyecto_id y cliente_id no se pueden combinar: "
                "los servicios de una planta y los de un cliente son "
                "conjuntos distintos."
            )

        limite = self._entero(request, "limit") or consulta_service.LIMITE_MAXIMO
        if limite > consulta_service.LIMITE_MAXIMO:
            raise ValidationError(
                {"limit": f"Máximo {consulta_service.LIMITE_MAXIMO}."}
            )

        return Response(consulta_service.agrupados(
            proyecto_id=proyecto_id, cliente_id=cliente_id, limite=limite
        ))

    @action(detail=False, methods=["get"], url_path="catalogo")
    def catalogo(self, request):
        """Los grupos y sus subservicios. Casi nunca cambia: el front lo cachea.

        Existe para que la vista deje de mantener su propia lista de grupos --
        dos definiciones que alguien tendría que acordarse de sincronizar.
        """
        return Response(grupos_service.catalogo())
