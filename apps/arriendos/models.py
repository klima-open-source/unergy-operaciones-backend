"""Modelos del dominio `arriendos`.

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

class ArrProyecto(Timer):
    id = models.BigAutoField(primary_key=True)
    codigo = models.CharField(max_length=120, null=True, blank=True)
    nombre = models.CharField(max_length=255)
    fecha_firma_contrato = models.DateField(null=True, blank=True)
    valor_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "arr_proyectos"
        unique_together = [("codigo",)]


class ArrArrendador(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="arr_arrendador_por_contrato_id")
    nombre = models.CharField(max_length=255)
    valor_base = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)
    anticipo_pagado_desde = models.DateField(null=True, blank=True)
    anticipo_pagado_hasta = models.DateField(null=True, blank=True)
    observaciones = models.CharField(max_length=1000, null=True, blank=True)

    class Meta:
        db_table = "arr_arrendador"


class ArrIpcTasa(Timer):
    id = models.BigAutoField(primary_key=True)
    año = models.IntegerField()
    tasa = models.DecimalField(max_digits=8, decimal_places=6)
    confirmado = models.BooleanField(default=False)
    fuente = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = "arr_ipc_tasas"
        unique_together = [("año",)]


class ArrDocumento(models.Model):
    id = models.BigAutoField(primary_key=True)
    arr_proyecto = models.ForeignKey("ArrProyecto", on_delete=models.CASCADE, db_column="arr_proyecto_id", null=True, blank=True, related_name="arr_documento_por_arr_proyecto_id")
    arr_arrendador = models.ForeignKey("ArrArrendador", on_delete=models.CASCADE, db_column="arr_arrendador_id", null=True, blank=True, related_name="arr_documento_por_arr_arrendador_id")
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", null=True, blank=True, related_name="arr_documento_por_proyecto_id")
    periodo = models.CharField(max_length=7, db_index=True)
    pago_id = models.IntegerField()
    codigo_contrato = models.CharField(max_length=120)
    tipo_documento = models.CharField(max_length=30)
    nombre_archivo = models.CharField(max_length=500)
    ruta_local = models.CharField(max_length=1000)
    ruta_original = models.CharField(max_length=1000, null=True, blank=True)
    nombre_secundario = models.CharField(max_length=500, null=True, blank=True)
    ruta_secundario = models.CharField(max_length=1000, null=True, blank=True)
    codigo_predio = models.CharField(max_length=120, null=True, blank=True)
    numero_cuenta_cobro = models.CharField(max_length=60, null=True, blank=True)
    nombre_arrendatario = models.CharField(max_length=255, null=True, blank=True)
    valor_individual = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    fecha_subida = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "arr_documento"
        unique_together = [("arr_arrendador", "arr_proyecto", "pago_id", "periodo")]


class ArrSeleccionMensual(Timer):
    id = models.BigAutoField(primary_key=True)
    arr_proyecto = models.ForeignKey("ArrProyecto", on_delete=models.CASCADE, db_column="arr_proyecto_id", null=True, blank=True, related_name="arr_seleccion_mensual_por_arr_proyecto_id")
    arr_arrendador = models.ForeignKey("ArrArrendador", on_delete=models.CASCADE, db_column="arr_arrendador_id", null=True, blank=True, related_name="arr_seleccion_mensual_por_arr_arrendador_id")
    periodo = models.CharField(max_length=7, db_index=True)
    incluido = models.BooleanField(default=True)
    facturado = models.BooleanField(default=False)
    valor_facturado_congelado = models.BigIntegerField(null=True, blank=True)
    motivo_exclusion = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        db_table = "arr_seleccion_mensual"
        unique_together = [("arr_arrendador", "periodo")]
