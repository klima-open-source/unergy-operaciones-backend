"""Serializers del proxy de Liquidaciones.

Casi todas las respuestas son dicts que arma
`apps/liquidaciones/services/agregados.py` y viajan tal cual: son la traducción
de lo que devuelve una API externa, y volver a declararlas campo por campo solo
añadiría un sitio donde desincronizarse. Acá van únicamente las de ESCRITURA,
que sí hay que validar antes de mandar nada afuera.
"""

from rest_framework import serializers

VERSIONES = ("txf", "txr", "tx2", "tx3", "tx4", "tx5", "tx6", "tx7", "tx8")


class ProyectoUpdateSerializer(serializers.Serializer):
    """Los campos de configuración que la API externa acepta actualizar.

    Los nombres son los de la API, no traducciones: el cliente filtra por
    `api_externa.CAMPOS_PROYECTO` antes de mandar el PATCH, así que un campo
    bautizado distinto acá se descarta entre las dos capas sin avisar. Lo
    vigila `tests/test_proxy_liquidaciones_campos_escritura.py`.

    Hay un campo por frontera: `_gen` es la generadora y `_con` la de consumo.
    """

    sic_gen = serializers.CharField(required=False, allow_null=True)
    sic_con = serializers.CharField(required=False, allow_null=True)
    frt_gen = serializers.CharField(required=False, allow_null=True)
    frt_con = serializers.CharField(required=False, allow_null=True)
    ac_power = serializers.FloatField(required=False, allow_null=True)
    from_generator = serializers.BooleanField(required=False)
    from_commercializer = serializers.BooleanField(required=False)


class SubproyectoUpdateSerializer(serializers.Serializer):
    """Los tres ids de Quoia.

    Se manda solo lo que venga (`partial`): en esta API enviar `null` **borra**
    el id, así que un campo omitido y uno vacío NO significan lo mismo.

    Igual que arriba, los nombres son los de la API (`CAMPOS_QUOIA`).
    """

    quoia_report_gen_id = serializers.CharField(required=False, allow_null=True)
    quoia_report_con_id = serializers.CharField(required=False, allow_null=True)
    quoia_node_id = serializers.CharField(required=False, allow_null=True)


class PeriodoSerializer(serializers.Serializer):
    month = serializers.IntegerField(min_value=1, max_value=12)
    year = serializers.IntegerField(min_value=2020, max_value=2100)
    version = serializers.ChoiceField(choices=VERSIONES, default="txf")


class RepartoSerializer(PeriodoSerializer):
    total_ac_power = serializers.FloatField()
    override = serializers.BooleanField(default=False)
    last_version = serializers.ChoiceField(
        choices=VERSIONES, required=False, allow_null=True
    )


class DiagnosticoSerializer(PeriodoSerializer):
    project = serializers.CharField()


class ProyectoDeContratoSerializer(serializers.Serializer):
    project = serializers.CharField()
    energy_price = serializers.IntegerField(required=False, allow_null=True)
    floor = serializers.FloatField(required=False, allow_null=True)
    roof = serializers.FloatField(required=False, allow_null=True)


class ContratoEnergiaSerializer(serializers.Serializer):
    """Alta de un contrato de energía en la API externa.

    `code` y `company` NO son obligatorios, aunque lo parezcan: la API tampoco
    los exige. De los 106 contratos que existen, 25 no tienen código y 8 no
    tienen empresa — y los **20** de tipo `no_contract` no tienen código ni uno
    solo, porque «Sin contrato» no tiene contrato en XM que codificar.

    Declararlos obligatorios hacía imposible crear un «Sin contrato»: el
    formulario omite el código a propósito y el POST se caía con 400. La vista
    descarta los nulos antes de mandar (`if v is not None`), así que omitirlos
    no cambia lo que viaja afuera.
    """

    date_from = serializers.DateField()
    date_to = serializers.DateField()
    code = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    contract_type = serializers.CharField()
    tariff_price_type = serializers.CharField(required=False, allow_null=True)
    percentage = serializers.FloatField(required=False, allow_null=True)
    company = serializers.IntegerField(required=False, allow_null=True)
    proyectos = ProyectoDeContratoSerializer(many=True, default=list)
