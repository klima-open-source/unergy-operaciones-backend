"""Serializers de Fallas — espejo de `app/schemas/fallas.py`.

Dos salidas y no una: `FallaListaSerializer` (la tabla) y `FallaSerializer` (el
detalle). La lista NO declara seguimientos, intervalos ni inversores afectados —
si los declarara forzaría una consulta por fila, y la tabla no los muestra.

Los campos calculados (`sla_limite_horas_efectivo`, `dias_abierta`,
`tiempo_afectacion_horas`, `fotos_lista`) son propiedades del dominio, no
columnas: viven en `apps/monitoreo/services/fallas/dominio.py` y acá solo se
leen.
"""

from rest_framework import serializers

from apps.monitoreo import models as mo_models
from apps.monitoreo.services.fallas import dominio


class FallaCatEstadoSerializer(serializers.ModelSerializer):
    class Meta:
        model = mo_models.FallaCatEstado
        fields = ["id", "codigo", "etiqueta", "orden", "es_estado_final"]


class FallaCatPrioridadSerializer(serializers.ModelSerializer):
    class Meta:
        model = mo_models.FallaCatPrioridad
        fields = ["id", "codigo", "etiqueta", "nivel"]


class FallaCatCategoriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = mo_models.FallaCatCategoria
        fields = ["id", "codigo", "etiqueta", "icono", "color_hex", "orden"]


class FallaCatTipoSerializer(serializers.ModelSerializer):
    categoria = FallaCatCategoriaSerializer()

    class Meta:
        model = mo_models.FallaCatTipo
        fields = ["id", "codigo", "etiqueta", "descripcion", "categoria"]


class FallaCatResolucionSerializer(serializers.ModelSerializer):
    class Meta:
        model = mo_models.FallaCatResolucion
        fields = ["id", "codigo", "etiqueta"]


class UsuarioResumenSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nombre = serializers.CharField()
    email = serializers.CharField()


class ProyectoResumenSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nombre_comercial = serializers.CharField()
    sub_project = serializers.CharField(allow_null=True, required=False)


class FallaIntervaloSerializer(serializers.ModelSerializer):
    falla_id = serializers.IntegerField(read_only=True)
    # ponytail: FastAPI declaraba `duracion_horas` en FallaIntervaloOut y nunca
    # lo poblaba. Se repone la llave con su `null` historico para no romper a un
    # consumidor estricto; si algun dia hay que calcularla, va en `dominio`.
    duracion_horas = serializers.SerializerMethodField()

    class Meta:
        model = mo_models.FallaIntervalo
        fields = ["id", "falla_id", "inicio", "fin", "nota", "duracion_horas", "created_at"]

    def get_duracion_horas(self, obj) -> None:
        return None


class FallaInversorSerializer(serializers.ModelSerializer):
    proyecto_inversor_id = serializers.IntegerField(allow_null=True, required=False)
    tipos = serializers.JSONField(required=False)

    class Meta:
        model = mo_models.FallaInversor
        fields = ["id", "proyecto_inversor_id", "nombre", "potencia_kw", "tipos"]

    def to_representation(self, instance):
        datos = super().to_representation(instance)
        datos["tipos"] = datos.get("tipos") or []
        return datos


class FallaSeguimientoSerializer(serializers.ModelSerializer):
    falla_id = serializers.IntegerField(read_only=True)
    estado_nuevo = FallaCatEstadoSerializer(read_only=True)
    usuario = UsuarioResumenSerializer(read_only=True)

    class Meta:
        model = mo_models.FallaSeguimiento
        fields = ["id", "falla_id", "nota", "estado_nuevo", "usuario", "created_at"]


class FallaSeguimientoEntradaSerializer(serializers.Serializer):
    nota = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    estado_nuevo_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class FallaListaSerializer(serializers.ModelSerializer):
    """La tabla. Sin las relaciones del detalle: ver el docstring del módulo."""

    proyecto = ProyectoResumenSerializer(read_only=True)
    tipo = FallaCatTipoSerializer(read_only=True)
    estado = FallaCatEstadoSerializer(read_only=True)
    prioridad = FallaCatPrioridadSerializer(read_only=True)
    resolucion = FallaCatResolucionSerializer(read_only=True)
    registrado_por = UsuarioResumenSerializer(read_only=True)
    sla_limite_horas_efectivo = serializers.SerializerMethodField()
    sla_limite_dias = serializers.SerializerMethodField()
    dias_abierta = serializers.SerializerMethodField()
    tiempo_afectacion_horas = serializers.SerializerMethodField()
    tiene_fotos = serializers.SerializerMethodField()
    fotos_lista = serializers.SerializerMethodField()

    class Meta:
        model = mo_models.Falla
        fields = [
            "id", "codigo_interno", "proyecto_id", "proyecto", "tipo", "estado",
            "prioridad", "resolucion", "registrado_por", "descripcion",
            "fecha_identificacion", "hora_identificacion", "fecha_ocurrencia",
            "fecha_resolucion", "sla_limite_horas", "sla_limite_horas_efectivo",
            "sla_cumplido", "tiene_fotos", "fotos_lista", "notificacion",
            "alarma_monitoreo_id", "kwh_perdidos_estimado", "impacto_economico_cop",
            "causa_raiz", "acciones_correctivas", "fecha_programada",
            "dias_abierta", "tiempo_afectacion_horas", "sla_limite_dias",
            "categoria_codigo", "subtipo_codigo", "subtipo_detalle", "clasificacion",
            "pendiente_reclasificar", "frontera_afecta_medicion",
            "frontera_perdida_comunicacion", "inversores_perdida_comunicacion",
            "created_at", "updated_at",
        ]

    def get_sla_limite_horas_efectivo(self, obj) -> int:
        return dominio.sla_limite_horas_efectivo(obj)

    def get_sla_limite_dias(self, obj) -> int:
        return dominio.sla_limite_dias(obj)

    def get_dias_abierta(self, obj):
        return dominio.dias_abierta(obj)

    def get_tiempo_afectacion_horas(self, obj):
        return dominio.tiempo_afectacion_horas(obj)

    def get_tiene_fotos(self, obj) -> bool:
        return bool(obj.fotos_urls)

    def get_fotos_lista(self, obj) -> list:
        return dominio.fotos_lista(obj)


