"""ViewSet de reconectadores."""

from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import (
    AuthenticationFailed, NotFound, ValidationError,
)
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.monitoreo.services import reconectadores as relay_service
from apps.proyectos import models as py_models

from . import queryset as relay_queryset
from . import serializers as relay_serializers


def _sol_id(proyecto) -> int:
    if not proyecto.project_id_solenium:
        raise ValidationError("Este proyecto no tiene ID de Solenium configurado")
    return int(proyecto.project_id_solenium)


@class_logger_wrapper(name="Operaciones | Monitoreo | Reconectadores")
class ReconectadorViewSet(viewsets.GenericViewSet):
    """Estado y comandos ON/OFF de los relays.

    GET  /api/v1/reconectadores/estados                estado y telemetría de todos
    GET  /api/v1/reconectadores/debug-relay/{id}       respuesta cruda de SolarView
    POST /api/v1/reconectadores/{id}/comando           ON/OFF

    Leer va a SolarView con el token del servidor. **Mandar un comando** sigue en
    Solenium y **exige las credenciales del usuario** en el cuerpo: se validan en
    cada llamada y no se almacenan. Abrir un relay apaga una planta y tiene que
    quedar atribuido.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = py_models.Proyecto.objects.none()

    @action(detail=False, methods=["get"], url_path="estados")
    def estados(self, request):
        try:
            relay_service.cliente()
        except relay_service.SolarViewNoConfigurado as exc:
            return Response({"detail": str(exc)}, status=503)

        estados = relay_service.estados_de(relay_queryset.proyectos_con_relay())
        return Response(
            relay_serializers.RelayEstadoSerializer(estados, many=True).data
        )

    @action(
        detail=False, methods=["get"],
        url_path=r"debug-relay/(?P<proyecto_id>[^/.]+)",
    )
    def debug_relay(self, request, proyecto_id=None):
        """Respuesta cruda de SolarView para el relay de un proyecto."""
        try:
            cliente = relay_service.cliente()
        except relay_service.SolarViewNoConfigurado as exc:
            return Response({"detail": str(exc)}, status=503)

        proyecto = py_models.Proyecto.objects.filter(pk=proyecto_id).first()
        try:
            sv_id = int(proyecto.project_id_solarview) if proyecto else None
        except (TypeError, ValueError):
            sv_id = None
        if sv_id is None:
            raise NotFound("Proyecto sin project_id_solarview")

        url = relay_service.url_relay(cliente)
        tiene, medidas = relay_service.leer_relay(sv_id)
        return Response({
            "sol_id": sv_id,
            "url": url,
            "raw": cliente._get(url, params={"project_id": sv_id}),
            "tiene_reconectador": tiene,
            "parsed": relay_service.build_estado(
                proyecto.id, proyecto.nombre_comercial, sv_id, medidas
            ) if tiene else None,
        })

    @action(detail=True, methods=["post"], url_path="comando")
    @log_endpoint(name="Operaciones | Monitoreo | Reconectadores | Comando")
    def comando(self, request, pk=None):
        proyecto = get_object_or_404(py_models.Proyecto, pk=pk)
        entrada = relay_serializers.ComandoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        sol_id = _sol_id(proyecto)

        try:
            token = relay_service.token_de_usuario(
                datos["username"], datos["password"]
            )
            respuesta = relay_service.enviar_comando(
                sol_id, datos["accion"], datos["is_interrogating"], token
            )
        except relay_service.CredencialesInvalidas as exc:
            raise AuthenticationFailed(str(exc))
        except relay_service.SoleniumNoResponde as exc:
            return Response({"detail": str(exc)}, status=503)
        except relay_service.RespuestaInesperada as exc:
            return Response({"detail": str(exc)}, status=502)

        if respuesta.status_code >= 300:
            return Response(
                {"detail": (
                    f"Solenium → HTTP {respuesta.status_code}: "
                    f"{respuesta.text[:120]}"
                )},
                status=502,
            )
        return Response({
            "success": True,
            "message": f'Comando {datos["accion"]} enviado a {proyecto.nombre_comercial}',
            "accion": datos["accion"],
            "detail": respuesta.text[:200],
        })
