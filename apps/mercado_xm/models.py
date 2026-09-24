"""Modelos del dominio `mercado_xm`.

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

class DespachoContratoDia(models.Model):
    id = models.BigAutoField(primary_key=True)
    periodo = models.CharField(max_length=7, db_index=True)
    codigo_sic_contrato = models.CharField(max_length=40, db_index=True)
    fecha = models.DateField()
    kwh = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        db_table = "despacho_contrato_dia"
        unique_together = [("codigo_sic_contrato", "fecha", "periodo")]


class DespachoContratoMensual(models.Model):
    id = models.BigAutoField(primary_key=True)
    periodo = models.CharField(max_length=7)
    codigo_sic_contrato = models.CharField(max_length=40)
    vendedor = models.CharField(max_length=40, null=True, blank=True)
    comprador = models.CharField(max_length=40, null=True, blank=True)
    tipo = models.CharField(max_length=20, null=True, blank=True)
    kwh = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    dias = models.IntegerField(null=True, blank=True)
    fecha_min = models.DateField(null=True, blank=True)
    fecha_max = models.DateField(null=True, blank=True)
    archivo = models.CharField(max_length=200, null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "despacho_contrato_mensual"
        unique_together = [("codigo_sic_contrato", "periodo")]


class PrecioBolsaMensual(models.Model):
    id = models.BigAutoField(primary_key=True)
    año = models.IntegerField()
    mes = models.IntegerField()
    valor = models.DecimalField(max_digits=12, decimal_places=4)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "precio_bolsa_mensual"
        unique_together = [("año", "mes")]


class AsicSolicitud(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="asic_solicitudes_por_proyecto_id")
    requerimiento_asic = models.CharField(max_length=20, null=True, blank=True)
    tipo_solicitud = models.CharField(max_length=13, choices=[("registro", "registro"), ("modificacion", "modificacion"), ("terminacion", "terminacion"), ("desistimiento", "desistimiento")])
    prioridad_limitacion = models.IntegerField(null=True, blank=True)
    codigo_sic_contrato = models.CharField(max_length=20, null=True, blank=True)
    codigo_sic_vendedor = models.CharField(max_length=10, null=True, blank=True)
    codigo_sic_comprador = models.CharField(max_length=10, null=True, blank=True)
    cedula_agente_vendedor = models.CharField(max_length=30, null=True, blank=True)
    cedula_agente_comprador = models.CharField(max_length=30, null=True, blank=True)
    contrato_interno = models.CharField(max_length=100, null=True, blank=True)
    nombre_contacto_solicitante = models.CharField(max_length=255, null=True, blank=True)
    fecha_solicitud = models.DateField(null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tipo_mercado = models.CharField(max_length=50, null=True, blank=True, default="No regulado")
    tipo_asignacion = models.CharField(max_length=100, null=True, blank=True)
    porcentaje_fncer = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    porcentaje_despacho = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    estado_solicitud = models.CharField(max_length=10, choices=[("en_proceso", "en_proceso"), ("publicado", "publicado"), ("rechazado", "rechazado"), ("desistido", "desistido"), ("terminado", "terminado")], default="en_proceso")
    nombre_interno = models.CharField(max_length=200, null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)
    link_archivo = models.CharField(max_length=1000, null=True, blank=True)
    reemplaza_anterior = models.BooleanField(default=True)
    es_duplicado = models.BooleanField(default=False)
    uso_del_recurso = models.BooleanField(default=False)
    modalidad_pago = models.CharField(max_length=3, null=True, blank=True)
    fecha_envio_xm = models.DateField(null=True, blank=True)
    fecha_respuesta_xm = models.DateField(null=True, blank=True)
    numero_radicado = models.CharField(max_length=100, null=True, blank=True)
    contrato_ppa = models.ForeignKey("contratos.Contrato", on_delete=models.DO_NOTHING, db_column="contrato_ppa_id", null=True, blank=True, related_name="asic_solicitudes_por_contrato_ppa_id")

    class Meta:
        db_table = "asic_solicitudes"


class AsicCambioContrato(models.Model):
    id = models.BigAutoField(primary_key=True)
    solicitud = models.ForeignKey("AsicSolicitud", on_delete=models.DO_NOTHING, db_column="solicitud_id", null=True, blank=True, related_name="asic_cambios_contratos_por_solicitud_id")
    codigo_sic_contrato = models.CharField(max_length=20, null=True, blank=True)
    contrato_interno = models.CharField(max_length=100, null=True, blank=True)
    proyecto_original = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_original_id", null=True, blank=True, related_name="asic_cambios_contratos_por_proyecto_original_id")
    codigo_frt_original = models.CharField(max_length=20, null=True, blank=True)
    energia_mensual_mwh_original = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    proyecto_nuevo = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_nuevo_id", null=True, blank=True, related_name="asic_cambios_contratos_por_proyecto_nuevo_id")
    codigo_frt_nuevo = models.CharField(max_length=20, null=True, blank=True)
    energia_mensual_mwh_nuevo = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    accion = models.CharField(max_length=100, null=True, blank=True)
    nombre_archivo = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "asic_cambios_contratos"


class GesconDiccionarioContrato(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo_contrato = models.CharField(max_length=100)
    nombre = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "gescon_diccionario_contratos"
        unique_together = [("codigo_contrato",)]


class CumplimientoMensual(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato_ppa = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_ppa_id", related_name="cumplimiento_mensual_por_contrato_ppa_id")
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.SET_NULL, db_column="proyecto_id", null=True, blank=True, related_name="cumplimiento_mensual_por_proyecto_id")
    anio = models.IntegerField()
    mes = models.IntegerField()
    gen_total_mwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    compromiso_mwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    compras_bolsa_mwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    excedentes_bolsa_mwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    precio_bolsa_promedio = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    compras_bolsa_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    excedentes_bolsa_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    estado = models.CharField(max_length=9, choices=[("pendiente", "pendiente"), ("cerrado", "cerrado"), ("facturado", "facturado")], default="pendiente")
    tarifa_ppa_cop_mwh = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    valoracion_contrato_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    liquidacion = models.ForeignKey("liquidaciones.Liquidacion", on_delete=models.SET_NULL, db_column="liquidacion_id", null=True, blank=True, related_name="cumplimiento_mensual_por_liquidacion_id")

    class Meta:
        db_table = "cumplimiento_mensual"
        unique_together = [("anio", "contrato_ppa", "mes")]


class ClasificacionEnergiaMensual(models.Model):
    id = models.BigAutoField(primary_key=True)
    anio = models.IntegerField()
    mes = models.IntegerField()
    categoria = models.CharField(max_length=32)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="clasificacion_energia_mensual_por_proyecto_id")
    contrato_ppa = models.ForeignKey("contratos.Contrato", on_delete=models.SET_NULL, db_column="contrato_ppa_id", null=True, blank=True, related_name="clasificacion_energia_mensual_por_contrato_ppa_id")
    codigo_sic = models.CharField(max_length=32, null=True, blank=True)
    uso_del_recurso = models.BooleanField(default=False)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    calculado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "clasificacion_energia_mensual"


class RecProceso(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="rec_procesos_por_proyecto_id")
    codigo_proceso = models.CharField(max_length=100, null=True, blank=True)
    estado = models.CharField(max_length=14, choices=[("en_preparacion", "en_preparacion"), ("radicado", "radicado"), ("en_revision", "en_revision"), ("aprobado", "aprobado"), ("rechazado", "rechazado")], default="en_preparacion")
    fecha_apertura = models.DateField(null=True, blank=True)
    fecha_cierre = models.DateField(null=True, blank=True)
    cantidad_energia_mwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    periodo_inicio = models.DateField(null=True, blank=True)
    periodo_fin = models.DateField(null=True, blank=True)
    titular_nombre = models.CharField(max_length=255, null=True, blank=True)
    titular_nit = models.CharField(max_length=20, null=True, blank=True)
    relacion_propietario = models.CharField(max_length=13, choices=[("propietario", "propietario"), ("comprador_ppa", "comprador_ppa"), ("tercero", "tercero")], null=True, blank=True)
    tecnologia_generacion = models.CharField(max_length=100, null=True, blank=True)
    fuente_datos = models.CharField(max_length=20, choices=[("asic", "asic"), ("medidor", "medidor"), ("plataforma_monitoreo", "plataforma_monitoreo"), ("otro", "otro")], null=True, blank=True)
    ente_certificador = models.CharField(max_length=255, null=True, blank=True)
    numero_radicado = models.CharField(max_length=100, null=True, blank=True)
    observaciones_ente = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "rec_procesos"
