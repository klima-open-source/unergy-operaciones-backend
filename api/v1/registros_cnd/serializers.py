"""Serializers de "Registros CND/ASIC".

Los resumenes ("en que va el proyecto") los arma el servicio como dict y salen
tal cual: replicarlos como serializer seria copiar un contrato de 20 campos que
ya esta escrito una vez. Aca solo se tipa lo que ENTRA (Create/Update/In) y lo
que sale del CRUD simple — equipos, documentos y parametros 9.3.

Espejo de `app/schemas/registros_cnd.py`. Cada `partial=True` de DRF equivale al
`exclude_unset=True` de Pydantic: un PATCH sin un campo no lo pisa con None.
"""

from rest_framework import serializers

from apps.registros_cnd import models as rc_models


class RegistroConexionCreateSerializer(serializers.ModelSerializer):
    proyecto_id = serializers.IntegerField()

    class Meta:
        model = rc_models.RegistroConexion
        fields = [
            "proyecto_id", "numero_expediente", "id_requerimiento_or",
            "numero_solicitud_appweb", "fecha_conexion_estimada",
            "vigencia_aprobacion_conexion", "fecha_visita_protecciones",
            "tipo_visita_protecciones", "exporta", "comercializador_es_or",
            "punto_conexion_texto", "notas",
        ]
        extra_kwargs = {c: {"required": False} for c in fields[1:]}


class RegistroConexionUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = rc_models.RegistroConexion
        fields = [
            "numero_expediente", "id_requerimiento_or", "numero_solicitud_appweb",
            "fecha_conexion_estimada", "vigencia_aprobacion_conexion",
            "fecha_visita_protecciones", "tipo_visita_protecciones", "exporta",
            "comercializador_es_or", "punto_conexion_texto", "notas",
        ]
        extra_kwargs = {c: {"required": False} for c in fields}


class TransicionSerializer(serializers.Serializer):
    etapa = serializers.CharField()
    a_estado = serializers.CharField()
    nota = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    actor = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)


class Parametros93Serializer(serializers.ModelSerializer):
    """Sirve de entrada (PUT, parcial) y de salida (GET). Igual que en Pydantic,
    donde `Parametros93Out` hereda de `Parametros93In`."""

    registro_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = rc_models.RegistroParametros93
        exclude = ["registro"]
        extra_kwargs = {"id": {"read_only": True}}

    def get_fields(self):
        campos = super().get_fields()
        for nombre, campo in campos.items():
            if not campo.read_only:
                campo.required = False
        return campos


class EquipoSerializer(serializers.ModelSerializer):
    registro_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = rc_models.RegistroEquipoFrontera
        exclude = ["registro"]
        extra_kwargs = {"id": {"read_only": True}}


class DocumentoSerializer(serializers.ModelSerializer):
    registro_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = rc_models.RegistroDocumento
        exclude = ["registro"]
        extra_kwargs = {"id": {"read_only": True}, "created_at": {"read_only": True}}


class ProyectoDisponibleSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nombre_comercial = serializers.CharField()
