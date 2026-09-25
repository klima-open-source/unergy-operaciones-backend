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

from apps.contratos.models import Contrato, ContratoProyecto
from apps.plataforma.models import Timer


class PpaContratoManager(models.Manager):
    """Solo las filas de compraventa de energía: las que NO tienen servicio_aplica.

    `servicio_aplica` lo llenan solo los contratos de servicio; un PPA lo deja nulo,
    así que es el discriminador entre las dos fachadas sobre `contratos`."""

    def get_queryset(self):
        return super().get_queryset().filter(servicio_aplica__isnull=True)


class PpaContrato(Contrato):
    """Fachada de solo-PPA sobre la única tabla `contratos` (modelo PROXY).

    `ppa_contratos` dejó de existir: un contrato PPA es una fila de `contratos` con
    `servicio_aplica` nulo. Conserva el nombre y la API de los lectores — todas las
    columnas PPA (numero_codigo_contrato, tipo_contrato, valor_indexacion_base,
    comprador, …) son columnas de `contratos`, así que se acceden y se filtran igual.
    Las tarifas mensuales están en `PpaTarifa` (`contrato.tarifas_ppa`), los
    compromisos en `PpaCompromisoEnergia` (`contrato.compromisos`)."""

    objects = PpaContratoManager()

    class Meta:
        proxy = True
        verbose_name = "Contrato PPA"


class PpaResponsable(Timer):
    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=120)
    incluir_en_cumplimiento = models.BooleanField(default=True)

    class Meta:
        db_table = "ppa_responsables"
        unique_together = [("nombre",)]


class PpaTarifa(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="tarifas_ppa")
    año = models.IntegerField()
    mes = models.IntegerField()
    tarifa = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "ppa_tarifas"
        unique_together = [("año", "contrato", "mes")]


class PpaCompromisoEnergia(models.Model):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id", related_name="compromisos")
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


class PpaContratoProyectoManager(models.Manager):
    """Solo los vínculos de contratos PPA (servicio_aplica nulo). `contrato_proyectos`
    ahora mezcla PPA y servicio; este filtro conserva la semántica vieja de
    `ppa_contrato_proyectos`, que era solo-PPA."""

    def get_queryset(self):
        return super().get_queryset().filter(contrato__servicio_aplica__isnull=True)


class PpaContratoProyecto(ContratoProyecto):
    """Fachada proxy sobre `ContratoProyecto` (tabla `contrato_proyectos`).

    `ppa_contrato_proyectos` dejó de existir: los vínculos PPA↔planta viven en
    `contrato_proyectos` junto con los de servicio. Conserva el nombre y la semántica
    solo-PPA para los lectores."""

    objects = PpaContratoProyectoManager()

    class Meta:
        proxy = True
