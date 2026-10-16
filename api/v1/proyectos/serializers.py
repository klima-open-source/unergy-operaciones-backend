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

import re

from rest_framework import serializers

from apps.contratos.models import GrupoContrato
from apps.proyectos import models as py_models

CAMPOS_CREACION = [
    "nombre_comercial", "portafolio_id", "sub_project", "topico_liquidaciones",
    "clasificacion_regulatoria", "tipo_tecnologia", "tipo_proyecto",
    "potencia_ac_kw", "produccion_especifica_kwh_kwp", "capacidad_instalada_kwp", "voltaje_red",
    "estado", "fecha_entrada_operacion", 
    "fecha_inicio_comercializacion", 
    "gen_mensual_promedio_mwh", "gen_promedio_origen",
    "departamento", "municipio", "direccion_vereda", "latitud", "longitud",
    "altitud_msnm", "operador_red_id", "project_id_solenium",
    "p90_mensual_kwh", "p50_mensual_kwh", "p99_mensual_kwh", "codigo_tsf",
    "origina_code", "sunfactory_project_id", "fase_construccion",
    "fecha_estimada_energizacion", "avance_obra_pct", "origen",
    # La minigranja de originabot: la manda «importar desde TSF».
    "originabot_minifarm_id",
    # El ECS de TSF: lo manda «Sincronizar con TSF».
    "ecs_estado", "ecs_codigo_insumo", "ecs_vencimiento", "ecs_archivo_url",
    "ecs_conformidad_url",
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


# Un codigo CREG: COL + letras y numeros, sin separadores. El `base_name`
# de Sun Factory es ESE prefijo mas el sitio ("COLCEST924P3_ASTREA_ORIENTE"),
# y ese entero va en `origina_code`, no aca.
_PREFIJO_CREG = re.compile(r"^COL[A-Z0-9]+$")


class ProyectoCrearSerializer(serializers.ModelSerializer):
    # Mismo alias de transición que en la salida, para el lado de la escritura:
    # un frontend todavía sin promover manda `potencia_instalada_kwp` al
    # guardar, y sin esto ese campo se descartaría en silencio.
    potencia_instalada_kwp = serializers.DecimalField(
        source="potencia_ac_kw", max_digits=12, decimal_places=3,
        required=False, allow_null=True, write_only=True,
    )
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
        fields = CAMPOS_CREACION + ["potencia_instalada_kwp"]  # alias, ver arriba
        extra_kwargs = {
            c: {"required": False, "allow_null": True}
            for c in CAMPOS_CREACION if c != "nombre_comercial"
        }

    def validate_codigo_tsf(self, valor):
        """Solo el prefijo CREG, no el `base_name` entero.

        Sun Factory manda un solo dato, el `base_name`
        (`COLCEST924P3_ASTREA_ORIENTE`), y de ahi salen dos campos nuestros:
        `origina_code` se queda con el entero y `codigo_tsf` con el prefijo
        (`COLCEST924P3`).

        Pero `codigo_tsf` tambien lo escribe una persona en el formulario, y
        hasta ahora nadie validaba el formato. Tres proyectos --las tres
        Astrea-- quedaron con el `base_name` completo en el campo del prefijo y
        `origina_code` vacio, y eso rompe el emparejamiento de `tsf_sync`, que
        cruza por el prefijo para reconocer un proyecto que ya existe. De hecho
        hay una linea que tambien prueba `Q(codigo_tsf=base_name)`: un parche
        para justo este caso.

        Se rechaza en vez de recortar en silencio: quien pego el valor completo
        probablemente quiso registrar el proyecto de Sun Factory, y decirselo es
        mas util que guardar la mitad sin avisar.
        """
        if valor in (None, ""):
            return valor
        codigo = str(valor).strip()
        if _PREFIJO_CREG.match(codigo):
            return codigo
        prefijo = codigo.split("_", 1)[0]
        if _PREFIJO_CREG.match(prefijo):
            raise serializers.ValidationError(
                f"Parece el codigo completo de Sun Factory. El Codigo TSF es solo "
                f"el prefijo: '{prefijo}'."
            )
        raise serializers.ValidationError(
            "Formato invalido: se espera un codigo CREG como 'COLCEST924P3'."
        )


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
    # El patrimonio autónomo por el que se tiene la parte (opcional): el cliente
    # sigue siendo el dueño; al patrimonio se le factura y liquida.
    patrimonio_id = serializers.PrimaryKeyRelatedField(
        source="patrimonio", queryset=py_models.Patrimonio.objects.all(),
        required=False, allow_null=True,
    )
    patrimonio_nombre = serializers.CharField(source="patrimonio.nombre", read_only=True,
                                              default=None)

    class Meta:
        model = py_models.ProyectoInversionista
        fields = [
            "id", "proyecto_id", "cliente_id", "porcentaje_participacion",
            "patrimonio_id", "patrimonio_nombre", "fecha_inicio", "fecha_fin",
            "cliente_nombre", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "porcentaje_participacion": {"required": False, "allow_null": True},
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


class ProyectoInfoTecnicaSerializer(serializers.Serializer):
    """`PUT /proyectos/{id}/info-tecnica`, con la forma de la vieja info técnica.

    Desde el 15-oct no hay tabla de info técnica: `equipos.guardar_ficha_tecnica`
    reparte estos campos en `proyectos`, `equipos` e `inversores`. Se conserva
    para el frontend que todavía lo llama; la ficha nueva usa `/equipos`.
    `potencia_ac_kw` va al proyecto.
    """

    voltaje_red = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    capacidad_instalada_kwp = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True)
    potencia_ac_kw = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False, allow_null=True)
    tipo_tracker = serializers.ChoiceField(choices=["1P", "2P"], required=False, allow_null=True,
                                           allow_blank=True)
    cantidad_total_paneles = serializers.IntegerField(required=False, allow_null=True)
    potencia_panel_kwp = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_paneles = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    cantidad_inversores = serializers.IntegerField(required=False, allow_null=True)
    potencia_inversores_kwp = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_inversores = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    cantidad_strings = serializers.IntegerField(required=False, allow_null=True)
    marca_transformador = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_reconectador_rele = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_totalizador = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_seguidor_solar = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_medidores_frontera = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_modem_reconectador = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_modems_frontera = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    ip_modem_reconectador = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    url_ubicacion = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    cctv_estado = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    marca_cctv = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    seguridad_fisica = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    tiene_internet = serializers.BooleanField(required=False, allow_null=True)


