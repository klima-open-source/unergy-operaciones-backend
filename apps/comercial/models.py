"""Modelos del dominio `comercial`.

GENERADO por scripts/generar_modelos_django.py desde los metadatos de
SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los
TextChoices de las columnas de estado y los docstrings que explican el
modelo de datos. Revisar antes de portar la API del recurso.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.
"""

from django.db import models
from django.utils import timezone

from apps.plataforma.models import Timer

class Oportunidad(Timer):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey("clientes.Cliente", on_delete=models.DO_NOTHING, db_column="cliente_id", related_name="oportunidades_por_cliente_id")
    nombre = models.CharField(max_length=255, null=True, blank=True)
    estado = models.CharField(max_length=11, choices=[("oportunidad", "oportunidad"), ("oferta", "oferta"), ("contrato", "contrato"), ("firmado", "firmado"), ("operando", "operando"), ("terminado", "terminado"), ("declinado", "declinado")], default="oportunidad")
    estado_desde = models.DateTimeField(default=timezone.now)
    numero_oferta = models.CharField(max_length=100, null=True, blank=True)
    fecha_tentativa_inicio_representacion = models.DateField(null=True, blank=True)
    fecha_tentativa_inicio_compra_energia = models.DateField(null=True, blank=True)
    fecha_estimada_firma = models.DateField(null=True, blank=True)
    notas = models.TextField(null=True, blank=True)
    es_migrada = models.BooleanField(default=False)
    creado_por_usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="creado_por_usuario_id", null=True, blank=True, related_name="oportunidades_por_creado_por_usuario_id")
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "oportunidades"


class OportunidadEstadoHistorial(models.Model):
    id = models.BigAutoField(primary_key=True)
    oportunidad = models.ForeignKey("Oportunidad", on_delete=models.DO_NOTHING, db_column="oportunidad_id", related_name="oportunidad_estado_historial_por_oportunidad_id")
    oferta = models.ForeignKey("OportunidadOferta", on_delete=models.CASCADE, db_column="oferta_id", null=True, blank=True, related_name="oportunidad_estado_historial_por_oferta_id")
    estado_anterior = models.CharField(max_length=20, null=True, blank=True)
    estado_nuevo = models.CharField(max_length=20)
    usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="usuario_id", null=True, blank=True, related_name="oportunidad_estado_historial_por_usuario_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "oportunidad_estado_historial"


class OportunidadGestion(models.Model):
    id = models.BigAutoField(primary_key=True)
    oportunidad = models.ForeignKey("Oportunidad", on_delete=models.DO_NOTHING, db_column="oportunidad_id", related_name="gestiones")
    oferta = models.ForeignKey("OportunidadOferta", on_delete=models.SET_NULL, db_column="oferta_id", null=True, blank=True, related_name="oportunidad_gestiones_por_oferta_id")
    tipo = models.CharField(max_length=8, choices=[("llamada", "llamada"), ("correo", "correo"), ("reunion", "reunion"), ("whatsapp", "whatsapp"), ("nota", "nota")])
    descripcion = models.TextField()
    fecha = models.DateTimeField(default=timezone.now)
    # Quien habló: nosotros (saliente) o el cliente (entrante).
    #
    # **Es el campo del que depende que la alerta no mienta.** Sin él,
    # `calcular_alerta` toma la gestión MÁS RECIENTE de cualquier tipo, así que
    # insistirle al cliente el jueves reinicia el contador aunque siga sin decir
    # una palabra: la alerta contesta «hace cuánto que no pasa nada» cuando
    # debería contestar «hace cuánto que no nos responden».
    #
    # NULL es el legado: las gestiones anteriores a este campo no dicen quién
    # habló, y se siguen contando como antes para no provocar una avalancha de
    # alertas el día que esto se despliegue. Las nuevas sí lo declaran.
    #
    # Ver `docs/DOMINIO_COMERCIAL.md`, P-9. El mismo concepto ya existe en
    # `apps/comun/correos_hilo.py`, que distingue nuestros mensajes de los suyos
    # por el dominio `@unergy.io`.
    direccion = models.CharField(
        max_length=9, null=True, blank=True,
        choices=[("saliente", "saliente"), ("entrante", "entrante")],
    )
    usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="usuario_id", null=True, blank=True, related_name="oportunidad_gestiones_por_usuario_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "oportunidad_gestiones"


