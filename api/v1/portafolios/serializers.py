"""Serializers de portafolios."""

from rest_framework import serializers

from apps.proyectos import models as py_models


class ProyectoEnPortafolioSerializer(serializers.ModelSerializer):
    """El proyecto reducido a lo que la pantalla de capas necesita mostrar."""

    nombre = serializers.CharField(source="nombre_comercial", allow_null=True)

    class Meta:
        model = py_models.Proyecto
        fields = ["id", "nombre", "sub_project", "municipio"]


class PortafolioSerializer(serializers.ModelSerializer):
    proyectos = ProyectoEnPortafolioSerializer(many=True, read_only=True)

    class Meta:
        model = py_models.Portafolio
        fields = ["id", "nombre", "proyectos"]


class PortafolioCreateSerializer(serializers.Serializer):
    nombre = serializers.CharField()

    def validate_nombre(self, valor):
        nombre = (valor or "").strip()
        if not nombre:
            raise serializers.ValidationError(
                "El nombre del portafolio no puede estar vacío"
            )
        return nombre


class PortafolioUpdateSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False)

    def validate_nombre(self, valor):
        nombre = (valor or "").strip()
        if not nombre:
            raise serializers.ValidationError("El nombre no puede estar vacío")
        return nombre


class AsignarSerializer(serializers.Serializer):
    """`{proyecto_id, portafolio_id, en_portafolio}`: true la mete, false la saca.

    Sin `en_portafolio` es el cuerpo del frontend anterior: mueve la planta a
    `portafolio_id` (null = la saca de todos).
    """

    proyecto_id = serializers.IntegerField()
    portafolio_id = serializers.IntegerField(required=False, allow_null=True)
    en_portafolio = serializers.BooleanField(required=False)
