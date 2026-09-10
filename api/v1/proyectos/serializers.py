"""Serializers de Proyectos.

Por ahora solo el de creación, que es COMPARTIDO: lo usan `POST /proyectos` y
`POST /comercial/oportunidades/{id}/proyectos`.

**El CRM no tiene un esquema propio de proyecto.** Antes declaraba sus cinco
campos a mano (nombre, kWp, departamento, municipio, operador) y descartaba en
silencio todo lo demás — coordenadas, dirección, tipo, estado, clasificación
regulatoria, códigos de cruce, curvas P50/P90. La planta nacía vacía y
`GET /comercial/proyectos-operando`, que resuelve casi toda su ficha desde el
Proyecto, devolvía campos nulos. Lo único que el CRM endurece es el operador de
red: ahí es obligatorio y en /proyectos es opcional.
"""

import json

from rest_framework import serializers

from apps.proyectos import models as py_models

CAMPOS_CREACION = [
    "nombre_comercial", "portafolio_id", "sub_project", "topico_liquidaciones",
    "clasificacion_regulatoria", "tipo_tecnologia", "tipo_proyecto",
    "potencia_instalada_kwp", "potencia_con_cen_mw", "produccion_especifica_kwh_kwp",
    "codigo_cnd", "estado", "fecha_entrada_operacion", "fecha_fin_representacion",
    "fecha_inicio_comercializacion", "fecha_comercializacion_editada_manual",
    "gen_mensual_promedio_mwh", "gen_promedio_origen",
    "departamento", "municipio", "direccion_vereda", "latitud", "longitud",
    "altitud_msnm", "operador_red_id", "project_id_solenium",
    "p90_mensual_kwh", "p50_mensual_kwh", "p99_mensual_kwh", "codigo_tsf",
    "srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa",
    "es_comunidad_energetica", "nombre_comunidad",
    "origina_code", "sunfactory_project_id", "fase_construccion",
    "fecha_estimada_energizacion", "avance_obra_pct", "origen",
]