class OportunidadOferta(Timer):
    id = models.BigAutoField(primary_key=True)
    oportunidad = models.ForeignKey("Oportunidad", on_delete=models.DO_NOTHING, db_column="oportunidad_id", related_name="ofertas")
    tipo = models.CharField(max_length=23, choices=[("servicios_operacionales", "servicios_operacionales"), ("compra_energia", "compra_energia"), ("comunidad_energetica", "comunidad_energetica")])
    planta_nombre = models.CharField(max_length=255, null=True, blank=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="oportunidad_ofertas_por_proyecto_id")
    numero_oferta = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    precio_detalle = models.TextField(null=True, blank=True)
    estado = models.CharField(max_length=11, choices=[("oportunidad", "oportunidad"), ("oferta", "oferta"), ("contrato", "contrato"), ("firmado", "firmado"), ("operando", "operando"), ("terminado", "terminado"), ("declinado", "declinado")], default="oportunidad")
    estado_desde = models.DateTimeField(default=timezone.now)
    resultado = models.CharField(max_length=9, choices=[("pendiente", "pendiente"), ("aceptado", "aceptado"), ("declinado", "declinado")], default="pendiente")
    etapa_texto = models.CharField(max_length=60, null=True, blank=True)
    fecha_oferta = models.DateField(null=True, blank=True)
    fecha_tentativa_inicio = models.DateField(null=True, blank=True)
    fecha_fin_tentativa = models.DateField(null=True, blank=True)
    contrato_firmado = models.CharField(max_length=150, null=True, blank=True)
    # NO borrar sin mirar: aunque ningun servicio del backend lo interpreta, el
    # frontend SI lo pinta. `OfertasPanel.vue` arma con el la columna "Servicios
    # buscados" (`detalle.servicios`) y la linea FPO (`detalle.fpo`). Borrarlo
    # dejaria esa columna en blanco en produccion sin que nada falle.
    #
    # Es data del cargue masivo de Excel y esta CONGELADO: ningun flujo lo
    # escribe --ni el front ni la API-- asi que solo puede encoger. Por eso
    # tampoco viaja ya en los serializers de escritura: un blob sin esquema que
    # acepta escritura es como se llena de basura.
    #
    # Su forma de hecho es {"servicios": [...], "fpo": "..."}. Pendiente medir
    # contra produccion cuantas ofertas lo traen y que valores tiene `servicios`
    # antes de decidir si `fpo` merece ser columna y `servicios` alinearse con
    # los subservicios de `apps/contratos/services/grupos.py`.
    detalle = models.JSONField(null=True, blank=True)
    seguimientos = models.IntegerField(default=0)
    fecha_ultima_respuesta = models.DateField(null=True, blank=True)
    documento_url = models.CharField(max_length=1000, null=True, blank=True)
    ppa_contrato = models.ForeignKey("contratos.Contrato", on_delete=models.SET_NULL, db_column="ppa_contrato_id", null=True, blank=True, related_name="oportunidad_ofertas_por_ppa_contrato_id")
    contrato_servicio = models.ForeignKey("contratos.Contrato", on_delete=models.SET_NULL, db_column="contrato_servicio_id", null=True, blank=True, related_name="oportunidad_ofertas_por_contrato_servicio_id")
    municipio = models.CharField(max_length=100, null=True, blank=True)
    departamento = models.CharField(max_length=100, null=True, blank=True)
    operador_red = models.ForeignKey("fronteras.OperadorRed", on_delete=models.DO_NOTHING, db_column="operador_red_id", null=True, blank=True, related_name="oportunidad_ofertas_por_operador_red_id")
    energia_promedio_kwh_mes = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    notas = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "oportunidad_ofertas"


