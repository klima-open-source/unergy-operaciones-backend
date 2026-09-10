"""Serializers de Clientes.

Espejo de `app/schemas/clientes.py`. Las salidas compuestas (panel 360, vista
comercial, servicios-contratos) las arma el servicio como dict y salen tal cual:
son agregados, no filas de una tabla.

El correo se normaliza a minúsculas y sin espacios ANTES de validar, igual que
el `field_validator` de Pydantic — si no, el UNIQUE de `(cliente_id, email,
tipo)` deja pasar "Juan@X.com" y "juan@x.com" como dos contactos distintos.
"""

import re

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.clientes import models as cl_models
from apps.clientes.services.gestion import normalizar_nit


class _EmailNormalizado(serializers.EmailField):
    def to_internal_value(self, data):
        return super().to_internal_value((data or "").strip().lower())


class _NitNormalizado(serializers.CharField):
    """El NIT, solo con sus dígitos.

    El UNIQUE de la base compara TEXTO, así que "900.123.456-7",
    "900123456-7" y "9001234567" eran tres valores distintos: el mismo cliente
    podía entrar tres veces sin que nada avisara. Guardar siempre los dígitos
    hace que las tres escrituras choquen entre sí.

    **Lo que esto NO resuelve:** "900123456" (sin el dígito de verificación) y
    "9001234567" (con él) siguen siendo distintos. No se puede decidir sin
    adivinar — el último dígito de una cédula de 10 cifras es parte del número,
    no un verificador —, y adivinar acá significa rechazar un cliente legítimo.
    """

    def run_validation(self, data=serializers.empty):
        """Normaliza ANTES de validar, y traduce el vacío a None.

        Dos razones para hacerlo acá y no solo en `to_internal_value`:

        - `CharField.run_validation` devuelve "" tal cual --sin pasar por
          `to_internal_value` ni por los validadores-- cuando `allow_blank` está
          puesto. Y en Postgres "" SÍ colisiona con otra "" (a diferencia de
          NULL), así que guardarla haría chocar al segundo cliente sin NIT.
        - " - . " tampoco es un NIT: normalizado no queda nada. Si eso llegara a
          los validadores de longitud como `None`, revienta con un TypeError
          (`len(None)`), que sale como 500 en vez de como "falta el NIT".
        """
        if data is not serializers.empty and data is not None:
            data = normalizar_nit(str(data))
        if data in (None, ""):
            return None
        return super().run_validation(data)

    def to_internal_value(self, data):
        # Vacío es "sin NIT", no un NIT que sea la cadena vacía.
        return normalizar_nit(super().to_internal_value(data))


class _TextoLimpio(serializers.CharField):
    """Sin espacios al borde y sin espacios dobles adentro.

    " Quantum  Energy " y "Quantum Energy" son el mismo cliente escrito con la
    mano temblorosa, y sin esto entraban como dos filas: el aviso de nombre
    parecido las habría marcado, pero es un aviso y se puede saltar. Limpiar
    antes de comparar hace que el UNIQUE y la búsqueda de duplicados vean lo
    mismo que ve una persona.
    """

    def to_internal_value(self, data):
        return re.sub(r"\s+", " ", super().to_internal_value(data) or "").strip()


class _UnicoSiVieneDato(UniqueValidator):
    """Igual que `UniqueValidator`, pero un valor vacío no choca con nada.

    Sin esto, un cliente sin NIT consultaba `nit_cedula IS NULL` y el segundo
    cliente sin NIT se rechazaba como duplicado. En Postgres los NULL no
    colisionan entre sí y la mayoría de los clientes no tienen NIT cargado, así
    que habría bloqueado el alta más común. (`UniqueTogetherValidator`, el que
    DRF armaba solo, ya saltaba los None; al pasar al validador por campo hay
    que replicarlo a mano.)
    """

    def __call__(self, value, serializer_field):
        if value in (None, ""):
            return
        super().__call__(value, serializer_field)


class ContactoParaClienteSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    telefono = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
    email = _EmailNormalizado()
    # Las choices estaban solo en el modelo, y este no es un ModelSerializer:
    # entraba cualquier cadena de <=11 caracteres (y con mas, un 500 al escribir
    # sobre un varchar(11)). Los cinco valores son los de `Contacto.tipo`.
    tipo = serializers.ChoiceField(
        choices=[c[0] for c in cl_models.Contacto._meta.get_field("tipo").choices],
        default="comercial",
    )


class ClienteSerializer(serializers.ModelSerializer):
    """Salida del detalle: incluye los documentos comerciales."""

    documentos_comerciales = serializers.SerializerMethodField()

    class Meta:
        model = cl_models.Cliente
        fields = [
            "id", "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "created_at", "updated_at", "origen_tipo", "origen_detalle",
            "documentos_comerciales",
        ]

    def get_documentos_comerciales(self, obj):
        return ClienteDocumentoSerializer(obj.documentos_comerciales.all(), many=True).data


class ClienteListSerializer(serializers.ModelSerializer):
    """Salida del listado: sin documentos, que en una lista de 500 son 500 consultas."""

    class Meta:
        model = cl_models.Cliente
        fields = [
            "id", "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "created_at", "updated_at",
        ]


class ClienteEntradaSerializer(serializers.ModelSerializer):
    """POST y PATCH. `contactos` solo se lee al crear."""

    contactos = ContactoParaClienteSerializer(many=True, required=False, default=list)
    # `nit_cedula` es UNIQUE en la base (`unique_together = [("nit_cedula",)]`),
    # y de ahi DRF armaba solo un `UniqueTogetherValidator` que responde
    # `{"non_field_errors": ["Los campos nit_cedula deben formar un conjunto
    # único."]}`. Es correcto pero ilegible, y sustituyo sin quererlo al mensaje
    # que daba FastAPI ("Ya existe un cliente con ese NIT/cédula.", un 409 con
    # `detail` de texto): al portar, la explicacion que el usuario entendia se
    # perdio. El validador por campo devuelve el mismo error con el texto de
    # antes, y sigue cubriendo el PATCH -- que por este camino nunca llega a
    # tocar la base, asi que no puede reventar con un 500.
    razon_social_nombre = _TextoLimpio(max_length=255)
    representante_legal = _TextoLimpio(
        max_length=255, required=False, allow_null=True, allow_blank=True)
    direccion = _TextoLimpio(
        max_length=500, required=False, allow_null=True, allow_blank=True)
    ciudad = _TextoLimpio(
        max_length=100, required=False, allow_null=True, allow_blank=True)
    departamento = _TextoLimpio(
        max_length=100, required=False, allow_null=True, allow_blank=True)
    nit_cedula = _NitNormalizado(
        max_length=20, required=False, allow_null=True, allow_blank=True,
        validators=[_UnicoSiVieneDato(
            # `.all()` y no los vivos: el UNIQUE de la base tampoco sabe de
            # borrado lógico, así que un cliente eliminado sigue ocupando su
            # NIT. Filtrar acá daría un mensaje de "ya existe" que la vista de
            # clientes no puede explicar, y un 500 al escribir.
            queryset=cl_models.Cliente.objects.all(),
            message="Ya existe un cliente con ese NIT/cédula.",
        )],
    )

    class Meta:
        model = cl_models.Cliente
        fields = [
            "razon_social_nombre", "nit_cedula", "tipo_persona",
            "representante_legal", "direccion", "ciudad", "departamento",
            "iva_pct", "retencion_pct", "reteica_pct", "reteiva_pct",
            "origen_tipo", "origen_detalle", "contactos",
        ]
        extra_kwargs = {c: {"required": False} for c in fields[1:]}
        # Sin esto DRF vuelve a agregar el `UniqueTogetherValidator` de
        # `(nit_cedula,)` y el error saldria dos veces, una en jerga: el
        # validador del campo (arriba) ya cubre ese UNIQUE. Es el unico
        # `unique_together` del modelo.
        validators = []

    # Un cliente NO se guarda sin estos dos, ni al crear ni al editar. El NIT es
    # su unica identidad real: mientras fue opcional, lo unico que habia para
    # detectar duplicados era el nombre, que es justo lo que se escribe de diez
    # formas distintas.
    OBLIGATORIOS = {
        "razon_social_nombre": "La razón social / nombre",
        "nit_cedula": "El NIT / cédula",
    }

    def validate(self, datos):
        """Exige `OBLIGATORIOS` en POST **y** en PATCH.

        `partial=True` vuelve opcional TODO por diseño, asi que la
        obligatoriedad de un PATCH hay que expresarla acá. La regla es "no se
        puede guardar un cliente sin estos dos", no "el payload tiene que
        traerlos":

          - un PATCH que no menciona el campo hereda el valor que el cliente ya
            tiene, asi que guardar solo la ciudad sigue funcionando;
          - un PATCH que lo manda vacío se rechaza (no se puede borrar);
          - y un cliente que HOY no tiene NIT no se puede guardar hasta que se
            le cargue -- que es el punto: los que ya están sin NIT se completan
            la próxima vez que alguien los toque.
        """
        errores = {}
        for campo, etiqueta in self.OBLIGATORIOS.items():
            if campo in datos:
                valor = datos[campo]
            elif self.partial:
                valor = getattr(self.instance, campo, None)
            else:
                valor = None
            if not str(valor or "").strip():
                errores[campo] = [f"{etiqueta} es obligatorio."]
        if errores:
            raise serializers.ValidationError(errores)
        return datos
        # Sin esto DRF vuelve a agregar el `UniqueTogetherValidator` de
        # `(nit_cedula,)` y el error saldria dos veces, una en jerga: el
        # validador del campo (arriba) ya cubre ese UNIQUE. Es el unico
        # `unique_together` del modelo.
        validators = []


