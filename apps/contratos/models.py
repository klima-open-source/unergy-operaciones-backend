"""Modelos del dominio `contratos`.

GENERADO por scripts/generar_modelos_django.py desde los metadatos de
SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los
TextChoices de las columnas de estado y los docstrings que explican el
modelo de datos. Revisar antes de portar la API del recurso.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import Q, F

from apps.plataforma.models import Timer

class ContratoServicio(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="contratos_servicio_por_proyecto_id")
    numero_contrato = models.CharField(max_length=100, null=True, blank=True)
    servicio_aplica = models.CharField(max_length=14, choices=[("representacion", "representacion"), ("cgm", "cgm"), ("mantenimiento", "mantenimiento"), ("arriendo", "arriendo"), ("internet", "internet")])
    contratante_nombre = models.CharField(max_length=255, null=True, blank=True)
    contratante_nit = models.CharField(max_length=20, null=True, blank=True)
    prestador_nombre = models.CharField(max_length=255, null=True, blank=True)
    prestador_nit = models.CharField(max_length=20, null=True, blank=True)
    contratante = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="contratante_id", null=True, blank=True, related_name="contratos_servicio_por_contratante_id")
    prestador = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="prestador_id", null=True, blank=True, related_name="contratos_servicio_por_prestador_id")
    inversionista = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="inversionista_id", null=True, blank=True, related_name="contratos_servicio_por_inversionista_id")
    cgm_codigo_sic = models.CharField(max_length=20, null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    periodicidad_pago = models.CharField(max_length=10, choices=[("mensual", "mensual"), ("bimestral", "bimestral"), ("trimestral", "trimestral"), ("semestral", "semestral"), ("anual", "anual")], null=True, blank=True)
    indice_indexacion = models.CharField(max_length=50, null=True, blank=True)
    # Solo lo que una persona decide. `vigente` y `vencido` salieron de acá: eran
    # consecuencia de `fecha_fin` y nadie los actualizaba -- 8 contratos decían
    # `vigente` con la fecha pasada. Esa mitad la calcula
    # `apps.contratos.services.vigencia`, que no se puede desactualizar porque
    # no se guarda. Ver la migración 0006.
    estado = models.CharField(max_length=13, choices=[("firmado", "firmado"), ("en_renovacion", "en_renovacion"), ("terminado", "terminado")], default="firmado")
    fecha_firma_contrato = models.DateField(null=True, blank=True)
    fecha_inicio_om = models.DateField(null=True, blank=True)
    renovacion_automatica = models.BooleanField(null=True, blank=True)
    fecha_indexacion = models.DateField(null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    estado_pago = models.CharField(max_length=20, null=True, blank=True)
    plan_datos_gb = models.CharField(max_length=50, null=True, blank=True)
    velocidad_mbps = models.IntegerField(null=True, blank=True)
    tipo_conexion = models.CharField(max_length=50, null=True, blank=True)
    linea_servicio = models.CharField(max_length=100, null=True, blank=True)
    id_router = models.CharField(max_length=100, null=True, blank=True)
    numero_kit = models.CharField(max_length=100, null=True, blank=True)
    latencia_ms = models.IntegerField(null=True, blank=True)
    wifi_seguridad = models.CharField(max_length=50, null=True, blank=True)
    wifi_password = models.CharField(max_length=100, null=True, blank=True)
    ubicacion_lat = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    ubicacion_lng = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_mensual = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    indexacion_anual = models.JSONField(null=True, blank=True)
    indexacion_mensual = models.JSONField(null=True, blank=True)
    inversionista_nombre = models.CharField(max_length=255, null=True, blank=True)
    portafolio = models.CharField(max_length=255, null=True, blank=True)
    codigo_sun_factory = models.CharField(max_length=50, null=True, blank=True)
    nombre_proyecto_ref = models.CharField(max_length=255, null=True, blank=True)
    tarifa_admin = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    tarifa_cgm = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_representacion = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    indexacion_cgm = models.JSONField(null=True, blank=True)
    indexacion_representacion = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "contratos_servicio"


class Poliza(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="polizas")
    numero_poliza = models.CharField(max_length=100, null=True, blank=True)
    poliza_om = models.BooleanField(default=False)
    fecha_vencimiento = models.DateField(null=True, blank=True, db_index=True)
    valor_poliza = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    mano_obra = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    estructura = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    paneles = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    inversores = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    otros = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_total_proyecto = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    link_estudio_suelos = models.CharField(max_length=500, null=True, blank=True)
    ipp_base = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ipp_base_fecha = models.DateField(null=True, blank=True)
    ipp_provisional = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ipp_provisional_fecha = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    generacion_anual_p90_kwh = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_lucro_cesante = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "polizas"


class AlertaAniversario(Timer):
    """Libro de avisos de aniversario ya enviados, uno por ventana.

    NO es una tabla generada: nace acá (2026-09-10) para que
    `contratos.alertas_representacion` sea idempotente y tolere corridas
    perdidas. Antes el disparo era coincidencia exacta (`dias in (30, 15)`), sin
    registro de nada: una corrida perdida perdía el aviso para siempre y dos
    corridas el mismo día mandaban dos correos.

    La existencia de la fila ES la marca de "ya avisé"; no hay columna de
    estado. Se escribe DESPUÉS de que el correo salió bien, así que un SMTP
    caído no consume el aviso: la ventana sigue cruzada y la corrida siguiente
    reintenta.

    `aniversario` va en la llave, y no basta `(contrato, dias_aviso)` como en
    `alertas` (la de PPA): un PPA tiene una sola `fecha_fin`, pero un contrato
    de representación cumple aniversario todos los años. Sin la fecha en la
    llave, el aviso saldría una única vez en la vida del contrato.
    """

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.ContratoServicio", on_delete=models.CASCADE,
        db_column="contrato_id", related_name="alertas_aniversario",
    )
    # La fecha concreta del aniversario avisado, no el año: es la que distingue
    # un aniversario del siguiente.
    aniversario = models.DateField()
    # El umbral cruzado (30 o 15), no los días reales que faltaban: es lo que
    # identifica la ventana. Los días reales van en el texto del correo.
    dias_aviso = models.IntegerField()

    class Meta:
        db_table = "contrato_alertas_aniversario"
        unique_together = [("contrato", "aniversario", "dias_aviso")]


# =====================================================================================
# Contratos unificados (D-10) + tarifas versionadas (D-24)
#
# Aterrizaje en Django del diseño de `docs/refactor/` (03-esquema.sql BLOQUE 8). Una
# sola tabla de contratos con el `tipo` como columna, roles en tabla puente, plantas en
# N:M, y las tarifas por concepto CON VIGENCIA — porque las tarifas se renegocian e
# indexan cada año y hoy ese histórico se pierde al pisar el escalar (bug de liquidación
# confirmado con negocio, D-24).
#
# FASE ADITIVA: estas tablas conviven con `ppa_contratos`, `contratos_servicio` y
# `ppa_tarifas`, que SIGUEN siendo la fuente de verdad de los lectores actuales
# (facturación, contabilidad, O&M, comercial). Se llenan por backfill; todavía no se
# cablean a esos consumidores.
#
# Divergencias deliberadas frente a 03-esquema.sql, documentadas donde aplican:
# - Los enums nativos del DDL se modelan como CharField(choices), que es la convención
#   del repo (Usuario.rol, ContratoServicio.servicio_aplica): el tipo físico es varchar,
#   los valores y la validación son idénticos, y btree_gist soporta `concepto` varchar en
#   el EXCLUDE sin necesidad de un tipo enum.
# - No hay columna `es_base`: la fila base se expresa con `origen='pactada'` (el DDL no
#   tiene es_base; la prosa de mapeo §F que lo menciona está desactualizada).
# =====================================================================================


class PgCheckConstraint(models.CheckConstraint):
    """`CheckConstraint` que se salta cuando el backend NO es PostgreSQL.

    Para el CHECK `NOT isempty(vigencia)`: `isempty` es una función de rangos de
    Postgres, y SQLite (el backend de las pruebas, ver `PgExclusionConstraint`) falla
    al crear la tabla con «no such function: isempty». En Postgres se crea igual."""

    def _solo_postgres(self, schema_editor):
        return schema_editor.connection.vendor == "postgresql"

    def constraint_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().constraint_sql(model, schema_editor)

    def create_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().create_sql(model, schema_editor)

    def remove_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().remove_sql(model, schema_editor)


class PgExclusionConstraint(ExclusionConstraint):
    """`ExclusionConstraint` que se salta cuando el backend NO es PostgreSQL.

    El `EXCLUDE USING gist` es válido solo en Postgres (producción). La suite de
    pruebas construye el esquema en SQLite en memoria a partir del ESTADO de los
    modelos (con las migraciones deshabilitadas, `MIGRATION_MODULES = {…: None}`),
    y el editor de esquema de SQLite emitiría el `EXCLUDE` en el `CREATE TABLE` y
    fallaría con «near "EXCLUDE": syntax error». Devolver None en un backend que no
    es Postgres deja la constraint fuera del DDL de SQLite sin sacarla del modelo:
    sigue siendo la fuente de verdad, `makemigrations` la ve, y en Postgres se crea
    igual. (Django no trae este guard: `create_sql`/`constraint_sql` emiten el
    EXCLUDE sin mirar el vendor.)"""

    def _solo_postgres(self, schema_editor):
        return schema_editor.connection.vendor == "postgresql"

    def constraint_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().constraint_sql(model, schema_editor)

    def create_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().create_sql(model, schema_editor)

    def remove_sql(self, model, schema_editor):
        if not self._solo_postgres(schema_editor):
            return None
        return super().remove_sql(model, schema_editor)


class TipoContrato(models.TextChoices):
    """Tipos ATÓMICOS de servicio que puede tener un contrato.

    Un contrato puede tener VARIOS (ver el modelo `ContratoTipo`, tabla puente
    `contrato_tipos`): así O&M es un contrato con {operacion, mantenimiento} y
    representación uno con {representacion, cgm}, sin inventar valores combinados.
    La administración NO es un tipo: es un concepto de tarifa (TarifaConcepto) del
    contrato de representación."""

    REPRESENTACION = "representacion", "Representación"
    CGM = "cgm", "CGM"
    COMPRAVENTA_ENERGIA = "compraventa_energia", "Compraventa de energía"
    ARRIENDO = "arriendo", "Arriendo"
    OPERACION = "operacion", "Operación"
    MANTENIMIENTO = "mantenimiento", "Mantenimiento"
    INTERNET = "internet", "Internet"


class EstadoContrato(models.TextChoices):
    VIGENTE = "vigente", "Vigente"
    VENCIDO = "vencido", "Vencido"
    TERMINADO = "terminado", "Terminado"
    EN_RENOVACION = "en_renovacion", "En renovación"


class ContratoRol(models.TextChoices):
    PROPIETARIO = "propietario", "Propietario"
    ARRENDADOR = "arrendador", "Arrendador"
    ARRENDATARIO = "arrendatario", "Arrendatario"
    COMPRADOR = "comprador", "Comprador"
    VENDEDOR = "vendedor", "Vendedor"
    OPERADOR = "operador", "Operador"
    MANTENEDOR = "mantenedor", "Mantenedor"
    REPRESENTANTE = "representante", "Representante"


class PeriodicidadPago(models.TextChoices):
    MENSUAL = "mensual", "Mensual"
    BIMESTRAL = "bimestral", "Bimestral"
    TRIMESTRAL = "trimestral", "Trimestral"
    SEMESTRAL = "semestral", "Semestral"
    ANUAL = "anual", "Anual"


class TarifaConcepto(models.TextChoices):
    ADMINISTRACION = "administracion", "Administración"
    CGM = "cgm", "CGM"
    REPRESENTACION = "representacion", "Representación"
    CANON = "canon", "Canon"
    ENERGIA = "energia", "Energía"


class TarifaUnidad(models.TextChoices):
    # `porcentaje` se guarda como fracción (0.038 = 3,8 %), con CHECK valor <= 1.
    PORCENTAJE = "porcentaje", "Porcentaje"
    COP_KWH = "cop_kwh", "COP/kWh"
    COP_MES = "cop_mes", "COP/mes"
    COP_TOTAL = "cop_total", "COP total"


class TarifaOrigen(models.TextChoices):
    PACTADA = "pactada", "Pactada"            # valor inicial del contrato (la base)
    INDEXACION = "indexacion", "Indexación"   # ajuste por IPC/IPP sobre el valor anterior
    RENEGOCIACION = "renegociacion", "Renegociación"  # acuerdo entre partes, sin índice
    CORRECCION = "correccion", "Corrección"   # se corrigió un valor mal cargado
    MIGRACION = "migracion", "Migración"      # viene del escalar viejo; fecha inicial incierta


class Compraventa(models.TextChoices):
    """Sentido de un contrato de compraventa de energía (PPA)."""

    VENTA = "venta", "Venta"
    COMPRA = "compra", "Compra"


class Contrato(Timer):
    """Acuerdo entre partes sobre una o varias plantas — la ÚNICA tabla de contratos.

    Reemplaza a `ppa_contratos` y `contratos_servicio`: un contrato de cualquier tipo
    (PPA, representación, O&M, arriendo) es una fila acá. Los TIPOS van en la tabla
    puente `contrato_tipos` (un contrato puede tener varios: representación + CGM, u
    operación + mantenimiento); se llega a ellos por `contrato.tipos`.

    Es una tabla ANCHA a propósito (decisión del usuario 2026-09-24): los atributos
    escalares específicos de cada tipo son columnas nullable acá. Lo que NO son columnas:
    - precios/indexación versionados -> `contrato_tarifas` (por concepto, con vigencia);
    - partes (comprador/vendedor/contratante/prestador) -> `contrato_partes` (los
      `*_nombre`/`*_nit` se conservan como copia denormalizada mientras se migran los
      lectores);
    - plantas cubiertas -> `contrato_proyectos`."""

    id = models.BigAutoField(primary_key=True)
    estado = models.CharField(
        max_length=13, choices=EstadoContrato.choices, default=EstadoContrato.VIGENTE
    )
    numero_contrato = models.CharField(max_length=120, null=True, blank=True)
    # Identificador propio del PPA (contratos_servicio usa `numero_contrato`; los dos
    # coexisten porque una fila es de un tipo o del otro).
    numero_codigo_contrato = models.CharField(max_length=100, null=True, blank=True)
    nombre_interno = models.CharField(max_length=255, null=True, blank=True)
    fecha_firma_contrato = models.DateField(null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    periodicidad_pago = models.CharField(
        max_length=10, choices=PeriodicidadPago.choices, null=True, blank=True
    )
    indice_indexacion = models.CharField(max_length=60, null=True, blank=True)
    renovacion_automatica = models.BooleanField(default=False)

    # --- Copia denormalizada de las partes (se llenan además de contrato_partes) ---
    comprador_nombre = models.CharField(max_length=255, null=True, blank=True)
    comprador_nit = models.CharField(max_length=20, null=True, blank=True)
    vendedor_nombre = models.CharField(max_length=255, null=True, blank=True)
    vendedor_nit = models.CharField(max_length=20, null=True, blank=True)
    contratante_nombre = models.CharField(max_length=255, null=True, blank=True)
    contratante_nit = models.CharField(max_length=20, null=True, blank=True)
    prestador_nombre = models.CharField(max_length=255, null=True, blank=True)
    prestador_nit = models.CharField(max_length=20, null=True, blank=True)
    inversionista_nombre = models.CharField(max_length=255, null=True, blank=True)

    # --- Específicas de compraventa de energía (PPA), antes en ppa_contratos ---
    responsable = models.ForeignKey(
        "ppa.PpaResponsable", on_delete=models.SET_NULL, db_column="responsable_id",
        null=True, blank=True, related_name="contratos_unificados",
    )
    # venta | compra (antes ppa_contratos.tipo_contrato).
    tipo_contrato = models.CharField(
        max_length=20, choices=Compraventa.choices, null=True, blank=True,
    )
    comprador = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="comprador_id",
        null=True, blank=True, related_name="contratos_unif_por_comprador",
    )
    vendedor = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="vendedor_id",
        null=True, blank=True, related_name="contratos_unif_por_vendedor",
    )
    periodicidad_indexacion = models.CharField(max_length=50, null=True, blank=True)
    periodo_indexacion_base = models.CharField(max_length=7, null=True, blank=True)
    valor_indexacion_base = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    cantidad_minima_kwh_mes = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    cantidad_maxima_kwh_mes = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    periodicidad_facturacion = models.CharField(max_length=50, null=True, blank=True)
    tiempo_pago = models.IntegerField(null=True, blank=True)
    condiciones_pago = models.CharField(max_length=500, null=True, blank=True)
    gescon_codigo = models.CharField(max_length=100, null=True, blank=True)
    gescon_fecha_inicio = models.DateField(null=True, blank=True)
    gescon_fecha_fin = models.DateField(null=True, blank=True)
    gescon_precio = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    gescon_cantidades_kwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    codigo_sic = models.CharField(max_length=50, null=True, blank=True)
    es_comunidad_energetica = models.BooleanField(null=True, blank=True)
    fecha_entrada_comunidad = models.DateField(null=True, blank=True)
    nombre_comunidad = models.CharField(max_length=255, null=True, blank=True)

    # --- Específicas de contratos de servicio, antes en contratos_servicio ---
    # Qué servicio presta esta fila (representacion/cgm/mantenimiento/arriendo/internet).
    # Se conserva como columna además de `contrato_tipos` para que los lectores actuales
    # sigan filtrando por ella sin reescribirse (fachadas proxy).
    servicio_aplica = models.CharField(
        max_length=14,
        choices=[("representacion", "representacion"), ("cgm", "cgm"),
                 ("mantenimiento", "mantenimiento"), ("arriendo", "arriendo"),
                 ("internet", "internet")],
        null=True, blank=True,
    )
    contratante = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="contratante_id",
        null=True, blank=True, related_name="contratos_unif_por_contratante",
    )
    prestador = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="prestador_id",
        null=True, blank=True, related_name="contratos_unif_por_prestador",
    )
    inversionista = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="inversionista_id",
        null=True, blank=True, related_name="contratos_unif_por_inversionista",
    )
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id",
        null=True, blank=True, related_name="contratos_unif_por_proyecto",
    )
    # Tarifas escalares e indexación JSON (redundantes con contrato_tarifas; autoritativas
    # para los lectores actuales durante la transición — decisión del usuario 2026-09-24).
    tarifa_admin = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    tarifa_cgm = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_representacion = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_mensual = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    indexacion_anual = models.JSONField(null=True, blank=True)
    indexacion_mensual = models.JSONField(null=True, blank=True)
    indexacion_cgm = models.JSONField(null=True, blank=True)
    indexacion_representacion = models.JSONField(null=True, blank=True)
    cgm_codigo_sic = models.CharField(max_length=20, null=True, blank=True)
    fecha_inicio_om = models.DateField(null=True, blank=True)
    fecha_indexacion = models.DateField(null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    estado_pago = models.CharField(max_length=20, null=True, blank=True)
    portafolio = models.CharField(max_length=255, null=True, blank=True)
    codigo_sun_factory = models.CharField(max_length=50, null=True, blank=True)
    nombre_proyecto_ref = models.CharField(max_length=255, null=True, blank=True)
    ubicacion_lat = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    ubicacion_lng = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    # Conectividad (internet/Starlink)
    plan_datos_gb = models.CharField(max_length=50, null=True, blank=True)
    velocidad_mbps = models.IntegerField(null=True, blank=True)
    tipo_conexion = models.CharField(max_length=50, null=True, blank=True)
    linea_servicio = models.CharField(max_length=100, null=True, blank=True)
    id_router = models.CharField(max_length=100, null=True, blank=True)
    numero_kit = models.CharField(max_length=100, null=True, blank=True)
    latencia_ms = models.IntegerField(null=True, blank=True)
    wifi_seguridad = models.CharField(max_length=50, null=True, blank=True)
    wifi_password = models.CharField(max_length=100, null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contratos"
        constraints = [
            models.CheckConstraint(
                name="ck_contratos_fechas",
                condition=(
                    Q(fecha_fin__isnull=True)
                    | Q(fecha_inicio__isnull=True)
                    | Q(fecha_fin__gte=F("fecha_inicio"))
                ),
            ),
            models.CheckConstraint(
                name="ck_contratos_tarifa",
                condition=Q(tarifa_base__isnull=True) | Q(tarifa_base__gte=0),
            ),
        ]


class ContratoTipo(models.Model):
    """Un tipo de servicio de un contrato (tabla puente `contrato_tipos`).

    Un contrato puede tener varias filas acá: representación + CGM en el mismo
    contrato, u operación + mantenimiento en el mismo (O&M). Evita tener que inventar
    valores de enum combinados. Se accede por `contrato.tipos`."""

    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="tipos",
    )
    tipo = models.CharField(max_length=19, choices=TipoContrato.choices)
    pk = models.CompositePrimaryKey("contrato_id", "tipo")

    class Meta:
        db_table = "contrato_tipos"


class ContratoParte(models.Model):
    """Qué papel juega cada cliente en un contrato; reemplaza las columnas
    contratante/prestador/comprador/vendedor. El DDL solo tiene `created_at`, así
    que NO hereda Timer."""

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="partes",
    )
    # ON DELETE RESTRICT del DDL: no se borra un cliente que es parte de un contrato.
    cliente = models.ForeignKey(
        "clientes.Cliente", on_delete=models.PROTECT, db_column="cliente_id",
        related_name="contrato_partes",
    )
    rol = models.CharField(max_length=13, choices=ContratoRol.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contrato_partes"
        constraints = [
            models.UniqueConstraint(
                fields=["contrato", "cliente", "rol"], name="uq_contrato_partes"
            ),
        ]


class ContratoProyecto(models.Model):
    """Qué plantas cubre un contrato (N:M); unifica el escalar de contratos_servicio
    y la N:M de los PPA. Espejo de PpaContratoProyecto."""

    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="proyectos",
    )
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id",
        related_name="contratos_unificados",
    )
    pk = models.CompositePrimaryKey("contrato_id", "proyecto_id")

    class Meta:
        db_table = "contrato_proyectos"


class ContratoTarifa(models.Model):
    """Qué se cobra en un contrato, por concepto y CON VIGENCIA (D-24).

    Las tarifas se renegocian e indexan cada año: la misma CGM de Ayura 1 vale 5,0 en
    2024, 5,26 en 2025 y 5,52826 en 2026. La `vigencia` (rango [desde, hasta)) permite
    que la liquidación de un periodo use la tarifa vigente EN ese periodo, no la actual.

    La base es la fila con `origen='pactada'` (no hay columna `es_base`). Una tarifa
    cargada por error se ANULA (anulada_en/motivo), no se borra: el histórico completo es
    el requisito. Las anuladas salen del EXCLUDE de no-solape. El DDL solo tiene
    `created_at`, así que NO hereda Timer."""

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="tarifas",
    )
    concepto = models.CharField(max_length=14, choices=TarifaConcepto.choices)
    valor = models.DecimalField(max_digits=14, decimal_places=6)
    # Obligatoria: administración es un porcentaje y CGM es COP/kWh, y en las columnas
    # de hoy son indistinguibles.
    unidad = models.CharField(max_length=10, choices=TarifaUnidad.choices)
    vigencia = DateRangeField()
    origen = models.CharField(max_length=13, choices=TarifaOrigen.choices)
    # Solo una indexación dice sobre qué índice y cuánto; el resto, NULL (CHECK).
    indice = models.CharField(max_length=20, null=True, blank=True)
    indice_pct = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    nota = models.TextField(null=True, blank=True)
    anulada_en = models.DateTimeField(null=True, blank=True)
    anulada_motivo = models.TextField(null=True, blank=True)
    anulada_por = models.ForeignKey(
        "plataforma.Usuario", on_delete=models.SET_NULL, db_column="anulada_por_id",
        null=True, blank=True, related_name="tarifas_anuladas",
    )
    registrado_por = models.ForeignKey(
        "plataforma.Usuario", on_delete=models.SET_NULL, db_column="registrado_por_id",
        null=True, blank=True, related_name="tarifas_registradas",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "contrato_tarifas"
        constraints = [
            models.CheckConstraint(
                name="ck_contrato_tarifas_valor", condition=Q(valor__gte=0)
            ),
            PgCheckConstraint(
                name="ck_contrato_tarifas_vigencia", condition=Q(vigencia__isempty=False)
            ),
            # Un porcentaje se guarda como fracción (<= 1).
            models.CheckConstraint(
                name="ck_contrato_tarifas_pct",
                condition=~Q(unidad=TarifaUnidad.PORCENTAJE) | Q(valor__lte=1),
            ),
            # Solo `indexacion` lleva índice/porcentaje; el resto los deja en NULL.
            models.CheckConstraint(
                name="ck_contrato_tarifas_indice",
                condition=(
                    (Q(origen=TarifaOrigen.INDEXACION) & Q(indice__isnull=False))
                    | (
                        ~Q(origen=TarifaOrigen.INDEXACION)
                        & Q(indice__isnull=True)
                        & Q(indice_pct__isnull=True)
                    )
                ),
            ),
            # Lo migrado debe decir por qué su fecha es incierta.
            models.CheckConstraint(
                name="ck_contrato_tarifas_migracion",
                condition=~Q(origen=TarifaOrigen.MIGRACION) | Q(nota__isnull=False),
            ),
            models.CheckConstraint(
                name="ck_contrato_tarifas_anulada",
                condition=(
                    (Q(anulada_en__isnull=True) & Q(anulada_motivo__isnull=True))
                    | (Q(anulada_en__isnull=False) & Q(anulada_motivo__isnull=False))
                ),
            ),
            # Un concepto no puede tener dos valores vigentes a la vez en el mismo
            # contrato. Las anuladas quedan fuera. Mismo mecanismo que protegerá la
            # composición accionaria (D-08).
            PgExclusionConstraint(
                name="ex_contrato_tarifas_sin_solape",
                expressions=[
                    ("contrato", RangeOperators.EQUAL),
                    ("concepto", RangeOperators.EQUAL),
                    ("vigencia", RangeOperators.OVERLAPS),
                ],
                condition=Q(anulada_en__isnull=True),
                index_type="gist",
            ),
        ]
