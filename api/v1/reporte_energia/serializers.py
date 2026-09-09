"""Las entradas del reporte de energía.

Las salidas las arman los servicios como dict: son fichas compuestas de varias
fuentes, no filas de una tabla.
"""

from rest_framework import serializers

# Las fuentes que una persona puede confirmar a mano. `matriz_ceros` NO está:
# no es una fuente real, es un valor de reemplazo — cae al genérico
# "editado_manualmente" y por eso tampoco toca `caso`.
# 'cgm' es distinta de las demás: no aporta otra curva para la matriz, decide
# que NO se manda matriz porque Quoia ya reportó bien (ver editar_curva).
FUENTES_MANUALES = ["principal", "respaldo", "inversores", "historico", "reconectador", "cgm"]


class _Curva24(serializers.ListField):
    child = serializers.FloatField(allow_null=True)


class EditarCurvaSerializer(serializers.Serializer):
    curva_final = _Curva24()
    # Si viene, manda tal cual (origen 'manual') y no se recalcula.
    curva_respaldo_final = _Curva24(required=False, allow_null=True, default=None)
    fuente = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)


class CrearExclusionSerializer(serializers.Serializer):
    frontera_id = serializers.IntegerField()
    motivo = serializers.CharField(min_length=1)
    fecha_inicio = serializers.DateField()
    # Sin fecha fin la exclusión sigue vigente hasta que alguien la resuelva.
    fecha_fin_estimada = serializers.DateField(required=False, allow_null=True, default=None)


class EditarExclusionSerializer(serializers.Serializer):
    motivo = serializers.CharField(min_length=1)
    fecha_fin_estimada = serializers.DateField(required=False, allow_null=True, default=None)
