"""Serializers del panel de Arriendos."""

from rest_framework import serializers

from apps.arriendos import models as ar_models
from apps.clientes import models as cl_models


class ArrProyectoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ar_models.ArrProyecto
        fields = [
            "id", "codigo", "nombre", "fecha_firma_contrato", "valor_base",
            "activo",
        ]


class ArrProyectoEscrituraSerializer(serializers.ModelSerializer):
    class Meta:
        model = ar_models.ArrProyecto
        fields = ["codigo", "nombre", "fecha_firma_contrato", "valor_base", "activo"]


class ArrendadorSerializer(serializers.ModelSerializer):
    contrato_id = serializers.IntegerField(read_only=True)
    cliente_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ar_models.ArrArrendador
        fields = [
            "id", "contrato_id", "cliente_id", "nombre", "valor_base",
            "responsable_iva", "activo", "anticipo_pagado_desde",
            "anticipo_pagado_hasta", "observaciones",
        ]


class ArrendadorEscrituraSerializer(serializers.ModelSerializer):
    # `PrimaryKeyRelatedField` con `source`, y no el campo que genera
    # ModelSerializer: el FK se llama `cliente`, el frontend manda `cliente_id`,
    # y DRF descarta en silencio la clave que no reconoce. Ese fue exactamente el
    # bug de las otras partes --200 y el vínculo sin guardar--, y no vale la pena
    # repetirlo acá. Ver api/v1/contratos_servicio/serializers.py.
    cliente_id = serializers.PrimaryKeyRelatedField(
        source="cliente", queryset=cl_models.Cliente.objects.all(),
        allow_null=True, required=False,
    )

    class Meta:
        model = ar_models.ArrArrendador
        fields = [
            "nombre", "cliente_id", "valor_base", "responsable_iva", "activo",
            "anticipo_pagado_desde", "anticipo_pagado_hasta", "observaciones",
        ]


class SeleccionSerializer(serializers.ModelSerializer):
    arr_arrendador_id = serializers.IntegerField(allow_null=True)
    arr_proyecto_id = serializers.IntegerField(allow_null=True)

    class Meta:
        model = ar_models.ArrSeleccionMensual
        fields = [
            "id", "arr_arrendador_id", "arr_proyecto_id", "periodo", "incluido",
            "facturado", "valor_facturado_congelado", "motivo_exclusion",
        ]


class SeleccionItemSerializer(serializers.Serializer):
    # `proyecto_id` es el nombre viejo del mismo campo; se acepta por
    # retrocompatibilidad con clientes que aún no migraron.
    arr_arrendador_id = serializers.IntegerField(required=False, allow_null=True)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True)
    incluido = serializers.BooleanField()
    motivo_exclusion = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )


class GuardarSeleccionSerializer(serializers.Serializer):
    items = SeleccionItemSerializer(many=True)


class IpcSerializer(serializers.ModelSerializer):
    class Meta:
        model = ar_models.ArrIpcTasa
        fields = ["id", "año", "tasa", "confirmado", "fuente"]


class IpcUpsertSerializer(serializers.Serializer):
    tasa = serializers.FloatField()
    confirmado = serializers.BooleanField(default=False)
    fuente = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class DocumentoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    arr_proyecto_id = serializers.IntegerField(allow_null=True)
    proyecto_id = serializers.IntegerField(allow_null=True)
    periodo = serializers.CharField()
    pago_id = serializers.IntegerField(allow_null=True)
    codigo_contrato = serializers.CharField(allow_null=True)
    tipo_documento = serializers.CharField(allow_null=True)
    nombre_archivo = serializers.CharField(allow_null=True)
    nombre_secundario = serializers.CharField(allow_null=True)
    codigo_predio = serializers.CharField(allow_null=True)
    numero_cuenta_cobro = serializers.CharField(allow_null=True)
    nombre_arrendatario = serializers.CharField(allow_null=True)
    valor_individual = serializers.FloatField(allow_null=True)
    fecha_subida = serializers.DateTimeField(allow_null=True)
