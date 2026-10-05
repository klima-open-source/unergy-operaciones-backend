"""ViewSet de reconectadores."""

from django.shortcuts import get_object_or_404
from rest_framework import viewsets
from rest_framework.decorators import action
import logging

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.monitoreo.services import reconectadores as relay_service
from apps.proyectos import models as py_models

from . import queryset as relay_queryset
from . import serializers as relay_serializers


logger = logging.getLogger("operaciones.reconectadores")

# Quién puede abrir o cerrar un reconectador. `admin` pasa siempre (ver
# `RolePermission`). Leer el estado no exige rol.
ROLES_COMANDO = ["operaciones"]
# Quién enciende o apaga el interruptor general. `RolePermission` deja pasar a
# `admin` siempre, así que esta lista basta para que SOLO admin pueda.
ROLES_INTERRUPTOR = ["admin"]


def _sv_id(proyecto) -> int:
    try:
        return int(proyecto.project_id_solarview)
    except (TypeError, ValueError):
        raise ValidationError("Este proyecto no tiene ID de SolarView configurado")


@class_logger_wrapper(name="Operaciones | Monitoreo | Reconectadores")
class ReconectadorViewSet(viewsets.GenericViewSet):
    """Estado y comandos ON/OFF de los relays.

    GET  /api/v1/reconectadores/estados                estado y telemetría de todos
    GET  /api/v1/reconectadores/interruptor            si los comandos están encendidos
    POST /api/v1/reconectadores/interruptor            encenderlos o apagarlos (admin)
    GET  /api/v1/reconectadores/debug-relay/{id}       respuesta cruda de SolarView
    POST /api/v1/reconectadores/{id}/comando           ON/OFF

    Las dos cosas van a SolarView con el token del servidor. **Mandar un
    comando** exige rol `admin` u `operaciones`, que el interruptor
    `RECONECTADORES_COMANDOS_HABILITADOS` esté encendido y el usuario y la
    contraseña de SolarView de quien lo manda; cada intento queda en el log con
    el usuario, la planta y la acción. Abrir un relay apaga una
    planta y puede haber gente en sitio.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = py_models.Proyecto.objects.none()

    @property
    def required_role(self):
        # Solo el comando tiene rol: sin `required_role`, `RolePermission`
        # deja pasar a cualquier usuario autenticado, incluso `solo_lectura`.
        if self.action == "comando":
            return ROLES_COMANDO
        if self.action == "interruptor" and self.request.method == "POST":
            return ROLES_INTERRUPTOR
        return []

    @action(detail=False, methods=["get", "post"], url_path="interruptor")
    def interruptor(self, request):
        if request.method == "GET":
            return Response(
                relay_serializers.InterruptorSerializer(relay_service.estado_interruptor()).data
            )

        entrada = relay_serializers.InterruptorSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        habilitado = entrada.validated_data["habilitado"]
        quien = str(request.user)
        estado = relay_service.cambiar_interruptor(habilitado, quien)
        logger.warning(
            "interruptor de reconectadores %s",
            "ENCENDIDO" if habilitado else "apagado",
            extra={"usuario_id": getattr(request.user, "id", None), "usuario": quien},
        )
        return Response(relay_serializers.InterruptorSerializer(estado).data)

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

        tiene, medidas = relay_service.leer_relay(sv_id)
        return Response({
            "sol_id": sv_id,
            "url": relay_service.url_relay(),
            "raw": cliente.get_recloser_con_estado(sv_id)[1],
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
        sv_id = _sv_id(proyecto)
        rastro = {
            "usuario_id": getattr(request.user, "id", None),
            "usuario_solarview": datos["username"],
            "proyecto_id": proyecto.id, "sv_id": sv_id, "accion": datos["accion"],
        }

        # Los candados (interruptor, credenciales de SolarView) los revisa el
        # servicio; acá solo se traducen a HTTP.
        try:
            respuesta = relay_service.enviar_comando(
                sv_id, datos["accion"], datos["username"], datos["password"],
            )
        except relay_service.ComandosDeshabilitados as exc:
            logger.warning("comando de reconectador rechazado: deshabilitado", extra=rastro)
            return Response({"detail": str(exc)}, status=503)
        except relay_service.CredencialesInvalidas as exc:
            logger.warning("comando de reconectador rechazado: credenciales", extra=rastro)
            return Response({"detail": str(exc)}, status=400)
        except relay_service.SolarViewNoConfigurado as exc:
            return Response({"detail": str(exc)}, status=503)
        except relay_service.SolarViewNoResponde as exc:
            logger.error("comando de reconectador sin respuesta: %s", exc, extra=rastro)
            return Response({"detail": str(exc)}, status=503)

        logger.warning(
            "comando de reconectador enviado: HTTP %s", respuesta.status_code,
            extra={**rastro, "respuesta": respuesta.text[:200]},
        )
        # Que la próxima consulta de estados vaya a SolarView y no al caché.
        relay_service.olvidar_estados()
        if respuesta.status_code >= 300:
            return Response(
                {"detail": (
                    f"SolarView → HTTP {respuesta.status_code}: "
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
