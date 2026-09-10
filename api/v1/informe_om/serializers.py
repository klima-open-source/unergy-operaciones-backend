"""Serializers del Informe de Puesta en Marcha.

La ficha es sobre todo JSONB de paso: veintitantos campos que el frontend manda
y recibe sin que el backend interprete su contenido. Por eso la escritura usa
`JSONField` y una normalización de forma, en vez de replicar el árbol de modelos
anidados de Pydantic — lo único que esos modelos aportaban eran valores por
defecto, y eso se resuelve en `apps/om/services/forma_ficha.py`.
"""

from rest_framework import serializers

from apps.om import models as om_models
from apps.proyectos import models as py_models

ESTADOS_FICHA = ("borrador", "en_revision", "aprobado")

# Campos de la ficha que son JSONB de paso, con su valor vacío.
JSONB_DICT = (
    "checklist_fusion_solar", "checklist_frontera", "checklist_estacion_meteo",
    "checklist_reconectador", "objetivo_alcance", "datos_generales",
    "arquitectura_comunicacion", "configuracion_monitoreo", "observaciones",
)
JSONB_LISTA = (
    "pendientes", "equipos", "variables_monitoreadas", "protocolo_pruebas",
    "eventos_operativos", "recomendaciones", "firmas", "evidencia_arquitectura",
)


class ProyectoRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = py_models.Proyecto
        fields = [
            "id", "nombre_comercial", "municipio", "departamento",
            "potencia_ac_kw", "codigo_cnd", "fecha_entrada_operacion",
        ]


class ListItemSerializer(serializers.Serializer):
    """Una fila del listado: el proyecto más si ya tiene ficha y cómo va."""

    id = serializers.IntegerField()
    nombre_comercial = serializers.CharField(allow_null=True)
    municipio = serializers.CharField(allow_null=True)
    departamento = serializers.CharField(allow_null=True)
    potencia_ac_kw = serializers.FloatField(allow_null=True)
    tiene_ficha = serializers.BooleanField()
    estado_global = serializers.CharField()


class KpisSerializer(serializers.Serializer):
    pruebas_ejecutadas = serializers.IntegerField()
    pruebas_conformes = serializers.IntegerField()
    pruebas_no_conformes = serializers.IntegerField()
    eventos_total = serializers.IntegerField()
    eventos_cerrados = serializers.IntegerField()
    eventos_en_gestion = serializers.IntegerField()
    checklist_aprobados = serializers.IntegerField()
    checklist_total = serializers.IntegerField()
    estado_global = serializers.CharField()


class FichaSerializer(serializers.ModelSerializer):
    """Lectura y escritura de la ficha. Los JSONB salen siempre con su forma
    completa, nunca en `null`: el frontend indexa dentro sin comprobar."""

    estado = serializers.ChoiceField(choices=ESTADOS_FICHA, default="borrador")

    class Meta:
        model = om_models.ProyectoInformeOm
        fields = [
            "version", "elaborado_por", "actividad", "estado",
            "empresa_contratista", "fecha_energizacion", "fecha_inicio_operacion",
            *JSONB_LISTA, *JSONB_DICT, "conclusion",
        ]
        extra_kwargs = {
            campo: {"required": False, "allow_null": True}
            for campo in fields if campo != "estado"
        }


class DetalleSerializer(serializers.Serializer):
    """La respuesta del detalle: proyecto + ficha + calculado + datos en vivo."""

    proyecto = ProyectoRefSerializer()
    ficha = serializers.DictField()
    kpis = KpisSerializer()
    inversores = serializers.ListField()
    fusion_solar_estado = serializers.CharField()
    frontera_estado = serializers.CharField()
    estacion_meteo_estado = serializers.CharField()
    reconectador_estado = serializers.CharField()
    frontera_live = serializers.DictField()
    evidencia_relacionada = serializers.ListField()
