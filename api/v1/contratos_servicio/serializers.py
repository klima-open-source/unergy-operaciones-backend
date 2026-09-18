"""Serializers de contratos de servicio, sus facturas y sus pagos."""

from rest_framework import serializers

from apps.contratos import models as ct_models
from apps.contratos.services import comunidades as comunidades_service
from apps.contratos.services import grupos as grupos_service
from apps.plataforma.services.fechas import hoy_col
from apps.facturacion import models as fa_models
from apps.proyectos import models as py_models


class ContratoSerializer(serializers.ModelSerializer):
    proyecto_id = serializers.IntegerField(allow_null=True)
    contratante_id = serializers.IntegerField(allow_null=True)
    prestador_id = serializers.IntegerField(allow_null=True)
    inversionista_id = serializers.IntegerField(allow_null=True)
    portafolio_id = serializers.IntegerField(allow_null=True)
    nombre_proyecto = serializers.SerializerMethodField()
    # El frontend (ServiciosUnificadoView.vue) decide si mostrar la planta o el
    # boton "Sin proyecto" mirando este objeto anidado -- `proyecto_id` solo
    # (arriba) no le alcanza. Sin esto TODO contrato se veia "Sin proyecto",
    # incluso los que si tenian planta asociada.
    proyecto = serializers.SerializerMethodField()
    frontera_ids = serializers.SerializerMethodField()
    enlace_drive = serializers.SerializerMethodField()
    # Se AGREGAN junto a `servicio_aplica`, que no se toca: el front filtra por
    # ese campo y migra a estos cuando quiera. `subservicios` es lista porque un
    # contrato de representación+CGM cubre los dos, y `servicio_aplica` -- que
    # admite un solo valor -- solo puede nombrar uno; el otro queda invisible.
    # Ver `docs/SERVICIOS_AGRUPACION.md`.
    grupo = serializers.SerializerMethodField()
    subservicios = serializers.SerializerMethodField()

    class Meta:
        model = ct_models.ContratoServicio
        exclude = ["contratante", "prestador", "inversionista", "portafolio"]

    def get_nombre_proyecto(self, obj) -> str | None:
        return obj.proyecto.nombre_comercial if obj.proyecto else None

    def get_proyecto(self, obj) -> dict | None:
        if not obj.proyecto:
            return None
        return {
            "id": obj.proyecto.id,
            "nombre_comercial": obj.proyecto.nombre_comercial,
            "tipo_proyecto": obj.proyecto.tipo_proyecto,
        }

    def get_grupo(self, obj) -> str | None:
        return grupos_service.grupo_de_contrato(obj)

    def get_subservicios(self, obj) -> list[str]:
        return grupos_service.subservicios_de(obj)

    def get_frontera_ids(self, obj) -> list[int]:
        return [
            v.frontera_id
            for v in obj.contrato_frontera_por_contrato_servicio_id.all()
        ]

    def get_enlace_drive(self, obj) -> str | None:
        """El enlace vive como documento comercial `tipo='contrato'`.

        Se lee de la relación YA precargada; buscarlo por fila sería un N+1.
        """
        for documento in obj.cliente_documentos_comerciales_por_contrato_servicio_id.all():
            if documento.tipo == "contrato" and documento.archivo_url:
                return documento.archivo_url
        return None


class ContratoEscrituraSerializer(serializers.ModelSerializer):
    frontera_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True
    )
    enlace_drive = serializers.CharField(
        required=False, allow_null=True, allow_blank=True
    )
    # `fields = "__all__"` generaba el campo relacional bajo la clave "proyecto"
    # (el nombre del FK), pero el frontend manda "proyecto_id" -- DRF ignora en
    # silencio una clave que no reconoce, asi que "Asociar a un proyecto"
    # respondia 200 sin guardar nada. Mismo patron que ya usa
    # api/v1/verificacion_costos/serializers.py.
    proyecto_id = serializers.PrimaryKeyRelatedField(
        source="proyecto", queryset=py_models.Proyecto.objects.all(),
        allow_null=True, required=False,
    )

    class Meta:
        model = ct_models.ContratoServicio
        exclude = ["proyecto"]
        extra_kwargs = {"servicio_aplica": {"required": False}}

    def validate(self, datos):
        """Una planta en comunidad energética no recibe representación ni CGM.

        La validación vive acá y no solo en el front porque es la API la que de
        verdad impide guardarlo: el wizard puede saltarse, una llamada directa
        no. El texto sale de `comunidades.motivo_bloqueo` para que el mensaje
        sea el mismo en los dos lados.

        Se valida sobre los datos YA combinados con la instancia: en un PATCH
        que solo cambia la planta, `servicio_aplica` no viene en el cuerpo.
        """
        proyecto = datos.get("proyecto", getattr(self.instance, "proyecto", None))
        servicio = datos.get(
            "servicio_aplica", getattr(self.instance, "servicio_aplica", None)
        )
        if proyecto is None or servicio is None:
            return datos

        motivo = comunidades_service.motivo_bloqueo(
            proyecto.id, servicio,
            comunidades_service.plantas_en_comunidad(hoy_col()),
        )
        if motivo:
            raise serializers.ValidationError({"proyecto_id": motivo})
        return datos


class FacturaSerializer(serializers.ModelSerializer):
    contrato_id = serializers.IntegerField(read_only=True)
    inversionista_id = serializers.IntegerField(allow_null=True, required=False)

    class Meta:
        model = fa_models.ContratoFactura
        fields = [
            "id", "contrato_id", "tipo", "fecha", "inversionista_id",
            "numero_factura", "monto", "enlace_soporte",
            "created_at", "updated_at",
        ]


class FacturaEscrituraSerializer(serializers.ModelSerializer):
    class Meta:
        model = fa_models.ContratoFactura
        fields = [
            "tipo", "fecha", "inversionista", "numero_factura", "monto",
            "enlace_soporte",
        ]
        extra_kwargs = {c: {"required": False} for c in fields}



class FilaIndexacionSerializer(serializers.Serializer):
    anio = serializers.IntegerField()
    ipc_aplicado = serializers.FloatField(allow_null=True, required=False)
    valor = serializers.FloatField(allow_null=True, required=False)


class ImportarIndexacionSerializer(serializers.Serializer):
    proyecto = serializers.CharField()
    filas = FilaIndexacionSerializer(many=True)


class FusionarSerializer(serializers.Serializer):
    # `null` o ausente = fusionar todos los grupos limpios.
    ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True
    )