class TasaServicioSerializer(serializers.ModelSerializer):
    cliente_id = serializers.IntegerField(read_only=True)
    proyecto_id = serializers.IntegerField(required=False, allow_null=True, default=None)

    class Meta:
        model = cl_models.ClienteTasaServicio
        fields = [
            "id", "cliente_id", "servicio", "proyecto_id",
            "iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "iva_pct": {"required": False, "allow_null": True},
            "retencion_pct": {"required": False, "allow_null": True},
            "reteiva_pct": {"required": False, "allow_null": True},
            "reteica_pct": {"required": False, "allow_null": True},
        }


class ContactoSerializer(serializers.ModelSerializer):
    cliente_id = serializers.IntegerField(read_only=True)
    email = _EmailNormalizado()

    class Meta:
        model = cl_models.Contacto
        fields = [
            "id", "cliente_id", "nombre", "email", "telefono", "tipo",
            "recibe_notificaciones", "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "recibe_notificaciones": {"required": False},
        }


class ClienteDocumentoSerializer(serializers.ModelSerializer):
    # Nullable desde la generalización (revisión 122): un documento puede
    # pertenecer a un ContratoServicio o a un PpaContrato en vez de a un Cliente.
    cliente_id = serializers.IntegerField(read_only=True)
    contrato_servicio_id = serializers.IntegerField(read_only=True)
    ppa_contrato_id = serializers.IntegerField(read_only=True)
    oportunidad_id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = cl_models.ClienteDocumentoComercial
        fields = [
            "id", "cliente_id", "contrato_servicio_id", "ppa_contrato_id",
            "tipo", "nombre", "numero", "fecha", "estado",
            "archivo_url", "archivo_nombre", "notas", "oportunidad_id",
            "created_at", "updated_at",
        ]
        extra_kwargs = {
            "id": {"read_only": True},
            "created_at": {"read_only": True},
            "updated_at": {"read_only": True},
            "numero": {"required": False, "allow_null": True},
            "fecha": {"required": False, "allow_null": True},
            "estado": {"required": False},
            "archivo_url": {"required": False, "allow_null": True},
            "archivo_nombre": {"required": False, "allow_null": True},
            "notas": {"required": False, "allow_null": True},
        }
