"""Modelos del dominio `proyectos`.

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

class Proyecto(Timer):
    id = models.BigAutoField(primary_key=True)
    portafolio = models.ForeignKey("Portafolio", on_delete=models.DO_NOTHING, db_column="portafolio_id", null=True, blank=True, related_name="proyectos")
    nombre_comercial = models.CharField(max_length=255)
    sub_project = models.CharField(max_length=50, null=True, blank=True)
    topico_liquidaciones = models.CharField(max_length=100, null=True, blank=True)
    clasificacion_regulatoria = models.CharField(max_length=4, choices=[("AGP", "AGP"), ("AGPE", "AGPE"), ("AGGE", "AGGE"), ("GD", "GD"), ("DER", "DER"), ("otra", "otra")], null=True, blank=True)
    tipo_tecnologia = models.CharField(max_length=10, choices=[("solar", "solar"), ("eolica", "eolica"), ("hidraulica", "hidraulica"), ("biomasa", "biomasa"), ("otra", "otra")], null=True, blank=True)
    tipo_proyecto = models.CharField(max_length=19, choices=[("minigranja", "minigranja"), ("autoconsumo", "autoconsumo"), ("gd", "gd"), ("movilidad_electrica", "movilidad_electrica"), ("otro", "otro")], null=True, blank=True)
    potencia_instalada_kwp = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    potencia_con_cen_mw = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    produccion_especifica_kwh_kwp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    codigo_cnd = models.CharField(max_length=50, null=True, blank=True)
    estado = models.CharField(max_length=13, choices=[("en_desarrollo", "en_desarrollo"), ("en_operacion", "en_operacion"), ("suspendido", "suspendido"), ("cancelado", "cancelado")], default="en_desarrollo")
    fecha_entrada_operacion = models.DateField(null=True, blank=True)
    fecha_fin_representacion = models.DateField(null=True, blank=True)
    fecha_inicio_comercializacion = models.DateField(null=True, blank=True)
    fecha_comercializacion_editada_manual = models.BooleanField(default=False)
    gen_mensual_promedio_mwh = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    gen_promedio_origen = models.CharField(max_length=10, null=True, blank=True)
    gen_promedio_dias = models.IntegerField(null=True, blank=True)
    gen_promedio_desde = models.DateField(null=True, blank=True)
    gen_promedio_hasta = models.DateField(null=True, blank=True)
    gen_promedio_actualizado_en = models.DateTimeField(null=True, blank=True)
    departamento = models.CharField(max_length=100, null=True, blank=True)
    municipio = models.CharField(max_length=100, null=True, blank=True)
    direccion_vereda = models.CharField(max_length=500, null=True, blank=True)
    latitud = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitud = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    altitud_msnm = models.IntegerField(null=True, blank=True)
    operador_red = models.ForeignKey("fronteras.OperadorRed", on_delete=models.DO_NOTHING, db_column="operador_red_id", null=True, blank=True, related_name="proyectos_por_operador_red_id")
    project_id_solenium = models.CharField(max_length=100, null=True, blank=True)
    project_id_solarview = models.CharField(max_length=100, null=True, blank=True)
    es_comunidad_energetica = models.BooleanField(default=False)
    nombre_comunidad = models.CharField(max_length=255, null=True, blank=True)
    srv_operacion = models.BooleanField(default=False)
    srv_representacion = models.BooleanField(default=False)
    srv_cgm = models.BooleanField(default=False)
    srv_ppa = models.BooleanField(default=False)
    p90_mensual_kwh = models.JSONField(null=True, blank=True)
    p50_mensual_kwh = models.JSONField(null=True, blank=True)
    p99_mensual_kwh = models.JSONField(null=True, blank=True)
    codigo_tsf = models.CharField(max_length=100, null=True, blank=True)
    origina_code = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    sunfactory_project_id = models.IntegerField(null=True, blank=True)
    fase_construccion = models.CharField(max_length=40, null=True, blank=True)
    fecha_estimada_energizacion = models.DateField(null=True, blank=True)
    avance_obra_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    origen = models.CharField(max_length=20, null=True, blank=True, default="manual")
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "proyectos"
        unique_together = [("sub_project",)]
        unique_together = [("project_id_solarview",)]
        unique_together = [("sunfactory_project_id",)]
        unique_together = [("project_id_solenium",)]


class Portafolio(Timer):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=255)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "portafolios"
        unique_together = [("nombre",)]


class ProyectoInfoTecnica(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="info_tecnica")
    voltaje_red = models.CharField(max_length=50, null=True, blank=True)
    potencia_ac_kw = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    capacidad_instalada_kwp = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    tipo_tracker = models.CharField(max_length=2, choices=[("1P", "1P"), ("2P", "2P")], null=True, blank=True)
    cantidad_total_paneles = models.IntegerField(null=True, blank=True)
    potencia_panel_kwp = models.CharField(max_length=100, null=True, blank=True)
    marca_paneles = models.CharField(max_length=255, null=True, blank=True)
    cantidad_inversores = models.IntegerField(null=True, blank=True)
    potencia_inversores_kwp = models.CharField(max_length=100, null=True, blank=True)
    marca_inversores = models.CharField(max_length=255, null=True, blank=True)
    cantidad_strings = models.IntegerField(null=True, blank=True)
    marca_transformador = models.CharField(max_length=255, null=True, blank=True)
    marca_reconectador_rele = models.CharField(max_length=500, null=True, blank=True)
    marca_totalizador = models.CharField(max_length=255, null=True, blank=True)
    marca_seguidor_solar = models.CharField(max_length=255, null=True, blank=True)
    marca_medidores_frontera = models.CharField(max_length=255, null=True, blank=True)
    marca_modem_reconectador = models.CharField(max_length=500, null=True, blank=True)
    marca_modems_frontera = models.CharField(max_length=255, null=True, blank=True)
    ip_modem_reconectador = models.CharField(max_length=100, null=True, blank=True)
    url_ubicacion = models.TextField(null=True, blank=True)
    cctv_estado = models.TextField(null=True, blank=True)
    marca_cctv = models.CharField(max_length=255, null=True, blank=True)
    seguridad_fisica = models.CharField(max_length=255, null=True, blank=True)
    tiene_internet = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "proyecto_info_tecnica"


class ProyectoInversor(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="inversores")
    nombre = models.CharField(max_length=120, null=True, blank=True)
    potencia_nominal_kw = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "proyecto_inversores"
        unique_together = [("nombre", "proyecto")]


class ProyectoInversionista(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="inversionistas")
    cliente = models.ForeignKey("clientes.Cliente", on_delete=models.DO_NOTHING, db_column="cliente_id", related_name="proyecto_inversionistas_por_cliente_id")
    porcentaje_participacion = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    es_patrimonio_autonomo = models.BooleanField(default=False)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "proyecto_inversionistas"


class ProyectoPendienteIgnorado(models.Model):
    id = models.BigAutoField(primary_key=True)
    clave = models.CharField(max_length=120)
    ignorado_por_usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="ignorado_por_usuario_id", null=True, blank=True, related_name="proyectos_pendientes_ignorados_por_ignorado_por_usuario_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "proyectos_pendientes_ignorados"
        unique_together = [("clave",)]


class GestionRegistro(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="gestion_registros_por_proyecto_id")
    tipo = models.CharField(max_length=50, choices=[("pqr", "pqr"), ("preventivo", "preventivo"), ("correctivo", "correctivo")])  # la columna es varchar(50)
    titulo = models.CharField(max_length=500)
    descripcion = models.TextField(null=True, blank=True)
    created_by = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "gestion_registros"


class CostoVariable(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="costos_variables_por_proyecto_id")
    tipo_accion = models.CharField(max_length=50)
    tipo_equipo = models.CharField(max_length=100)
    monto = models.DecimalField(max_digits=18, decimal_places=2)
    fecha = models.DateField(db_index=True)
    descripcion = models.TextField()
    observaciones = models.TextField(null=True, blank=True)
    url_factura = models.CharField(max_length=500, null=True, blank=True)
    nombre_factura = models.CharField(max_length=255, null=True, blank=True)
    url_cotizacion = models.CharField(max_length=500, null=True, blank=True)
    nombre_cotizacion = models.CharField(max_length=255, null=True, blank=True)
    url_rut = models.CharField(max_length=500, null=True, blank=True)
    nombre_rut = models.CharField(max_length=255, null=True, blank=True)
    url_certificado_bancario = models.CharField(max_length=500, null=True, blank=True)
    nombre_certificado_bancario = models.CharField(max_length=255, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "costos_variables"


class VerificacionCosto(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="verificacion_costos")
    costos_generador = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    costos_comercializador = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    ac_power = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "verificacion_costos"


class GeneracionDiaria(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="generacion_diaria_por_proyecto_id")
    fecha = models.DateField()
    kwh_real = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    kwh_p90 = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    kwh_autoconsumo = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuente = models.CharField(max_length=50, default="manual")
    notas = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "generacion_diaria"
        unique_together = [("fecha", "proyecto")]
