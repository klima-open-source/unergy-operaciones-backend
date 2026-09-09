"""Las entradas del reporte de energía.

Las salidas las arman los servicios como dict: son fichas compuestas de varias
fuentes, no filas de una tabla.
"""

from rest_framework import serializers


class _Curva24(serializers.ListField):
    child = serializers.FloatField(allow_null=True)


class EditarCurvaSerializer(serializers.Serializer):
    curva_final = _Curva24()
    # Si viene, manda tal cual (origen 'manual') y no se recalcula.
    curva_respaldo_final = _Curva24(required=False, allow_null=True, default=None)
    # Texto libre a proposito, NO validado contra una lista. La unica lista de
    # fuentes es FUENTES_MANUALES (correcciones.py), y editar_curva() manda
    # cualquier valor desconocido al generico "editado_manualmente" -- que es
    # lo correcto para 'ceros' (Matriz de ceros: un valor de reemplazo, no una
    # fuente) y para la edicion celda por celda, los dos casos legitimos que
    # un ChoiceField convertiria en 400. Antes habia una copia de la lista
    # aca que no validaba nada y solo la leia un test: dos verdades para lo
    # mismo, listas para separarse.
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
