"""Modelos del dominio `ppa`.

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

class PpaContrato(Timer):
    id = models.BigAutoField(primary_key=True)
    numero_codigo_contrato = models.CharField(max_length=100, null=True, blank=True)
    nombre_interno = models.CharField(max_length=200, null=True, blank=True)
    responsable = models.ForeignKey("PpaResponsable", on_delete=models.SET_NULL, db_column="responsable_id", null=True, blank=True, related_name="contratos")
    comprador = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="comprador_id", null=True, blank=True, related_name="ppa_contratos_por_comprador_id")
    vendedor = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="vendedor_id", null=True, blank=True, related_name="ppa_contratos_por_vendedor_id")
    comprador_nombre = models.CharField(max_length=255, null=True, blank=True)
    comprador_nit = models.CharField(max_length=20, null=True, blank=True)
    vendedor_nombre = models.CharField(max_length=255, null=True, blank=True)
    vendedor_nit = models.CharField(max_length=20, null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    indice_indexacion = models.CharField(max_length=50, null=True, blank=True)
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
    tipo_contrato = models.CharField(max_length=20, null=True, blank=True, default="venta")
    renovacion_automatica = models.BooleanField(null=True, blank=True)
    es_comunidad_energetica = models.BooleanField(null=True, blank=True)
    # Desde cuándo la planta está en la comunidad, que NO es cuándo arranca el
    # PPA. Marca el corte a partir del cual dejan de prestarse representación y
    # CGM; ver `apps.contratos.services.grupos.presta_servicio`.
    fecha_entrada_comunidad = models.DateField(null=True, blank=True)
    # El nombre vive acá y no en la planta: cinco plantas de la misma
    # comunidad guardaban cinco copias del mismo texto.
    nombre_comunidad = models.CharField(max_length=255, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ppa_contratos"


class PpaResponsable(Timer):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=120)
    incluir_en_cumplimiento = models.BooleanField(default=True)

    class Meta:
        db_table = "ppa_responsables"
        unique_together = [("nombre",)]


class PpaTarifa(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("PpaContrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="tarifas")
    año = models.IntegerField()
    mes = models.IntegerField()
    tarifa = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "ppa_tarifas"
        unique_together = [("año", "contrato", "mes")]


class PpaCompromisoEnergia(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("PpaContrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="compromisos")
    año = models.IntegerField()
    mes = models.IntegerField()
    energia_minima = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    energia_maxima = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    cantidad_proyectos = models.IntegerField(null=True, blank=True, default=0)

    class Meta:
        db_table = "ppa_compromisos_energia"
        unique_together = [("año", "contrato", "mes")]


class IppMensual(models.Model):
    id = models.BigAutoField(primary_key=True)
    año = models.IntegerField()
    mes = models.IntegerField()
    valor = models.DecimalField(max_digits=12, decimal_places=4)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "ipp_mensual"
        unique_together = [("año", "mes")]


class PpaContratoProyecto(models.Model):
    contrato = models.ForeignKey("PpaContrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="proyectos_vinculados")
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="contratos_ppa")
    pk = models.CompositePrimaryKey("contrato_id", "proyecto_id")

    class Meta:
        db_table = "ppa_contrato_proyectos"
