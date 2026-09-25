"""Modelos del dominio `fronteras`.

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

class Frontera(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="fronteras")
    codigo_frontera = models.CharField(max_length=50, null=True, blank=True)
    nombre_frontera = models.CharField(max_length=255)
    tipo_frontera = models.CharField(max_length=18, choices=[("generacion", "generacion"), ("consumo", "consumo"), ("generacion_consumo", "generacion_consumo"), ("consumo_auxiliar", "consumo_auxiliar"), ("consumo_propio", "consumo_propio")])
    estado = models.CharField(max_length=11, choices=[("activa", "activa"), ("en_registro", "en_registro"), ("cancelada", "cancelada"), ("en_falla", "en_falla")], default="en_registro")
    fecha_registro_asic = models.DateField(null=True, blank=True)
    tipo_punto_medicion = models.IntegerField(null=True, blank=True)
    nivel_tension_kv = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    clase_ct = models.CharField(max_length=4, choices=[("0.2", "0.2"), ("0.2s", "0.2s"), ("0.5s", "0.5s")], null=True, blank=True)
    clase_pt = models.CharField(max_length=3, choices=[("0.2", "0.2"), ("0.5", "0.5")], null=True, blank=True)
    nivel_tension = models.IntegerField(null=True, blank=True)
    transferencia_maxima_kwh = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fecha_inicio_representacion = models.DateField(null=True, blank=True)
    operador_red = models.ForeignKey("OperadorRed", on_delete=models.DO_NOTHING, db_column="operador_red_id", null=True, blank=True, related_name="fronteras_por_operador_red_id")
    agente_exportador = models.CharField(max_length=255, null=True, blank=True)
    agente_importador = models.CharField(max_length=255, null=True, blank=True)
    codigo_sic_submercado_exportador = models.CharField(max_length=20, null=True, blank=True)
    codigo_sic_submercado_consumo = models.CharField(max_length=20, null=True, blank=True)
    nro_serie_med_ppal = models.CharField(max_length=100, null=True, blank=True)
    marca_med_ppal = models.CharField(max_length=100, null=True, blank=True)
    modelo_med_ppal = models.CharField(max_length=100, null=True, blank=True)
    clase_medidor = models.CharField(max_length=4, choices=[("0.2s", "0.2s"), ("0.5s", "0.5s")], null=True, blank=True)
    num_elementos_med_ppal = models.IntegerField(null=True, blank=True)
    fecha_cambio_med_ppal = models.DateField(null=True, blank=True)
    entidad_calibradora_med_ppal = models.CharField(max_length=255, null=True, blank=True)
    fecha_calibracion_med_ppal = models.DateField(null=True, blank=True)
    fecha_actualizacion_ppal = models.DateField(null=True, blank=True)
    nro_serie_med_resp = models.CharField(max_length=100, null=True, blank=True)
    marca_med_resp = models.CharField(max_length=100, null=True, blank=True)
    modelo_med_resp = models.CharField(max_length=100, null=True, blank=True)
    num_elementos_med_resp = models.IntegerField(null=True, blank=True)
    fecha_cambio_med_resp = models.DateField(null=True, blank=True)
    entidad_calibradora_med_resp = models.CharField(max_length=255, null=True, blank=True)
    fecha_calibracion_med_resp = models.DateField(null=True, blank=True)
    fecha_actualizacion_resp = models.DateField(null=True, blank=True)
    quoia_border_id = models.IntegerField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "fronteras"


class FronteraQuoiaIgnorada(models.Model):
    id = models.BigAutoField(primary_key=True)
    frt_code = models.CharField(max_length=50)
    ignorado_por_usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="ignorado_por_usuario_id", null=True, blank=True, related_name="fronteras_quoia_ignoradas_por_ignorado_por_usuario_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "fronteras_quoia_ignoradas"
        unique_together = [("frt_code",)]


class ContratoFrontera(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato_servicio = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_servicio_id", related_name="contrato_frontera_por_contrato_servicio_id")
    frontera = models.ForeignKey("Frontera", on_delete=models.CASCADE, db_column="frontera_id", related_name="contrato_frontera_por_frontera_id")

    class Meta:
        db_table = "contrato_frontera"
        unique_together = [("contrato_servicio", "frontera")]


class OperadorRed(Timer):
    id = models.BigAutoField(primary_key=True)
    nombre_legal = models.CharField(max_length=255)
    nombre_comercial = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = "operadores_red"
        unique_together = [("nombre_legal",)]


class OperadorRedContacto(models.Model):
    id = models.BigAutoField(primary_key=True)
    operador_red = models.ForeignKey("OperadorRed", on_delete=models.CASCADE, db_column="operador_red_id", related_name="contactos")
    email = models.CharField(max_length=255)
    nombre = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "operadores_red_contactos"
