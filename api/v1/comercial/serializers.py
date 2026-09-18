"""Serializers del CRM comercial — espejo de `app/schemas/comercial.py`.

Las salidas las arma `apps/comercial/services/salidas.py`: son fichas resueltas
por cascada, no filas de una tabla. Acá solo entra lo que ESCRIBE.
"""

from rest_framework import serializers

ESTADOS = [
    "oportunidad", "oferta", "contrato", "firmado", "operando", "terminado", "declinado",
]
TIPOS_OFERTA = ["servicios_operacionales", "compra_energia", "comunidad_energetica"]
TIPOS_GESTION = ["llamada", "correo", "reunion", "whatsapp", "nota"]
TIPOS_CONTACTO = ["liquidacion", "operacional", "comercial", "cgm", "contable"]
ORIGENES_CLIENTE = ["prospeccion_propia", "recomendacion", "referido", "otro"]


class ContactoNuevoSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    telefono = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    email = serializers.EmailField(min_length=3)
    tipo = serializers.ChoiceField(choices=TIPOS_CONTACTO, default="comercial")

    def validate_email(self, v):
        return v.strip().lower()


class ClienteNuevoSerializer(serializers.Serializer):
    razon_social_nombre = serializers.CharField(min_length=1)
    nit_cedula = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    origen_tipo = serializers.ChoiceField(choices=ORIGENES_CLIENTE, required=False, allow_null=True, default=None)
    origen_detalle = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    contactos = ContactoNuevoSerializer(many=True, allow_empty=False)


class _UnSoloCliente(serializers.Serializer):
    """Exactamente uno de `cliente_id` (existente) o `cliente_nuevo`."""

    cliente_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    cliente_nuevo = ClienteNuevoSerializer(required=False, allow_null=True, default=None)
    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    notas = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    forzar_cliente_duplicado = serializers.BooleanField(default=False)

    def validate(self, datos):
        if bool(datos.get("cliente_id")) == bool(datos.get("cliente_nuevo")):
            raise serializers.ValidationError(
                "Envía cliente_id O cliente_nuevo (exactamente uno)"
            )
        return datos


class OportunidadCrearSerializer(_UnSoloCliente):
    pass


class OportunidadActualizarSerializer(serializers.Serializer):
    """`estado` NO es editable por PATCH: se mueve con POST /{id}/estado."""

    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    numero_oferta = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    fecha_tentativa_inicio_representacion = serializers.DateField(required=False, allow_null=True)
    fecha_tentativa_inicio_compra_energia = serializers.DateField(required=False, allow_null=True)
    fecha_estimada_firma = serializers.DateField(required=False, allow_null=True)
    notas = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class OfertaCrearSerializer(serializers.Serializer):
    tipo = serializers.ChoiceField(choices=TIPOS_OFERTA)
    planta_nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    # Las plantas de la oferta (M2M). Una oferta puede cubrir varias ("Balmora 1
    # y 2"); es lo que /firmar pasa al contrato. La primera se copia también a
    # `proyecto_id`, que es lo que siguen leyendo el vinculador y la ficha.
    proyecto_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_null=True, default=None,
    )
    numero_oferta = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    precio_detalle = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    # `resultado` no se envía: se deriva de `estado`.
    estado = serializers.ChoiceField(choices=ESTADOS, default="oportunidad")
    etapa_texto = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    fecha_oferta = serializers.DateField(required=False, allow_null=True, default=None)
    fecha_tentativa_inicio = serializers.DateField(required=False, allow_null=True, default=None)
    fecha_fin_tentativa = serializers.DateField(required=False, allow_null=True, default=None)
    contrato_firmado = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    documento_url = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    # Ficha operativa DECLARADA: solo aplica cuando la planta no existe como
    # Proyecto. Si lo tiene, manda el Proyecto (ver `ficha_operativa`).
    municipio = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    departamento = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    operador_red_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    energia_promedio_kwh_mes = serializers.FloatField(required=False, allow_null=True, min_value=0, default=None)
    notas = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)


class OfertaActualizarSerializer(serializers.Serializer):
    """PATCH parcial: solo viaja lo que el cliente mandó.

    `estado` no está: se mueve con POST /ofertas/{id}/estado, que deja histórico.
    `seguimientos` es editable para corregir un conteo mal importado, no para
    reemplazar a POST /ofertas/{id}/seguimiento.
    """

    tipo = serializers.ChoiceField(choices=TIPOS_OFERTA, required=False)
    planta_nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True)
    # Lista vacía = desvincular todas las plantas.
    proyecto_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_null=True)
    numero_oferta = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    precio_detalle = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    etapa_texto = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    fecha_oferta = serializers.DateField(required=False, allow_null=True)
    fecha_tentativa_inicio = serializers.DateField(required=False, allow_null=True)
    fecha_fin_tentativa = serializers.DateField(required=False, allow_null=True)
    contrato_firmado = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    fecha_ultima_respuesta = serializers.DateField(required=False, allow_null=True)
    seguimientos = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    documento_url = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    # El PPA lo enlaza /firmar solo; los contratos de representación se crean por
    # su propio wizard, así que este enlace tiene que poder hacerse a mano o la
    # oferta queda huérfana.
    contrato_servicio_id = serializers.IntegerField(required=False, allow_null=True)
    municipio = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    departamento = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    operador_red_id = serializers.IntegerField(required=False, allow_null=True)
    energia_promedio_kwh_mes = serializers.FloatField(required=False, allow_null=True, min_value=0)
    notas = serializers.CharField(required=False, allow_null=True, allow_blank=True)


class EstadoCambioSerializer(serializers.Serializer):
    estado = serializers.ChoiceField(choices=ESTADOS)