class _CurvaMensual(serializers.JSONField):
    """P50/P90/P99: lista de 12 valores, que a veces llega como string JSON."""

    def to_internal_value(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                return None
        return data if isinstance(data, list) else None


class ProyectoCrearSerializer(serializers.ModelSerializer):
    portafolio_id = serializers.IntegerField(required=False, allow_null=True)
    operador_red_id = serializers.IntegerField(required=False, allow_null=True)
    latitud = serializers.FloatField(required=False, allow_null=True, min_value=-90, max_value=90)
    longitud = serializers.FloatField(required=False, allow_null=True, min_value=-180, max_value=180)
    altitud_msnm = serializers.IntegerField(required=False, allow_null=True, min_value=-100, max_value=6000)
    p90_mensual_kwh = _CurvaMensual(required=False, allow_null=True)
    p50_mensual_kwh = _CurvaMensual(required=False, allow_null=True)
    p99_mensual_kwh = _CurvaMensual(required=False, allow_null=True)

    class Meta:
        model = py_models.Proyecto
        fields = CAMPOS_CREACION
        extra_kwargs = {
            c: {"required": False, "allow_null": True}
            for c in CAMPOS_CREACION if c != "nombre_comercial"
        }


class ProyectoDesdeCrmSerializer(ProyectoCrearSerializer):
    """El mismo esquema, con el operador de red obligatorio (regla del CRM)."""

    operador_red_id = serializers.IntegerField()


class ProyectoActualizarSerializer(ProyectoCrearSerializer):
    """PATCH: todo opcional, incluido el nombre."""

    nombre_comercial = serializers.CharField(required=False)


# ── Sub-recursos del proyecto ────────────────────────────────────────────────

class ProyectoInversionistaSerializer(serializers.ModelSerializer):
    proyecto_id = serializers.IntegerField(read_only=True)
    cliente_id = serializers.IntegerField()
    cliente_nombre = serializers.CharField(source="cliente.razon_social_nombre",
                                           read_only=True, default="")

    class Meta:
        model = py_models.ProyectoInversionista
        fields = [
            "id", "proyecto_id", "cliente_id", "porcentaje_participacion",
            "es_patrimonio_autonomo", "fecha_inicio", "fecha_fin",
            "cliente_nombre", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "porcentaje_participacion": {"required": False, "allow_null": True},
            "es_patrimonio_autonomo": {"required": False},
            "fecha_inicio": {"required": False, "allow_null": True},
            "fecha_fin": {"required": False, "allow_null": True},
        }

    def validate_porcentaje_participacion(self, v):
        # 0–1, no 0–100: la columna guarda la fracción.
        if v is not None and not (0 <= v <= 1):
            raise serializers.ValidationError(
                "El porcentaje de participación debe estar entre 0 y 1 (equivale a 0%–100%)"
            )
        return v


class ProyectoInfoTecnicaSerializer(serializers.ModelSerializer):
    proyecto_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = py_models.ProyectoInfoTecnica
        exclude = ["proyecto"]
        extra_kwargs = {"id": {"read_only": True}}

    def get_fields(self):
        campos = super().get_fields()
        for campo in campos.values():
            if not campo.read_only:
                campo.required = False
        return campos


class ProyectoInversorSerializer(serializers.ModelSerializer):
    proyecto_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = py_models.ProyectoInversor
        fields = [
            "id", "proyecto_id", "nombre", "potencia_nominal_kw", "orden",
            "activo", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "nombre": {"required": False, "allow_null": True},
            "potencia_nominal_kw": {"required": False, "allow_null": True},
            "orden": {"required": False, "allow_null": True},
            "activo": {"required": False},
        }


class ProyectoAreaContactoSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    proyecto_id = serializers.IntegerField()
    tipo = serializers.CharField()
    cliente_id = serializers.IntegerField()
    cliente_nombre = serializers.CharField(allow_null=True, required=False)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class AreaContactoSetSerializer(serializers.Serializer):
    cliente_id = serializers.IntegerField()


class PpaResumenSerializer(serializers.Serializer):
    """Lo justo para identificar y filtrar por el contrato desde el listado. El
    detalle completo del PPA sigue viviendo en /ppa."""

    id = serializers.IntegerField()
    numero_codigo_contrato = serializers.CharField(allow_null=True, required=False)
    nombre_interno = serializers.CharField(allow_null=True, required=False)
    tipo_contrato = serializers.CharField(allow_null=True, required=False)
    comprador_nombre = serializers.CharField(allow_null=True, required=False)
    fecha_inicio = serializers.DateField(allow_null=True, required=False)
    fecha_fin = serializers.DateField(allow_null=True, required=False)


class ProyectoSerializer(serializers.ModelSerializer):
    """El detalle completo, con sus cinco relaciones anidadas."""

    portafolio_id = serializers.IntegerField(allow_null=True)
    operador_red_id = serializers.IntegerField(allow_null=True)
    operador_red_legal = serializers.SerializerMethodField()
    ppa_contratos = serializers.SerializerMethodField()
    inversionistas = ProyectoInversionistaSerializer(many=True, read_only=True)
    info_tecnica = serializers.SerializerMethodField()
    inversores = ProyectoInversorSerializer(many=True, read_only=True)
    area_contactos = serializers.SerializerMethodField()

    class Meta:
        model = py_models.Proyecto
        fields = CAMPOS_CREACION + [
            "id", "operador_red_legal", "ppa_contratos", "inversionistas",
            "info_tecnica", "inversores", "area_contactos",
            "created_at", "updated_at",
        ]

    def get_operador_red_legal(self, obj) -> str | None:
        from apps.comercial.services.pipeline import operador_red_legal

        return operador_red_legal(obj)

    def get_ppa_contratos(self, obj) -> list:
        """La relación pasa por la tabla puente y no conoce el borrado lógico de
        `ppa_contratos`: los eliminados se filtran acá para que no reaparezcan."""
        contratos = [
            v.contrato for v in obj.contratos_ppa.all()
            if v.contrato and v.contrato.deleted_at is None
        ]
        return PpaResumenSerializer(contratos, many=True).data

    def get_info_tecnica(self, obj):
        it = next(iter(obj.info_tecnica.all()), None)
        return ProyectoInfoTecnicaSerializer(it).data if it else None

    def get_area_contactos(self, obj) -> list:
        return [
            {
                "id": a.id, "proyecto_id": a.proyecto_id, "tipo": a.tipo,
                "cliente_id": a.cliente_id,
                "cliente_nombre": a.cliente.razon_social_nombre if a.cliente_id else None,
                "created_at": a.created_at, "updated_at": a.updated_at,
            }
            for a in obj.area_contactos.all()
        ]


class ProyectoListaSerializer(serializers.ModelSerializer):
    """`GET /proyectos/lista`: lo justo para identificar un proyecto y quedarse
    con su `id`. Sin relaciones anidadas a propósito — ese es el valor del paso
    del detalle."""

    class Meta:
        model = py_models.Proyecto
        fields = [
            "id", "nombre_comercial", "estado", "tipo_proyecto", "municipio",
            "departamento", "potencia_instalada_kwp", "sub_project", "codigo_tsf",
        ]


class PendienteConfirmarSerializer(serializers.Serializer):
    """Todos los campos son overrides OPCIONALES: si no se envían, se usa lo que
    trajo la fuente."""

    nombre_comercial = serializers.CharField(required=False, allow_null=True)
    tipo_proyecto = serializers.CharField(required=False, allow_null=True)
    municipio = serializers.CharField(required=False, allow_null=True)
    departamento = serializers.CharField(required=False, allow_null=True)
    potencia_ac_kw = serializers.FloatField(required=False, allow_null=True)
    capacidad_instalada_kwp = serializers.FloatField(required=False, allow_null=True)
