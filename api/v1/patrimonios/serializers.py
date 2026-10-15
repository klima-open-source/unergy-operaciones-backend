"""Serializers de patrimonios autónomos."""

from rest_framework import serializers

from apps.clientes import models as cl_models
from apps.proyectos import models as py_models


class ParticipacionEnPatrimonioSerializer(serializers.Serializer):
    """Una planta que se tiene a través del patrimonio, con su dueño."""

    id = serializers.IntegerField()
    proyecto_id = serializers.IntegerField()
    proyecto_nombre = serializers.CharField(source="proyecto.nombre_comercial", default=None)
    inversionista_nombre = serializers.CharField(
        source="inversionista.cliente.razon_social_nombre", default=None)
    porcentaje_participacion = serializers.DecimalField(
        max_digits=10, decimal_places=7, allow_null=True)


class SubpatrimonioSerializer(serializers.ModelSerializer):
    cliente_nombre = serializers.CharField(source="cliente.razon_social_nombre", default=None)
    cliente_nit = serializers.CharField(source="cliente.nit_cedula", default=None)
    participaciones = ParticipacionEnPatrimonioSerializer(many=True, read_only=True)

    class Meta:
        model = py_models.Patrimonio
        fields = ["id", "nombre", "codigo", "padre_id", "cliente_id", "cliente_nombre",
                  "cliente_nit", "participaciones"]


class PatrimonioSerializer(SubpatrimonioSerializer):
    """Un general, con sus subpatrimonios debajo."""

    subpatrimonios = SubpatrimonioSerializer(many=True, read_only=True)

    class Meta(SubpatrimonioSerializer.Meta):
        fields = SubpatrimonioSerializer.Meta.fields + ["subpatrimonios"]


class PatrimonioEscrituraSerializer(serializers.Serializer):
    """POST y PATCH. `padre_id` null = es un general."""

    nombre = serializers.CharField(required=False)
    codigo = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    padre_id = serializers.PrimaryKeyRelatedField(
        source="padre", queryset=py_models.Patrimonio.objects.all(),
        required=False, allow_null=True,
    )
    cliente_id = serializers.PrimaryKeyRelatedField(
        source="cliente", queryset=cl_models.Cliente.objects.all(),
        required=False, allow_null=True,
    )

    def validate_nombre(self, valor):
        nombre = (valor or "").strip()
        if not nombre:
            raise serializers.ValidationError("El nombre del patrimonio no puede estar vacío")
        return nombre

    def validate_codigo(self, valor):
        return (valor or "").strip() or None