class ProyectoInversorSerializer(serializers.ModelSerializer):
    """Un inversor de la planta, con su potencia. Cuelga del equipo de inversores."""

    proyecto_id = serializers.IntegerField(read_only=True)
    equipo_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = py_models.ProyectoInversor
        fields = [
            "id", "proyecto_id", "equipo_id", "nombre", "potencia_nominal_kw", "orden",
            "cantidad_strings", "estado", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "nombre": {"required": False, "allow_null": True},
            "potencia_nominal_kw": {"required": False, "allow_null": True},
            "orden": {"required": False, "allow_null": True},
            "cantidad_strings": {"required": False, "allow_null": True},
            "estado": {"required": False},
        }


class EquipoSerializer(serializers.ModelSerializer):
    """Un equipo de la planta. El de tipo inversor trae sus inversores."""

    proyecto_id = serializers.IntegerField(read_only=True)
    tipo_etiqueta = serializers.CharField(source="get_tipo_display", read_only=True)
    inversores = serializers.SerializerMethodField()

    class Meta:
        model = py_models.Equipo
        fields = ["id", "proyecto_id", "tipo", "tipo_etiqueta", "cantidad", "marca", "potencia",
                  "detalle", "inversores", "created_at", "updated_at"]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
        }

    def get_inversores(self, obj) -> list | None:
        if obj.tipo != py_models.TipoEquipo.INVERSOR:
            return None
        filas = sorted(obj.inversores.all(), key=lambda i: (i.orden, i.id))
        return ProyectoInversorSerializer(filas, many=True).data



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

    # ALIAS DE TRANSICIÓN, se quita en el próximo despliegue. `potencia_ac_kw`
    # se llamaba `potencia_instalada_kwp` hasta el 2026-09-10 y el frontend lo
    # lee con el nombre viejo en 12 archivos. Entre el push del backend y el
    # promote del front hay una ventana en la que el front viejo habla con el
    # backend nuevo: sin este alias, la potencia sale en blanco en toda la
    # plataforma durante ese rato.
    potencia_instalada_kwp = serializers.DecimalField(
        source="potencia_ac_kw", max_digits=12, decimal_places=3,
        read_only=True, allow_null=True,
    )
    # Un proyecto está en un solo portafolio (Sara, 15-oct).
    portafolio_id = serializers.IntegerField(allow_null=True)
    portafolio_nombre = serializers.CharField(source="portafolio.nombre", read_only=True, default=None)
    operador_red_id = serializers.IntegerField(allow_null=True)
    operador_red_legal = serializers.SerializerMethodField()
    ppa_contratos = serializers.SerializerMethodField()
    inversionistas = ProyectoInversionistaSerializer(many=True, read_only=True)
    info_tecnica = serializers.SerializerMethodField()
    equipos = serializers.SerializerMethodField()
    inversores = ProyectoInversorSerializer(many=True, read_only=True)
    area_contactos = serializers.SerializerMethodField()
    servicios = serializers.SerializerMethodField()
    # DERIVADO, no guardado. `proyectos.es_comunidad_energetica` se elimino en
    # `proyectos/0008` porque era una segunda verdad: la marca se negocia en el
    # PPA y de ahi baja a la planta. La pantalla del proyecto seguia mostrando
    # este campo desde entonces, y siempre salia vacio porque nadie lo mandaba.
    es_comunidad_energetica = serializers.SerializerMethodField()

    class Meta:
        model = py_models.Proyecto
        fields = CAMPOS_CREACION + [
            "id", "potencia_instalada_kwp",   # alias de transición, ver arriba
            "operador_red_legal", "ppa_contratos", "inversionistas",
            "info_tecnica", "equipos", "inversores", "area_contactos", "servicios",
            "es_comunidad_energetica", "portafolio_nombre",
            "created_at", "updated_at",
        ]

    def get_servicios(self, obj) -> dict:
        """`{operacion, representacion, cgm, ppa}`, de los contratos vigentes.

        Reemplaza a las banderas `srv_*`, que se ponían a mano y nada sincronizaba
        con los contratos (`apps/contratos/services/plantas.py`). Solo lectura. Un
        listado pasa `servicios_por_proyecto` por contexto, calculado una vez para
        toda la página; el detalle lo calcula para su único proyecto.
        """
        from apps.contratos.services import plantas
        from apps.plataforma.services.fechas import hoy_col

        hoy = hoy_col()
        por_proyecto = self.context.get("servicios_por_proyecto")
        if por_proyecto is None:
            por_proyecto = plantas.servicios_por_proyecto(hoy)
        return plantas.resumen_servicios([obj], hoy, por_proyecto)[obj.id]

    def get_es_comunidad_energetica(self, obj) -> bool:
        """Si la planta esta hoy en una comunidad energetica, segun sus PPA.

        Lo resuelve `comunidades.plantas_en_comunidad`, que es la UNICA
        definicion de esto --la misma que decide si la planta recibe
        representacion y CGM--. Derivarlo aca de `obj.contratos_ppa` seria mas
        rapido pero crearia una segunda regla, y esa duplicacion es justo lo que
        el cambio de comunidades vino a quitar.

        El set se calcula UNA vez por serializacion y no por fila: en un listado
        de 200 plantas seria una consulta por cada una.
        """
        return obj.id in self._plantas_en_comunidad()

    def _plantas_en_comunidad(self) -> set:
        cache = self.context.get("_plantas_en_comunidad")
        if cache is None:
            from apps.contratos.services import comunidades as comunidades_service
            from apps.plataforma.services.fechas import hoy_col

            cache = comunidades_service.plantas_en_comunidad(hoy_col())
            self.context["_plantas_en_comunidad"] = cache
        return cache

    def get_operador_red_legal(self, obj) -> str | None:
        from apps.comercial.services.pipeline import operador_red_legal

        return operador_red_legal(obj)

    def get_ppa_contratos(self, obj) -> list:
        """La relación pasa por la tabla puente y no conoce el borrado lógico de
        `ppa_contratos`: los eliminados se filtran acá para que no reaparezcan.

        La tabla puente (`contrato_proyectos`) tiene también los contratos de
        servicio desde la rama de inversionistas: aquí solo van los PPA."""
        contratos = [
            v.contrato for v in obj.contratos_ppa.all()
            if v.contrato and v.contrato.deleted_at is None
            and v.contrato.grupo == GrupoContrato.PPA
        ]
        return PpaResumenSerializer(contratos, many=True).data

    def get_info_tecnica(self, obj):
        """La forma vieja (un objeto con la marca de cada tipo), armada desde
        `proyectos` y `equipos`: la ficha anterior y Comercial la leen así."""
        from apps.proyectos.services.equipos import ficha_tecnica

        return ficha_tecnica(obj)

    def get_equipos(self, obj) -> list:
        return EquipoSerializer(
            sorted(obj.equipos.all(), key=lambda e: e.tipo), many=True
        ).data

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
            "departamento", "potencia_ac_kw", "sub_project", "codigo_tsf",
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
