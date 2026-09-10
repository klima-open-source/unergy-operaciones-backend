"""Serializers de fronteras."""

from rest_framework import serializers

from apps.fronteras import models as fr_models


class FronteraSerializer(serializers.ModelSerializer):
    """Lectura. Los campos `proyecto_*`, `operador_*`, `clientes_cgm` y las dos
    banderas de generación los anota la vista; acá se leen como cualquier otro.
    """

    proyecto_id = serializers.IntegerField(allow_null=True)
    operador_red_id = serializers.IntegerField(allow_null=True)
    proyecto_nombre = serializers.SerializerMethodField()
    proyecto_fecha_inicio_comercializacion = serializers.SerializerMethodField()
    proyecto_potencia_instalada_mw = serializers.SerializerMethodField()
    proyecto_departamento = serializers.SerializerMethodField()
    proyecto_municipio = serializers.SerializerMethodField()
    proyecto_direccion = serializers.SerializerMethodField()
    proyecto_tipo_tecnologia = serializers.SerializerMethodField()
    proyecto_latitud = serializers.SerializerMethodField()
    proyecto_longitud = serializers.SerializerMethodField()
    proyecto_altitud_msnm = serializers.SerializerMethodField()
    operador_comercial = serializers.SerializerMethodField()
    operador_correos = serializers.SerializerMethodField()
    clientes_cgm = serializers.ListField(read_only=True, default=list)
    generando_actual = serializers.BooleanField(read_only=True, required=False)
    fecha_ultima_generacion = serializers.DateField(
        read_only=True, required=False, allow_null=True
    )

    class Meta:
        model = fr_models.Frontera
        exclude = ["proyecto", "operador_red"]

    def _proyecto(self, obj, campo, transformar=None):
        proyecto = obj.proyecto
        if proyecto is None:
            return None
        valor = getattr(proyecto, campo, None)
        return transformar(valor) if (transformar and valor is not None) else valor

    def get_proyecto_nombre(self, obj):
        return self._proyecto(obj, "nombre_comercial")

    def get_proyecto_fecha_inicio_comercializacion(self, obj):
        return self._proyecto(obj, "fecha_inicio_comercializacion")

    def get_proyecto_potencia_instalada_mw(self, obj):
        # La base guarda kWp; la ficha de frontera muestra MW.
        return self._proyecto(
            obj, "potencia_ac_kw", lambda v: float(v) / 1000
        )

    def get_proyecto_departamento(self, obj):
        return self._proyecto(obj, "departamento")

    def get_proyecto_municipio(self, obj):
        return self._proyecto(obj, "municipio")

    def get_proyecto_direccion(self, obj):
        return self._proyecto(obj, "direccion_vereda")

    def get_proyecto_tipo_tecnologia(self, obj):
        return self._proyecto(obj, "tipo_tecnologia")

    def get_proyecto_latitud(self, obj):
        return self._proyecto(obj, "latitud", float)

    def get_proyecto_longitud(self, obj):
        return self._proyecto(obj, "longitud", float)

    def get_proyecto_altitud_msnm(self, obj):
        return self._proyecto(obj, "altitud_msnm")

    def get_operador_comercial(self, obj):
        # El campo del modelo es `operador_red` (db_column operador_red_id), no
        # `operador`: con el nombre corto el listado entero devolvia 500.
        if obj.operador_red is None:
            return None
        return obj.operador_red.nombre_comercial or obj.operador_red.nombre_legal

    def get_operador_correos(self, obj) -> list:
        if obj.operador_red is None:
            return []
        return [c.email for c in obj.operador_red.contactos.all()]


class FronteraEscrituraSerializer(serializers.ModelSerializer):
    class Meta:
        model = fr_models.Frontera
        exclude = ["deleted_at"]
        extra_kwargs = {"codigo_frontera": {"required": False}}


class ConfirmarQuoiaSerializer(serializers.Serializer):
    proyecto_id = serializers.IntegerField()
    nombre_frontera = serializers.CharField(required=False, allow_null=True)
    tipo_frontera = serializers.CharField(required=False, allow_null=True)


class PendienteSerializer(serializers.Serializer):
    frt_code = serializers.CharField()
    nombre_quoia = serializers.CharField(allow_null=True)
    categoria = serializers.CharField()
    proyecto_sugerido_id = serializers.IntegerField(allow_null=True)
    proyecto_sugerido_nombre = serializers.CharField(allow_null=True)
