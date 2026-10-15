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
    """`{proyecto_id, portafolio_id}`: la pone en ese portafolio y la saca del que
    tenía (`null` = en ninguno). Un proyecto está en un solo portafolio.

    `en_portafolio: false` (cuerpo del front de varios portafolios) la saca de
    `portafolio_id` si es el suyo.
    """

    proyecto_id = serializers.IntegerField()
    portafolio_id = serializers.IntegerField(required=False, allow_null=True)
    en_portafolio = serializers.BooleanField(required=False)
