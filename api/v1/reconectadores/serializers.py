"""Serializers de reconectadores."""

from rest_framework import serializers

from apps.monitoreo.services import reconectadores as relay_service


class RelayEstadoSerializer(serializers.Serializer):
    """Estado y telemetría de un relay. No hay modelo: el dato vive en SolarView."""

    proyecto_id = serializers.IntegerField()
    nombre = serializers.CharField(allow_null=True)
    sol_id = serializers.IntegerField()
    # True=ON, False=OFF, None=sin dato.
    active = serializers.BooleanField(allow_null=True)
    ultima_actualizacion = serializers.CharField(allow_null=True)

    def get_fields(self):
        campos = super().get_fields()
        # Las 14 medidas eléctricas son todas float opcionales; se declaran
        # desde el mismo mapa que usa el servicio para que no puedan divergir.
        for nombre in relay_service.TELEMETRIA:
            campos[nombre] = serializers.FloatField(allow_null=True, required=False)
        return campos


class ComandoSerializer(serializers.Serializer):
    """El comando va con el token del servidor. `password` es la de la cuenta de
    la PLATAFORMA de quien lo manda: confirma la acción, igual que SolarView
    pide la contraseña antes de abrir o cerrar un reconectador. No se guarda ni
    va al log."""

    accion = serializers.ChoiceField(choices=["ON", "OFF"])
    password = serializers.CharField(write_only=True, trim_whitespace=False)
