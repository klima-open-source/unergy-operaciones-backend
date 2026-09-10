"""Serializers de la vista de pólizas.

La fila mezcla tres tablas —`proyectos`, `proyecto_info_tecnica` y `polizas`— y
sale plana, que es como la consume la pantalla. Los campos de la póliza salen en
`None` (y `poliza_om` en `False`) cuando el proyecto todavía no tiene una: la
fila existe igual, con la parte de póliza vacía.
"""

from rest_framework import serializers

from apps.contratos import models as ct_models


class _CampoDeInfoTecnica(serializers.Field):
    """Lee un campo de `proyecto.info`, que puede no existir."""

    def __init__(self, campo, numerico=False, **kwargs):
        self.campo = campo
        self.numerico = numerico
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return getattr(instance, "info", None)

    def to_representation(self, info):
        valor = getattr(info, self.campo, None) if info else None
        return float(valor) if (self.numerico and valor is not None) else valor


class _CampoDePoliza(_CampoDeInfoTecnica):
    """Igual, pero contra `proyecto.poliza`, con su valor por defecto."""

    def __init__(self, campo, numerico=False, defecto=None, **kwargs):
        self.defecto = defecto
        super().__init__(campo, numerico=numerico, **kwargs)

    def get_attribute(self, instance):
        return getattr(instance, "poliza", None)

    def to_representation(self, poliza):
        if poliza is None:
            return self.defecto
        valor = getattr(poliza, self.campo, None)
        return float(valor) if (self.numerico and valor is not None) else valor


class PolizaFilaSerializer(serializers.Serializer):
    """Una fila del listado: proyecto + info técnica + póliza, aplanados."""

    proyecto_id = serializers.IntegerField(source="id")
    nombre_comercial = serializers.CharField(allow_null=True)
    tipo_proyecto = serializers.CharField(allow_null=True)
    municipio = serializers.CharField(allow_null=True)
    departamento = serializers.CharField(allow_null=True)
    direccion_vereda = serializers.CharField(allow_null=True)
    operador_red = serializers.CharField(allow_null=True)

    marca_paneles = _CampoDeInfoTecnica("marca_paneles")
    cantidad_total_paneles = _CampoDeInfoTecnica("cantidad_total_paneles")
    marca_inversores = _CampoDeInfoTecnica("marca_inversores")
    cantidad_inversores = _CampoDeInfoTecnica("cantidad_inversores")
    capacidad_instalada_kwp = _CampoDeInfoTecnica("capacidad_instalada_kwp", numerico=True)
    voltaje_red = _CampoDeInfoTecnica("voltaje_red")
    potencia_panel_kwp = _CampoDeInfoTecnica("potencia_panel_kwp")
    potencia_inversores_kwp = _CampoDeInfoTecnica("potencia_inversores_kwp")
    # Del PROYECTO y no de la info técnica: la potencia AC dejó de estar
    # duplicada en las dos tablas el 2026-09-10. Es el valor asegurable, así
    # que leerlo de la copia que ya no existe lo dejaría en blanco.
    potencia_ac_kw = serializers.FloatField(allow_null=True)

    numero_poliza = _CampoDePoliza("numero_poliza")
    poliza_om = _CampoDePoliza("poliza_om", defecto=False)
    fecha_vencimiento = _CampoDePoliza("fecha_vencimiento")
    valor_poliza = _CampoDePoliza("valor_poliza", numerico=True)
    mano_obra = _CampoDePoliza("mano_obra", numerico=True)
    estructura = _CampoDePoliza("estructura", numerico=True)
    paneles = _CampoDePoliza("paneles", numerico=True)
    inversores = _CampoDePoliza("inversores", numerico=True)
    otros = _CampoDePoliza("otros", numerico=True)
    valor_total_proyecto = _CampoDePoliza("valor_total_proyecto", numerico=True)
    link_estudio_suelos = _CampoDePoliza("link_estudio_suelos")
    ipp_base = _CampoDePoliza("ipp_base", numerico=True)
    ipp_base_fecha = _CampoDePoliza("ipp_base_fecha")
    ipp_provisional = _CampoDePoliza("ipp_provisional", numerico=True)
    ipp_provisional_fecha = _CampoDePoliza("ipp_provisional_fecha")
    tarifa_base = _CampoDePoliza("tarifa_base", numerico=True)
    generacion_anual_p90_kwh = _CampoDePoliza("generacion_anual_p90_kwh", numerico=True)
    valor_lucro_cesante = _CampoDePoliza("valor_lucro_cesante", numerico=True)
    updated_at = _CampoDePoliza("updated_at")


class PolizaUpsertSerializer(serializers.ModelSerializer):
    """Escritura. `valor_total_proyecto` y `valor_lucro_cesante` NO se aceptan:
    los calcula el servicio de dominio desde sus insumos."""

    class Meta:
        model = ct_models.Poliza
        fields = [
            "numero_poliza", "poliza_om", "fecha_vencimiento", "valor_poliza",
            "mano_obra", "estructura", "paneles", "inversores", "otros",
            "link_estudio_suelos", "ipp_base", "ipp_base_fecha", "ipp_provisional",
            "ipp_provisional_fecha", "tarifa_base", "generacion_anual_p90_kwh",
        ]
