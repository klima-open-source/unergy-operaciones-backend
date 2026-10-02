"""Modelos del dominio `om`.

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

class OmIpcTasa(Timer):
    id = models.BigAutoField(primary_key=True)
    año = models.IntegerField()
    tasa = models.DecimalField(max_digits=8, decimal_places=6)
    confirmado = models.BooleanField(default=False)
    fuente = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = "om_ipc_tasas"
        unique_together = [("año",)]


class OmSeleccionMensual(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="om_seleccion_mensual_por_contrato_id")
    periodo = models.CharField(max_length=7, db_index=True)
    incluido = models.BooleanField(default=True)
    facturado = models.BooleanField(default=False)
    valor_manual = models.DecimalField(max_digits=14, decimal_places=0, null=True, blank=True)  # numeric(14,0): pesos sin centavos
    valor_facturado_congelado = models.DecimalField(max_digits=14, decimal_places=0, null=True, blank=True)  # numeric(14,0)
    motivo_exclusion = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        db_table = "om_seleccion_mensual"
        unique_together = [("contrato", "periodo")]


class OmFacturaMensual(models.Model):
    id = models.BigAutoField(primary_key=True)
    periodo = models.CharField(max_length=7, db_index=True)
    nombre_archivo = models.CharField(max_length=500, null=True, blank=True)
    enlace_pdf = models.CharField(max_length=2000, null=True, blank=True)
    ruta_local = models.CharField(max_length=1000, null=True, blank=True)
    subido_en = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "om_factura_mensual"


class OmPaginaSinMatch(models.Model):
    id = models.BigAutoField(primary_key=True)
    periodo = models.CharField(max_length=7, db_index=True)
    pagina = models.IntegerField()
    nombre_extraido = models.CharField(max_length=300, null=True, blank=True)
    estrategia = models.CharField(max_length=30, null=True, blank=True)
    razon = models.CharField(max_length=200)
    numero_factura = models.CharField(max_length=30, null=True, blank=True)
    muestra_texto = models.CharField(max_length=500, null=True, blank=True)
    origen = models.CharField(max_length=20, default="upload")
    resuelto = models.BooleanField(default=False)
    contrato_id_asignado = models.ForeignKey("contratos.Contrato", on_delete=models.DO_NOTHING, db_column="contrato_id_asignado", null=True, blank=True, related_name="om_pagina_sin_match_por_contrato_id_asignado")
    asignado_en = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "om_pagina_sin_match"
        unique_together = [("pagina", "periodo")]


class OmDocumentoProyecto(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="om_documento_proyecto_por_contrato_id")
    periodo = models.CharField(max_length=7, db_index=True)
    nombre_archivo = models.CharField(max_length=500)
    ruta_local = models.CharField(max_length=1000)
    numero_factura = models.CharField(max_length=30, null=True, blank=True)
    total_sin_impuestos = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    iva = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    total_pagar = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    fecha_facturacion = models.DateField(null=True, blank=True)
    cufe = models.CharField(max_length=200, null=True, blank=True)
    procesado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "om_documento_proyecto"
        unique_together = [("contrato", "periodo")]


class ProyectoInformeOm(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="proyecto_informe_om_por_proyecto_id")
    version = models.CharField(max_length=100, null=True, blank=True)
    elaborado_por = models.CharField(max_length=255, null=True, blank=True)
    actividad = models.CharField(max_length=255, null=True, blank=True)
    estado = models.CharField(max_length=11, choices=[("borrador", "borrador"), ("en_revision", "en_revision"), ("aprobado", "aprobado")], default="borrador")
    empresa_contratista = models.CharField(max_length=255, null=True, blank=True)
    fecha_energizacion = models.DateField(null=True, blank=True)
    fecha_inicio_operacion = models.DateField(null=True, blank=True)
    pendientes = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    checklist_fusion_solar = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    checklist_frontera = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    checklist_estacion_meteo = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    checklist_reconectador = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    objetivo_alcance = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    datos_generales = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    arquitectura_comunicacion = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    equipos = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    variables_monitoreadas = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    configuracion_monitoreo = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    protocolo_pruebas = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    eventos_operativos = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    observaciones = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    recomendaciones = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    conclusion = models.TextField(null=True, blank=True)
    firmas = models.JSONField(default=list)  # la base ya trae '[]'::jsonb
    evidencia_arquitectura = models.JSONField(default=list)  # la base ya trae '[]'::jsonb

    class Meta:
        db_table = "proyecto_informe_om"