class OportunidadOfertaProyecto(models.Model):
    oferta = models.ForeignKey("OportunidadOferta", on_delete=models.CASCADE, db_column="oferta_id", related_name="proyectos_declarados")
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="oportunidad_oferta_proyectos_por_proyecto_id")
    pk = models.CompositePrimaryKey("oferta_id", "proyecto_id")

    class Meta:
        db_table = "oportunidad_oferta_proyectos"


class OportunidadOfertaVersion(Timer):
    """Una propuesta concreta de una oferta. APPEND-ONLY: no se editan.

    **Reofertar no crea otra oferta: le agrega una versión.** La oferta conserva
    su identidad —el consecutivo NUNCA cambia— y lo que varía son las
    condiciones de cada propuesta. Antes había un solo `documento_url` y un
    `precio_detalle` de texto que se sobrescribían: reofertar borraba la
    propuesta anterior sin rastro. Ver `docs/DOMINIO_COMERCIAL.md`, O-8 y O-9.

    Es el mismo patrón que la bitácora de Prospección: un registro inmutable del
    que se derivan los estados. Por eso no hay ni `PATCH` ni `DELETE` sobre una
    versión; corregir es agregar la siguiente.

    **`fecha_aceptacion` es la pieza que conecta con el contrato.** Responde cuál
    de las tres propuestas fue la que se firmó, y de ahí salen las condiciones
    con las que nace el PPA. Sin ella el contrato no sabría con qué precio nacer.

    Lo que guarda es lo que el CONTRATO necesita, no todo lo que dice el PDF de
    la oferta. La modalidad, las garantías y la forma de pago son idénticas en
    todas las ofertas revisadas y nada en la plataforma ramifica por ellas: viven
    en el documento, que está en Drive.
    """

    id = models.BigAutoField(primary_key=True)
    oferta = models.ForeignKey(
        "OportunidadOferta", on_delete=models.CASCADE, db_column="oferta_id",
        related_name="versiones",
    )
    # 1, 2, 3… por oferta. Lo asigna el servicio, no el cliente.
    numero = models.IntegerField()
    # Sin fecha de envío la versión es un BORRADOR: se está preparando.
    fecha_envio = models.DateField(null=True, blank=True)
    # La versión que el cliente aceptó. Como mucho una por oferta.
    fecha_aceptacion = models.DateField(null=True, blank=True)
    documento_url = models.CharField(max_length=1000, null=True, blank=True)
    # Las dos condiciones de indexación que el contrato necesita y la oferta no
    # guardaba en ninguna parte.
    indice_indexacion = models.CharField(max_length=50, null=True, blank=True)
    # Mes base en YYYY-MM, como lo guarda `ppa_contratos`. En el PDF es la fila
    # "Precio Base": los precios son pesos constantes de ese mes.
    periodo_indexacion_base = models.CharField(max_length=7, null=True, blank=True)
    que_cambio = models.TextField(null=True, blank=True)
    creado_por_usuario = models.ForeignKey(
        "plataforma.Usuario", on_delete=models.DO_NOTHING,
        db_column="creado_por_usuario_id", null=True, blank=True,
        related_name="oferta_versiones_creadas",
    )

    class Meta:
        db_table = "oportunidad_oferta_versiones"
        unique_together = [("oferta", "numero")]


class OportunidadOfertaVersionPrecio(models.Model):
    """El precio por año de una versión. Es la tabla 2 del PDF de la oferta.

    En tabla aparte y no en un JSON por dos razones: es la misma forma que
    `ppa_tarifas` —a donde van estos precios al firmar, expandidos a filas
    mensuales recortadas al período— y así el año único lo garantiza la base en
    vez de el código.
    """

    id = models.BigAutoField(primary_key=True)
    version = models.ForeignKey(
        "OportunidadOfertaVersion", on_delete=models.CASCADE,
        db_column="version_id", related_name="precios",
    )
    anio = models.IntegerField()
    precio = models.DecimalField(max_digits=12, decimal_places=4)

    class Meta:
        db_table = "oportunidad_oferta_version_precios"
        unique_together = [("version", "anio")]
