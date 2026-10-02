"""Modelos del dominio `facturacion`.

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

class FacturaAgrupacion(models.Model):
    id = models.BigAutoField(primary_key=True)
    codigo_sic_contrato = models.CharField(max_length=40, null=True, blank=True)  # la base la acepta nula
    nombre = models.CharField(max_length=120)
    porcentaje = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "factura_agrupacion"
        unique_together = [("codigo_sic_contrato",)]


class FacturaOrden(models.Model):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=120)
    orden = models.IntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "factura_orden"
        unique_together = [("nombre",)]


class FacturaEmitida(models.Model):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=120)
    periodo = models.CharField(max_length=7)
    numero_factura = models.CharField(max_length=80, null=True, blank=True)
    emitida_por = models.CharField(max_length=120, null=True, blank=True)
    emitida_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "factura_emitida"
        unique_together = [("nombre", "periodo")]


class ContratoFactura(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="contrato_factura_por_contrato_id")
    tipo = models.CharField(max_length=13, choices=[("solenium", "solenium"), ("inversionista", "inversionista")])
    fecha = models.CharField(max_length=7)
    inversionista = models.CharField(max_length=255, null=True, blank=True)
    numero_factura = models.CharField(max_length=100, null=True, blank=True)
    monto = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    enlace_soporte = models.CharField(max_length=1000, null=True, blank=True)

    class Meta:
        db_table = "contrato_factura"
