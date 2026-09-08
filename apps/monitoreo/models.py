"""Modelos del dominio `monitoreo`.

GENERADO por scripts/generar_modelos_django.py desde los metadatos de
SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los
TextChoices de las columnas de estado y los docstrings que explican el
modelo de datos. Revisar antes de portar la API del recurso.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.
"""

from datetime import date, timedelta, timezone as tz_std

from django.db import models
from django.utils import timezone

from apps.plataforma.models import Timer

_COL_TZ = tz_std(timedelta(hours=-5))  # Colombia (UTC-5), sin horario de verano

class FallaCatCategoria(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=50)
    etiqueta = models.CharField(max_length=255)
    icono = models.CharField(max_length=100, null=True, blank=True)
    color_hex = models.CharField(max_length=7, null=True, blank=True)
    orden = models.IntegerField(default=0)
    activa = models.BooleanField(default=True)

    class Meta:
        db_table = "fallas_cat_categorias"
        unique_together = [("codigo",)]


class FallaCatTipo(models.Model):
    id = models.BigAutoField(primary_key=True)
    categoria = models.ForeignKey("FallaCatCategoria", on_delete=models.DO_NOTHING, db_column="categoria_id", related_name="tipos")
    codigo = models.CharField(max_length=50)
    etiqueta = models.CharField(max_length=255)
    descripcion = models.TextField(null=True, blank=True)
    activa = models.BooleanField(default=True)

    class Meta:
        db_table = "fallas_cat_tipos"
        unique_together = [("codigo",)]


class FallaCatEstado(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=50)
    etiqueta = models.CharField(max_length=255)
    orden = models.IntegerField(default=0)
    es_estado_final = models.BooleanField(default=False)

    class Meta:
        db_table = "fallas_cat_estados"
        unique_together = [("codigo",)]


class FallaCatPrioridad(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=50)
    etiqueta = models.CharField(max_length=255)
    nivel = models.IntegerField()

    class Meta:
        db_table = "fallas_cat_prioridades"
        unique_together = [("codigo",)]


class FallaCatResolucion(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=50)
    etiqueta = models.CharField(max_length=255)

    class Meta:
        db_table = "fallas_cat_resoluciones"
        unique_together = [("codigo",)]


class Falla(Timer):
    id = models.BigAutoField(primary_key=True)
    codigo_interno = models.CharField(max_length=30)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="fallas_por_proyecto_id")
    tipo = models.ForeignKey("FallaCatTipo", on_delete=models.DO_NOTHING, db_column="tipo_id", null=True, blank=True, related_name="fallas_por_tipo_id")
    estado = models.ForeignKey("FallaCatEstado", on_delete=models.DO_NOTHING, db_column="estado_id", related_name="fallas_por_estado_id")
    prioridad = models.ForeignKey("FallaCatPrioridad", on_delete=models.DO_NOTHING, db_column="prioridad_id", related_name="fallas_por_prioridad_id")
    resolucion = models.ForeignKey("FallaCatResolucion", on_delete=models.DO_NOTHING, db_column="resolucion_id", null=True, blank=True, related_name="fallas_por_resolucion_id")
    registrado_por = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="registrado_por_id", related_name="fallas_por_registrado_por_id")
    descripcion = models.TextField()
    fecha_identificacion = models.DateField(db_index=True)
    hora_identificacion = models.TimeField(null=True, blank=True)
    fecha_ocurrencia = models.DateTimeField(null=True, blank=True)
    fecha_resolucion = models.DateTimeField(null=True, blank=True)
    sla_limite_horas = models.IntegerField(null=True, blank=True)
    sla_cumplido = models.BooleanField(null=True, blank=True)
    fotos_urls = models.JSONField(null=True, blank=True)
    notificacion = models.BooleanField(default=False)
    alarma_monitoreo = models.ForeignKey("AlarmaMonitoreo", on_delete=models.SET_NULL, db_column="alarma_monitoreo_id", null=True, blank=True, related_name="fallas_por_alarma_monitoreo_id")
    kwh_perdidos_estimado = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    impacto_economico_cop = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)
    causa_raiz = models.TextField(null=True, blank=True)
    acciones_correctivas = models.TextField(null=True, blank=True)
    fecha_programada = models.DateField(null=True, blank=True, db_index=True)
    categoria_codigo = models.CharField(max_length=50, null=True, blank=True, db_index=True)
    subtipo_codigo = models.CharField(max_length=80, null=True, blank=True, db_index=True)
    subtipo_detalle = models.TextField(null=True, blank=True)
    clasificacion = models.JSONField(null=True, blank=True)
    pendiente_reclasificar = models.BooleanField(default=False)
    frontera_afecta_medicion = models.BooleanField(null=True, blank=True)
    frontera_perdida_comunicacion = models.BooleanField(null=True, blank=True)
    inversores_perdida_comunicacion = models.BooleanField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fallas"
        unique_together = [("codigo_interno",)]


