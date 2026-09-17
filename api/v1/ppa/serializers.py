"""Serializers de contratos PPA."""

from rest_framework import serializers

from apps.clientes import models as cl_models
from apps.ppa import models as ppa_models


class ProyectoRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nombre_comercial = serializers.CharField(allow_null=True)


class TarifaSerializer(serializers.ModelSerializer):
    class Meta:
        model = ppa_models.PpaTarifa
        fields = ["id", "año", "mes", "tarifa"]


class TarifaEntradaSerializer(serializers.Serializer):
    año = serializers.IntegerField()
    mes = serializers.IntegerField(min_value=1, max_value=12)
    tarifa = serializers.FloatField(allow_null=True, required=False)


class CompromisoSerializer(serializers.ModelSerializer):
    class Meta:
        model = ppa_models.PpaCompromisoEnergia
        fields = [
            "id", "año", "mes", "energia_minima", "energia_maxima",
            "cantidad_proyectos",
        ]


class CompromisoEntradaSerializer(serializers.Serializer):
    año = serializers.IntegerField()
    mes = serializers.IntegerField(min_value=1, max_value=12)
    energia_minima = serializers.FloatField(allow_null=True, required=False)
    energia_maxima = serializers.FloatField(allow_null=True, required=False)
    cantidad_proyectos = serializers.IntegerField(allow_null=True, required=False)


class ContratoSerializer(serializers.ModelSerializer):
    """Lectura. `carpeta_link` sale de los documentos comerciales precargados."""

    responsable_id = serializers.IntegerField(allow_null=True)
    comprador_id = serializers.IntegerField(allow_null=True)
    vendedor_id = serializers.IntegerField(allow_null=True)
    proyectos = serializers.SerializerMethodField()
    tarifas = TarifaSerializer(many=True, read_only=True)
    compromisos_energia = CompromisoSerializer(
        source="compromisos", many=True, read_only=True
    )
    carpeta_link = serializers.SerializerMethodField()
    # Los pone la vista desde `contratos_service.visibilidad`; en el listado no
    # viajan (se calculan por contrato y serían N consultas).
    estado_cumplimiento = serializers.CharField(read_only=True, required=False)
    dias_restantes = serializers.IntegerField(read_only=True, required=False)
    cobertura_actual_pct = serializers.FloatField(read_only=True, required=False)

    class Meta:
        model = ppa_models.PpaContrato
        fields = [
            "id", "numero_codigo_contrato", "nombre_interno", "responsable_id",
            "comprador_id", "vendedor_id", "comprador_nombre", "comprador_nit",
            "vendedor_nombre", "vendedor_nit", "fecha_inicio", "fecha_fin",
            "tarifa_base", "indice_indexacion", "periodicidad_indexacion",
            "periodo_indexacion_base", "valor_indexacion_base",
            "cantidad_minima_kwh_mes", "cantidad_maxima_kwh_mes",
            "periodicidad_facturacion", "tiempo_pago", "condiciones_pago",
            "gescon_codigo", "gescon_fecha_inicio", "gescon_fecha_fin",
            "gescon_precio", "gescon_cantidades_kwh", "codigo_sic",
            "tipo_contrato", "renovacion_automatica", "es_comunidad_energetica",
            "fecha_entrada_comunidad",
            "proyectos", "tarifas", "compromisos_energia", "carpeta_link",
            "estado_cumplimiento", "dias_restantes", "cobertura_actual_pct",
            "created_at", "updated_at",
        ]

    def get_proyectos(self, obj) -> list:
        return [
            {
                "id": v.proyecto_id,
                "nombre_comercial": (
                    v.proyecto.nombre_comercial if v.proyecto else None
                ),
            }
            for v in obj.proyectos_vinculados.all()
        ]

    def get_carpeta_link(self, obj) -> str | None:
        """El enlace de Drive vive como documento comercial `tipo='contrato'`.

        Se lee de la relación YA precargada: buscarlo con una consulta por fila
        haría del listado un N+1.
        """
        for documento in obj.documentos_comerciales.all():
            if documento.tipo == "contrato" and documento.archivo_url:
                return documento.archivo_url
        return None


class ContratoEscrituraSerializer(serializers.ModelSerializer):
    """Escritura de un contrato PPA.

    **Las tres relaciones viajan como `<rol>_id`, igual que en la lectura.** Si
    se dejan con el nombre que les da el ORM (`comprador`, `vendedor`,
    `responsable`), DRF descarta en silencio las claves que manda el frontend
    —`comprador_id` y compañía— y el contrato se crea SIN partes y SIN
    responsable, devolviendo 201. Fue exactamente lo que pasó entre el port a
    Django (2026-09-04) y este arreglo; `PPAContratoCreate` de FastAPI las
    recibía con `_id` y el front nunca cambió. Ver `docs/DIAGNOSTICO_PPA.md` §3.

    `PrimaryKeyRelatedField` además valida: un id que no existe da 400 en vez de
    pasar de largo.
    """

    responsable_id = serializers.PrimaryKeyRelatedField(
        source="responsable", queryset=ppa_models.PpaResponsable.objects.all(),
        required=False, allow_null=True,
    )
    comprador_id = serializers.PrimaryKeyRelatedField(
        source="comprador", queryset=cl_models.Cliente.objects.all(),
        required=False, allow_null=True,
    )
    vendedor_id = serializers.PrimaryKeyRelatedField(
        source="vendedor", queryset=cl_models.Cliente.objects.all(),
        required=False, allow_null=True,
    )
    proyecto_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True
    )
    carpeta_link = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )

    class Meta:
        model = ppa_models.PpaContrato
        fields = [
            "numero_codigo_contrato", "nombre_interno", "responsable_id",
            "comprador_id", "vendedor_id", "comprador_nombre", "comprador_nit",
            "vendedor_nombre", "vendedor_nit", "fecha_inicio", "fecha_fin",
            "tarifa_base", "indice_indexacion", "periodicidad_indexacion",
            "periodo_indexacion_base", "valor_indexacion_base",
            "cantidad_minima_kwh_mes", "cantidad_maxima_kwh_mes",
            "periodicidad_facturacion", "tiempo_pago", "condiciones_pago",
            "gescon_codigo", "gescon_fecha_inicio", "gescon_fecha_fin",
            "gescon_precio", "gescon_cantidades_kwh", "codigo_sic",
            "tipo_contrato", "renovacion_automatica", "es_comunidad_energetica",
            "fecha_entrada_comunidad",
            "proyecto_ids", "carpeta_link",
        ]
        extra_kwargs = {c: {"required": False} for c in fields}


class ResponsableSerializer(serializers.ModelSerializer):
    n_contratos = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ppa_models.PpaResponsable
        fields = ["id", "nombre", "incluir_en_cumplimiento", "n_contratos"]


class ResponsableEntradaSerializer(serializers.Serializer):
    nombre = serializers.CharField()
    incluir_en_cumplimiento = serializers.BooleanField(default=True)


class ResponsableUpdateSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False, allow_null=True)
    incluir_en_cumplimiento = serializers.BooleanField(required=False)


class AsignarResponsableSerializer(serializers.Serializer):
    contrato_ids = serializers.ListField(child=serializers.IntegerField())
    # `null` desasigna.
    responsable_id = serializers.IntegerField(required=False, allow_null=True)


class IppMensualSerializer(serializers.Serializer):
    año = serializers.IntegerField()
    mes = serializers.IntegerField(min_value=1, max_value=12)
    valor = serializers.FloatField()
