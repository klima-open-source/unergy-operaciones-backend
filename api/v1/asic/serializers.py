"""Serializers de GESCON/ASIC."""

from rest_framework import serializers

from apps.mercado_xm import models as mx_models
from apps.ppa import models as ppa_models
from apps.proyectos import models as py_models

TIPOS_SOLICITUD = (
    "registro", "modificacion", "terminacion", "desistimiento",
)
ESTADOS = ("borrador", "radicado", "publicado", "rechazado", "desistimiento")


class SolicitudSerializer(serializers.ModelSerializer):
    """Lectura. Los tres campos calculados los anota `asic_vigencia.preparar`."""

    proyecto_id = serializers.IntegerField(allow_null=True)
    contrato_ppa_id = serializers.IntegerField(allow_null=True)
    planta_nombre = serializers.CharField(read_only=True, allow_null=True)
    fecha_fin_efectiva = serializers.DateField(read_only=True, allow_null=True)
    es_version_vigente = serializers.BooleanField(read_only=True)

    class Meta:
        model = mx_models.AsicSolicitud
        fields = [
            "id", "proyecto_id", "contrato_ppa_id", "requerimiento_asic",
            "tipo_solicitud", "prioridad_limitacion", "codigo_sic_contrato",
            "codigo_sic_vendedor", "codigo_sic_comprador",
            "cedula_agente_vendedor", "cedula_agente_comprador",
            "contrato_interno", "nombre_contacto_solicitante",
            "fecha_solicitud", "fecha_inicio", "fecha_fin", "tipo_mercado",
            "tipo_asignacion", "porcentaje_fncer", "porcentaje_despacho",
            "estado_solicitud", "nombre_interno", "observaciones",
            "link_archivo", "reemplaza_anterior", "es_duplicado",
            "uso_del_recurso", "modalidad_pago", "fecha_envio_xm",
            "fecha_respuesta_xm", "numero_radicado",
            "planta_nombre", "fecha_fin_efectiva", "es_version_vigente",
            "created_at", "updated_at",
        ]


class SolicitudEscrituraSerializer(serializers.ModelSerializer):
    """Escritura. Acepta los MISMOS nombres de clave foránea que emite la lectura.

    `SolicitudSerializer` devuelve `proyecto_id` y `contrato_ppa_id`, y eso es lo
    que el formulario manda de vuelta. Como `ModelSerializer` nombra sus campos
    igual que las FK del modelo (`proyecto`, `contrato_ppa`), esos dos llegaban
    con un nombre que el serializer no reconocía — y DRF **descarta en silencio**
    lo que no reconoce. El PATCH respondía 200 con `validated_data` vacío y la
    planta no cambiaba: el contrato CTLP0003368 quedó en GD Marimonda sin forma
    de moverlo desde la vista (reportado el 2026-09-15).

    Se declaran los dos alias en vez de renombrar los campos para no romper a
    quien ya manda `proyecto`.
    """

    proyecto_id = serializers.PrimaryKeyRelatedField(
        source="proyecto", queryset=py_models.Proyecto.objects.all(),
        required=False, allow_null=True,
    )
    # `null` es un valor con significado: una terminación manda la planta vacía
    # a propósito, para que Cumplimiento prorratee el mes en vez de borrarlo.
    contrato_ppa_id = serializers.PrimaryKeyRelatedField(
        source="contrato_ppa", queryset=ppa_models.PpaContrato.objects.all(),
        required=False, allow_null=True,
    )

    class Meta:
        model = mx_models.AsicSolicitud
        fields = [
            "proyecto_id", "contrato_ppa_id",
            "proyecto", "contrato_ppa", "requerimiento_asic", "tipo_solicitud",
            "prioridad_limitacion", "codigo_sic_contrato", "codigo_sic_vendedor",
            "codigo_sic_comprador", "cedula_agente_vendedor",
            "cedula_agente_comprador", "contrato_interno",
            "nombre_contacto_solicitante", "fecha_solicitud", "fecha_inicio",
            "fecha_fin", "tipo_mercado", "tipo_asignacion", "porcentaje_fncer",
            "porcentaje_despacho", "estado_solicitud", "nombre_interno",
            "observaciones", "link_archivo", "reemplaza_anterior",
            "es_duplicado", "uso_del_recurso", "modalidad_pago",
            "fecha_envio_xm", "fecha_respuesta_xm", "numero_radicado",
        ]
        extra_kwargs = {c: {"required": False} for c in fields}


class ModificacionSerializer(serializers.Serializer):
    """Solo lo que una modificación puede cambiar; el resto se hereda."""

    codigo_sic_contrato = serializers.CharField()
    fecha_entrada = serializers.DateField()
    requerimiento_asic = serializers.CharField()
    estado_solicitud = serializers.ChoiceField(choices=ESTADOS)
    fecha_solicitud = serializers.DateField(required=False, allow_null=True)
    # Cuál de las plantas inscritas releva esta modificación. Obligatorio solo
    # cuando el SIC tiene más de una.
    proyecto_saliente_id = serializers.IntegerField(required=False, allow_null=True)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True)
    fecha_fin = serializers.DateField(required=False, allow_null=True)
    porcentaje_despacho = serializers.FloatField(required=False, allow_null=True)
    modalidad = serializers.CharField(required=False, allow_null=True)
    modalidad_pago = serializers.CharField(required=False, allow_null=True)
    link_archivo = serializers.CharField(required=False, allow_null=True)
    observaciones = serializers.CharField(required=False, allow_null=True)


class TerminacionSerializer(serializers.Serializer):
    """Solo lo que XM exige; la identidad del contrato se hereda del SIC."""

    codigo_sic_contrato = serializers.CharField()
    fecha_terminacion = serializers.DateField()
    estado_solicitud = serializers.ChoiceField(choices=ESTADOS)
    requerimiento_asic = serializers.CharField(required=False, allow_null=True)
    fecha_solicitud = serializers.DateField(required=False, allow_null=True)
    cedula_agente_vendedor = serializers.CharField(required=False, allow_null=True)
    cedula_agente_comprador = serializers.CharField(required=False, allow_null=True)
    link_archivo = serializers.CharField(required=False, allow_null=True)
    observaciones = serializers.CharField(required=False, allow_null=True)


class CambioSerializer(serializers.ModelSerializer):
    class Meta:
        model = mx_models.AsicCambioContrato
        fields = "__all__"


class DiccionarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = mx_models.GesconDiccionarioContrato
        fields = "__all__"