class FallaSeguimiento(models.Model):
    id = models.BigAutoField(primary_key=True)
    falla = models.ForeignKey("Falla", on_delete=models.DO_NOTHING, db_column="falla_id", related_name="seguimientos")
    usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="usuario_id", related_name="fallas_seguimientos_por_usuario_id")
    nota = models.TextField(null=True, blank=True)
    estado_nuevo = models.ForeignKey("FallaCatEstado", on_delete=models.DO_NOTHING, db_column="estado_nuevo_id", null=True, blank=True, related_name="fallas_seguimientos_por_estado_nuevo_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "fallas_seguimientos"
        # El historial se lee SIEMPRE del mas reciente al mas viejo, y se ordena
        # aca y no en cada vista.
        #
        # Sin esto el orden lo decidia Postgres (sin ORDER BY no esta definido) y
        # cada pantalla resolvia por su cuenta: `FallaDetailView.vue` ordenaba en
        # el cliente y `FallaDetailSheet.vue` (movil) renderizaba tal cual
        # llegaba, asi que la misma falla podia mostrar su cronologia desordenada
        # en el telefono y bien en el escritorio. Es la tercera vez que esas dos
        # vistas divergen por una decision duplicada (ver
        # app/features/solar/serieSolar.js en el frontend).
        #
        # `-id` desempata: dos notas del mismo segundo (una carga masiva) no
        # pueden quedar en orden arbitrario.
        ordering = ["-created_at", "-id"]


class FallaIntervalo(models.Model):
    id = models.BigAutoField(primary_key=True)
    falla = models.ForeignKey("Falla", on_delete=models.DO_NOTHING, db_column="falla_id", related_name="intervalos")
    inicio = models.DateTimeField()
    fin = models.DateTimeField(null=True, blank=True)
    nota = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, null=True, blank=True)  # la base la acepta nula

    class Meta:
        db_table = "fallas_intervalos"


class FallaInversor(models.Model):
    id = models.BigAutoField(primary_key=True)
    falla = models.ForeignKey("Falla", on_delete=models.DO_NOTHING, db_column="falla_id", related_name="inversores_afectados")
    proyecto_inversor = models.ForeignKey("proyectos.ProyectoInversor", on_delete=models.SET_NULL, db_column="proyecto_inversor_id", null=True, blank=True, related_name="falla_inversores_por_proyecto_inversor_id")
    nombre = models.CharField(max_length=120, null=True, blank=True)
    potencia_kw = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    tipos = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "falla_inversores"


class Alerta(Timer):
    id = models.BigAutoField(primary_key=True)
    ppa = models.ForeignKey("ppa.PpaContrato", on_delete=models.CASCADE, db_column="ppa_id", related_name="alertas_por_ppa_id")
    project = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="project_id", null=True, blank=True, related_name="alertas_por_project_id")
    alert_type = models.CharField(max_length=50)
    description = models.TextField(null=True, blank=True)
    due_date = models.DateField()
    trigger_date = models.DateField(default=date.today)  # la base ya trae CURRENT_DATE
    days_to_expiration = models.IntegerField()
    status = models.CharField(max_length=20, default="new")

    class Meta:
        db_table = "alertas"
        unique_together = [("days_to_expiration", "ppa")]