class FallaSerializer(FallaListaSerializer):
    """El detalle: lo de la lista más las tres relaciones que sí se muestran."""

    inversores_afectados = FallaInversorSerializer(many=True, read_only=True)
    seguimientos = FallaSeguimientoSerializer(many=True, read_only=True)
    intervalos = FallaIntervaloSerializer(many=True, read_only=True)

    class Meta(FallaListaSerializer.Meta):
        fields = FallaListaSerializer.Meta.fields + [
            "inversores_afectados", "seguimientos", "intervalos",
        ]


class FallaIntervaloEntradaSerializer(serializers.Serializer):
    inicio = serializers.DateTimeField()
    fin = serializers.DateTimeField(required=False, allow_null=True, default=None)
    nota = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)


class FallaInversorEntradaSerializer(serializers.Serializer):
    proyecto_inversor_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    potencia_kw = serializers.FloatField(required=False, allow_null=True, default=None)
    tipos = serializers.ListField(child=serializers.CharField(), required=False, default=list)


# `tipo_id`, `estado_id`, `prioridad_id`, `resolucion_id` y `alarma_monitoreo_id`
# son *attnames* de FK, no nombres de campo del modelo: `ModelSerializer` no sabe
# construirlos y cae en `build_property_field`, que los degrada a `ReadOnlyField`.
# El serializer valida sin errores y `validated_data` sale SIN ellos — un PATCH de
# estado respondia 200 sin cambiar nada. Van declarados a mano en los dos
# serializers de entrada, y como `IntegerField` (no `PrimaryKeyRelatedField`) para
# conservar el contrato: el FK lo valida la base y `dominio.integridad_a_error`
# traduce el `IntegrityError` a 422, igual que FastAPI.
_COLUMNAS_ENTRADA = [
    "tipo_id", "estado_id", "prioridad_id", "resolucion_id", "descripcion",
    "fecha_identificacion", "hora_identificacion", "fecha_ocurrencia",
    "fecha_resolucion", "sla_limite_horas", "fotos_urls", "notificacion",
    "alarma_monitoreo_id", "kwh_perdidos_estimado", "impacto_economico_cop",
    "causa_raiz", "acciones_correctivas", "fecha_programada",
    "categoria_codigo", "subtipo_codigo", "subtipo_detalle",
    "frontera_afecta_medicion", "frontera_perdida_comunicacion",
]


_COLUMNAS_ACTUALIZAR = [c for c in _COLUMNAS_ENTRADA if c != "alarma_monitoreo_id"]


class FallaCrearSerializer(serializers.ModelSerializer):
    """POST. `intervalos`, `inversores` y `generar_impacto` NO son columnas."""

    proyecto_id = serializers.IntegerField()
    estado_id = serializers.IntegerField()
    prioridad_id = serializers.IntegerField()
    tipo_id = serializers.IntegerField(required=False, allow_null=True)
    resolucion_id = serializers.IntegerField(required=False, allow_null=True)
    alarma_monitoreo_id = serializers.IntegerField(required=False, allow_null=True)
    # Sin `default=None`: `intervalos` es tambien el `related_name` del FK inverso,
    # y DRF se niega a darle default a un campo m2m — reventaba con ValueError al
    # CONSTRUIR el serializer, o sea 500 en todo POST. El view ya tolera la
    # ausencia (`datos.pop("intervalos", None)`).
    intervalos = FallaIntervaloEntradaSerializer(many=True, required=False, allow_null=True)
    inversores = FallaInversorEntradaSerializer(many=True, required=False, allow_null=True)
    generar_impacto = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = mo_models.Falla
        fields = ["proyecto_id", *_COLUMNAS_ENTRADA, "intervalos", "inversores", "generar_impacto"]
        extra_kwargs = {
            c: {"required": False} for c in _COLUMNAS_ENTRADA
            if c not in ("descripcion", "fecha_identificacion")
        }


class FallaActualizarSerializer(serializers.ModelSerializer):
    """PATCH. `sla_cumplido` NO es editable: siempre lo calcula
    `dominio.sincronizar_resolucion`."""

    tipo_id = serializers.IntegerField(required=False, allow_null=True)
    estado_id = serializers.IntegerField(required=False, allow_null=True)
    prioridad_id = serializers.IntegerField(required=False, allow_null=True)
    resolucion_id = serializers.IntegerField(required=False, allow_null=True)
    intervalos = FallaIntervaloEntradaSerializer(many=True, required=False, allow_null=True)
    inversores = FallaInversorEntradaSerializer(many=True, required=False, allow_null=True)

    class Meta:
        model = mo_models.Falla
        # `alarma_monitoreo_id` fuera: `FallaUpdate` no lo expone, y aceptarlo
        # dejaria reasignar la alarma de una falla por PATCH.
        fields = [*_COLUMNAS_ACTUALIZAR, "pendiente_reclasificar", "intervalos", "inversores"]
        extra_kwargs = {c: {"required": False} for c in _COLUMNAS_ACTUALIZAR}
