"""Serializers de Clientes.

Espejo de `app/schemas/clientes.py`. Las salidas compuestas (panel 360, vista
comercial, servicios-contratos) las arma el servicio como dict y salen tal cual:
son agregados, no filas de una tabla.

El correo se normaliza a minúsculas y sin espacios ANTES de validar, igual que
el `field_validator` de Pydantic — si no, el UNIQUE de `(cliente_id, email,
tipo)` deja pasar "Juan@X.com" y "juan@x.com" como dos contactos distintos.
"""

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.clientes import models as cl_models


class _EmailNormalizado(serializers.EmailField):
    def to_internal_value(self, data):
        return super().to_internal_value((data or "").strip().lower())


class ContactoParaClienteSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    telefono = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    email = _EmailNormalizado()
    tipo = serializers.CharField(default="comercial")


class ClienteSerializer(serializers.ModelSerializer):
    """Salida del detalle: incluye los documentos comerciales."""

    documentos_comerciales = serializers.SerializerMethodField()

    class Meta:
        model = cl_models.Cliente
        fields = [
            "id", "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "created_at", "updated_at", "origen_tipo", "origen_detalle",
            "documentos_comerciales",
        ]

    def get_documentos_comerciales(self, obj):
        return ClienteDocumentoSerializer(obj.documentos_comerciales.all(), many=True).data


class ClienteListSerializer(serializers.ModelSerializer):
    """Salida del listado: sin documentos, que en una lista de 500 son 500 consultas."""

    class Meta:
        model = cl_models.Cliente
        fields = [
            "id", "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "created_at", "updated_at",
        ]


class ClienteEntradaSerializer(serializers.ModelSerializer):
    """POST y PATCH. `contactos` solo se lee al crear."""

    contactos = ContactoParaClienteSerializer(many=True, required=False, default=list)
    # `nit_cedula` es UNIQUE en la base (`unique_together = [("nit_cedula",)]`),
    # y de ahi DRF armaba solo un `UniqueTogetherValidator` que responde
    # `{"non_field_errors": ["Los campos nit_cedula deben formar un conjunto
    # único."]}`. Es correcto pero ilegible, y sustituyo sin quererlo al mensaje
    # que daba FastAPI ("Ya existe un cliente con ese NIT/cédula.", un 409 con
    # `detail` de texto): al portar, la explicacion que el usuario entendia se
    # perdio. El validador por campo devuelve el mismo error con el texto de
    # antes, y sigue cubriendo el PATCH -- que por este camino nunca llega a
    # tocar la base, asi que no puede reventar con un 500.
    nit_cedula = serializers.CharField(
        max_length=20, required=False, allow_null=True, allow_blank=True,
        validators=[UniqueValidator(
            queryset=cl_models.Cliente.objects.all(),
            message="Ya existe un cliente con ese NIT/cédula.",
        )],
    )

    class Meta:
        model = cl_models.Cliente
        fields = [
            "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "origen_tipo", "origen_detalle", "contactos",
        ]
        extra_kwargs = {c: {"required": False} for c in fields[1:]}
        # Sin esto DRF vuelve a agregar el `UniqueTogetherValidator` de
        # `(nit_cedula,)` y el error saldria dos veces, una en jerga: el
        # validador del campo (arriba) ya cubre ese UNIQUE. Es el unico
        # `unique_together` del modelo.
        validators = []


class TasaServicioSerializer(serializers.ModelSerializer):
    cliente_id = serializers.IntegerField(read_only=True)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True, default=None)

    class Meta:
        model = cl_models.ClienteTasaServicio
        fields = [
            "id", "cliente_id", "servicio", "proyecto_id",
            "iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "iva_pct": {"required": False, "allow_null": True},
            "retencion_pct": {"required": False, "allow_null": True},
            "reteiva_pct": {"required": False, "allow_null": True},
            "reteica_pct": {"required": False, "allow_null": True},
        }


class ContactoSerializer(serializers.ModelSerializer):
    cliente_id = serializers.IntegerField(read_only=True)
    email = _EmailNormalizado()

    class Meta:
        model = cl_models.Contacto
        fields = [
            "id", "cliente_id", "nombre", "email", "telefono", "tipo",
            "recibe_notificaciones", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "recibe_notificaciones": {"required": False},
        }


class ClienteDocumentoSerializer(serializers.ModelSerializer):
    # Nullable desde la generalización (revisión 122): un documento puede
    # pertenecer a un ContratoServicio o a un PpaContrato en vez de a un Cliente.
    cliente_id = serializers.IntegerField(read_only=True)
    contrato_servicio_id = serializers.IntegerField(read_only=True)
    ppa_contrato_id = serializers.IntegerField(read_only=True)
    oportunidad_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = cl_models.ClienteDocumentoComercial
        fields = [
            "id", "cliente_id", "contrato_servicio_id", "ppa_contrato_id",
            "tipo", "nombre", "numero", "fecha", "estado",
            "archivo_url", "archivo_nombre", "notas", "oportunidad_id",
            "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "numero": {"required": False, "allow_null": True},
            "fecha": {"required": False, "allow_null": True},
            "estado": {"required": False},
            "archivo_url": {"required": False, "allow_null": True},
            "archivo_nombre": {"required": False, "allow_null": True},
            "notas": {"required": False, "allow_null": True},
        }