class Mantenimiento(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="mantenimientos_por_proyecto_id")
    registrado_por = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="registrado_por_id", related_name="mantenimientos_por_registrado_por_id")
    fecha = models.DateField()
    tipo = models.CharField(max_length=10, choices=[("preventivo", "preventivo"), ("correctivo", "correctivo"), ("predictivo", "predictivo")])
    descripcion = models.TextField()
    estado = models.CharField(max_length=12, choices=[("programado", "programado"), ("en_ejecucion", "en_ejecucion"), ("completado", "completado"), ("cancelado", "cancelado")], default="programado")
    observaciones = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "mantenimientos"


class MantenimientoImpacto(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="mantenimiento_impacto_por_proyecto_id")
    falla = models.ForeignKey("Falla", on_delete=models.SET_NULL, db_column="falla_id", null=True, blank=True, related_name="mantenimiento_impacto_por_falla_id")
    maintenance_type = models.CharField(max_length=50, default="scheduled")
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField()
    expected_generation_kwh = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    actual_generation_kwh = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    lost_energy_kwh = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    financial_impact_cop = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    ppa_penalty_risk_flag = models.BooleanField(default=False)
    created_by = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="created_by", null=True, blank=True, related_name="mantenimiento_impacto_por_created_by")

    class Meta:
        db_table = "mantenimiento_impacto"

    @property
    def duration_hours(self) -> float | None:
        """Duracion del evento (end - start) en horas. None si falta un extremo.

        No es una columna: se calcula. En SQLAlchemy era un `@hybrid_property`
        (app/models/mantenimiento_impacto.py) y `scripts/generar_modelos_django.py`
        no la trajo, porque lee los metadatos de las COLUMNAS -- una propiedad de
        Python no aparece ahi. `ImpactoSerializer` la siguio pidiendo en
        `Meta.fields`, asi que el serializer no se podia ni construir y los cuatro
        endpoints de `mantenimiento-impacto` respondian 500 en cada peticion.

        Se repone como propiedad del modelo y no como campo del serializer para
        que siga siendo la misma definicion para todo el que la consulte, igual
        que antes.
        """
        if not self.start_time or not self.end_time:
            return None
        inicio, fin = self.start_time, self.end_time
        # Con USE_TZ=True Django las devuelve aware, pero una instancia armada a
        # mano (un test, un script) puede traerlas naive: se asume Colombia,
        # mismo criterio que tenia el hybrid_property.
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=_COL_TZ)
        if fin.tzinfo is None:
            fin = fin.replace(tzinfo=_COL_TZ)
        return round(max(0.0, (fin - inicio).total_seconds() / 3600), 2)


class StarlinkFactura(Timer):
    id = models.BigAutoField(primary_key=True)
    periodo = models.CharField(max_length=7, db_index=True)
    items_json = models.TextField()
    agrupado_json = models.TextField()
    cargos_totales = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    suma_items = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "starlink_facturas"


class StarlinkMapeoSitio(Timer):
    id = models.BigAutoField(primary_key=True)
    patron = models.CharField(max_length=255, db_index=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="starlink_mapeo_sitio_por_proyecto_id")
    activo = models.BooleanField(default=True)
    excluido = models.BooleanField(default=False)

    class Meta:
        db_table = "starlink_mapeo_sitio"


class StarlinkFacturaLinea(Timer):
    id = models.BigAutoField(primary_key=True)
    factura = models.ForeignKey("StarlinkFactura", on_delete=models.CASCADE, db_column="factura_id", related_name="lineas")
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="starlink_factura_linea_por_proyecto_id")
    descripcion = models.CharField(max_length=255)
    excluido = models.BooleanField(default=False)
    sin_iva = models.DecimalField(max_digits=15, decimal_places=2)
    iva = models.DecimalField(max_digits=15, decimal_places=2)
    monto_total = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        db_table = "starlink_factura_linea"


class AlarmaMonitoreo(models.Model):
    id = models.BigAutoField(primary_key=True)
    proyecto_nombre = models.CharField(max_length=255)
    severity = models.CharField(max_length=20)
    alarm_type = models.CharField(max_length=50)
    details = models.TextField()
    source_data = models.JSONField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "alarmas_monitoreo"