class GestionCrearSerializer(serializers.Serializer):
    tipo = serializers.ChoiceField(choices=TIPOS_GESTION)
    descripcion = serializers.CharField(min_length=1)
    fecha = serializers.DateTimeField(required=False, allow_null=True, default=None)
    # A cuál oferta se refiere. NULL = gestión DEL CLIENTE: cuenta para todas sus
    # ofertas, que es como se comportaban todas antes de 2026-08-19.
    oferta_id = serializers.IntegerField(required=False, allow_null=True, default=None)


class RegistroComercialSerializer(_UnSoloCliente):
    """Registro completo en UNA transacción: cliente + oportunidad + ofertas.

    `ofertas` exige al menos una: una oportunidad sin ofertas es INVISIBLE en
    toda la aplicación, porque el tablero y la tabla se alimentan de las ofertas.
    """

    ofertas = OfertaCrearSerializer(many=True, allow_empty=False)


class PrecioAnualSerializer(serializers.Serializer):
    """Una fila de la tabla de precios: año de suministro y $COP/kWh. Al firmar se
    expande a 12 filas de `ppa_tarifas`."""

    anio = serializers.IntegerField(min_value=2000, max_value=2100)
    precio = serializers.FloatField(min_value=0.000001)


class VersionOfertaSerializer(serializers.Serializer):
    """Lectura de una versión. La tabla de precios viaja adentro.

    No hay serializer de edición: las versiones son append-only. Corregir una
    propuesta es agregar la siguiente, igual que la bitácora de Prospección.
    """

    id = serializers.IntegerField(read_only=True)
    numero = serializers.IntegerField(read_only=True)
    fecha_envio = serializers.DateField(allow_null=True)
    fecha_aceptacion = serializers.DateField(allow_null=True)
    documento_url = serializers.CharField(allow_null=True)
    indice_indexacion = serializers.CharField(allow_null=True)
    periodo_indexacion_base = serializers.CharField(allow_null=True)
    que_cambio = serializers.CharField(allow_null=True)
    creado_por_usuario_id = serializers.IntegerField(allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    precios = serializers.SerializerMethodField()

    def get_precios(self, obj) -> list:
        return [
            {"anio": p.anio, "precio": float(p.precio)}
            for p in sorted(obj.precios.all(), key=lambda x: x.anio)
        ]


class VersionOfertaCrearSerializer(serializers.Serializer):
    """`POST /comercial/ofertas/{id}/versiones`.

    `numero` NO se recibe: es el orden de la propuesta dentro de su oferta, no
    un dato que alguien elija. Lo asigna el servicio.

    Sin `fecha_envio` la versión es un BORRADOR: se está preparando y todavía no
    salió. Por eso un borrador no se puede aceptar.
    """

    fecha_envio = serializers.DateField(required=False, allow_null=True, default=None)
    documento_url = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None
    )
    indice_indexacion = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None
    )
    # Mes base en YYYY-MM, como lo guarda `ppa_contratos`. En el PDF de la oferta
    # es la fila "Precio Base": los precios son pesos constantes de ese mes.
    periodo_indexacion_base = serializers.RegexField(
        r"^\d{4}-(0[1-9]|1[0-2])$", required=False, allow_null=True, default=None,
    )
    que_cambio = serializers.CharField(
        required=False, allow_null=True, allow_blank=True, default=None
    )
    # La tabla 2 del PDF. Misma forma que en la firma: al crear el contrato se
    # expande a filas mensuales de `ppa_tarifas`.
    precios = PrecioAnualSerializer(many=True, required=False, default=list)


class AceptarVersionSerializer(serializers.Serializer):
    """Cuándo el cliente aceptó esa propuesta. Es la que se firmará."""

    fecha_aceptacion = serializers.DateField()


class FirmarOfertaSerializer(serializers.Serializer):
    """Convierte una oferta aceptada en su contrato y la mueve a 'firmado'.

    Las condiciones NO se guardan en la oferta: alimentan el contrato PPA, que es
    donde ya viven y donde las leen Cumplimiento y Liquidaciones. La oferta solo
    se queda con el enlace.
    """

    numero_codigo_contrato = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    nombre_interno = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    fecha_inicio = serializers.DateField()
    fecha_fin = serializers.DateField()
    # Tarifa única cuando no hay tabla por año (Bayunca: 300 $/kWh planos).
    tarifa_base = serializers.FloatField(required=False, allow_null=True, min_value=0.000001, default=None)
    precios_anuales = PrecioAnualSerializer(many=True, required=False, allow_null=True, default=None)
    indice_indexacion = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    # Mes base de indexación en YYYY-MM, como lo guarda `ppa_contratos`.
    periodo_indexacion_base = serializers.RegexField(
        r"^\d{4}-(0[1-9]|1[0-2])$", required=False, allow_null=True, default=None,
    )
    cantidad_minima_kwh_mes = serializers.FloatField(required=False, allow_null=True, min_value=0, default=None)
    carpeta_link = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, datos):
        if datos["fecha_fin"] < datos["fecha_inicio"]:
            raise serializers.ValidationError(
                "fecha_fin no puede ser anterior a fecha_inicio"
            )
        # El precio ya NO se exige acá: puede venir de la propuesta aceptada, y
        # este serializer no ve la oferta. La regla "el contrato necesita un
        # precio" vive en `escritura.firmar`, despues de mezclar las dos fuentes.
        precios = datos.get("precios_anuales") or []
        anios = [p["anio"] for p in precios]
        if len(anios) != len(set(anios)):
            raise serializers.ValidationError("la tabla de precios tiene años repetidos")
        return datos
