"""Modelos del dominio `garantias`.

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

class GarCalculo(models.Model):
    id = models.BigAutoField(primary_key=True)
    agente = models.CharField(max_length=10)
    esquema = models.CharField(max_length=10)
    fecha_vencimiento = models.DateField(db_index=True)
    fecha_calculo = models.DateField(null=True, blank=True)
    periodo_ini = models.DateField()
    periodo_fin = models.DateField()
    etiqueta_periodo = models.CharField(max_length=40, null=True, blank=True)
    base_30d_ini = models.DateField(null=True, blank=True)
    base_30d_fin = models.DateField(null=True, blank=True)
    base_sem_ini = models.DateField(null=True, blank=True)
    base_sem_fin = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "gar_calculo"
        unique_together = [("agente", "esquema", "fecha_vencimiento", "periodo_fin", "periodo_ini")]


class GarComponenteReal(models.Model):
    id = models.BigAutoField(primary_key=True)
    calculo = models.ForeignKey("GarCalculo", on_delete=models.CASCADE, db_column="calculo_id", related_name="reales")
    componente = models.CharField(max_length=80)
    valor = models.DecimalField(max_digits=22, decimal_places=2)

    class Meta:
        db_table = "gar_componente_real"
        unique_together = [("calculo", "componente")]


class GarComponentePred(models.Model):
    id = models.BigAutoField(primary_key=True)
    calculo = models.ForeignKey("GarCalculo", on_delete=models.CASCADE, db_column="calculo_id", related_name="predicciones")
    componente = models.CharField(max_length=80)
    horizonte_dias = models.IntegerField()
    cuantil = models.DecimalField(max_digits=4, decimal_places=3)
    valor = models.DecimalField(max_digits=22, decimal_places=2)
    modelo_version = models.CharField(max_length=40)
    calculado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "gar_componente_pred"
        unique_together = [("calculo", "componente", "cuantil", "horizonte_dias", "modelo_version")]


class XmArchivo(models.Model):
    id = models.BigAutoField(primary_key=True)
    tipo = models.CharField(max_length=30, db_index=True)
    nombre_archivo = models.CharField(max_length=300)
    version = models.CharField(max_length=10, null=True, blank=True)
    periodo_ini = models.DateField(null=True, blank=True)
    periodo_fin = models.DateField(null=True, blank=True)
    disponible_desde = models.DateTimeField()
    origen_disponibilidad = models.CharField(max_length=12)
    sha256 = models.CharField(max_length=64)
    bytes_len = models.IntegerField(default=0)
    filas_ingeridas = models.IntegerField(default=0)
    esquema_ok = models.BooleanField(default=True)
    esquema_detalle = models.JSONField(null=True, blank=True)
    ingerido_en = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "xm_archivo"
        unique_together = [("sha256",)]


class XmMedida(models.Model):
    id = models.BigAutoField(primary_key=True)
    archivo = models.ForeignKey("XmArchivo", on_delete=models.PROTECT, db_column="archivo_id", related_name="xm_medida_por_archivo_id")
    tipo = models.CharField(max_length=30)
    fecha_documento = models.DateField()
    hora = models.IntegerField(default=0)
    entidad = models.CharField(max_length=60)
    concepto = models.CharField(max_length=120)
    concepto_raw = models.CharField(max_length=200, null=True, blank=True)
    valor = models.DecimalField(max_digits=22, decimal_places=6)
    version = models.CharField(max_length=10, null=True, blank=True)

    class Meta:
        db_table = "xm_medida"
        unique_together = [("concepto", "entidad", "fecha_documento", "hora", "tipo", "version")]


class GarantiaAjuste(Timer):
    id = models.BigAutoField(primary_key=True)
    tipo = models.CharField(max_length=7, choices=[("semanal", "semanal"), ("txr", "txr"), ("mensual", "mensual")])
    fecha = models.DateField(db_index=True)
    pb = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    restricciones = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    stn = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    trm = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    ptb = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_ungc = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_ungg = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_consignar = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    disponible_custodia = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    congelado = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    saldo = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_ajuste_txr = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    snapshot = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "garantias_ajustes"


class GarantiaSnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    fecha_corte = models.DateField(db_index=True)
    clave = models.CharField(max_length=30)
    anio = models.IntegerField()
    mes = models.IntegerField()
    neto_mwh = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    precio_bolsa = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    valor_energia = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    valor_plantas_nuevas = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    costo_regulatorio = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    garantia_total = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    plantas_nuevas = models.IntegerField(default=0)
    kwh_planta_nueva = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    regulatorio_anio = models.IntegerField(null=True, blank=True)
    regulatorio_mes = models.IntegerField(null=True, blank=True)
    regulatorio_fallback = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "garantia_snapshot"


class GarantiaPagado(models.Model):
    id = models.BigAutoField(primary_key=True)
    anio = models.IntegerField()
    mes = models.IntegerField()
    valor = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "garantia_pagado"
        unique_together = [("anio", "mes")]


class GarantiaContrato(models.Model):
    """Reparto de la garantía de un corte entre los contratos que la generan.

    La garantía la causa el DÉFICIT horario (compra en bolsa por los mínimos de
    ciertos contratos). Esta tabla guarda, por corte y ventana (anio/mes), cuánto
    de la garantía total le toca a cada contrato según su déficit —para poder
    cobrárselo al cliente. La produce `apps.garantias.services.atribucion`.
    """
    id = models.BigAutoField(primary_key=True)
    fecha_corte = models.DateField(db_index=True)
    anio = models.IntegerField()
    mes = models.IntegerField()
    codigo = models.CharField(max_length=40)
    contrato = models.CharField(max_length=200, null=True, blank=True)
    # Nombre del comprador (razón social completa del resumen de Cumplimiento),
    # no el código SIC: puede ser largo ("... S.A.S. E.S.P.").
    comprador = models.CharField(max_length=120, null=True, blank=True)
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id",
        null=True, blank=True, related_name="garantia_contrato_por_proyecto_id",
    )
    deficit_mwh = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    pct = models.DecimalField(max_digits=9, decimal_places=6, default=0)
    monto = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_garantia = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "garantia_contrato"
        unique_together = [("fecha_corte", "anio", "mes", "codigo")]


class BalcttosNeto(models.Model):
    id = models.BigAutoField(primary_key=True)
    anio = models.IntegerField()
    mes = models.IntegerField()
    dia_corte = models.IntegerField(default=0)
    neto_mwh = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "balcttos_neto"
        unique_together = [("anio", "mes")]
