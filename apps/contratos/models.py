"""Modelos del dominio `contratos`.

GENERADO por scripts/generar_modelos_django.py desde los metadatos de
SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los
TextChoices de las columnas de estado y los docstrings que explican el
modelo de datos. Revisar antes de portar la API del recurso.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.
"""

from django.db import models

from apps.plataforma.models import Timer

class ContratoServicio(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", null=True, blank=True, related_name="contratos_servicio_por_proyecto_id")
    numero_contrato = models.CharField(max_length=100, null=True, blank=True)
    servicio_aplica = models.CharField(max_length=14, choices=[("representacion", "representacion"), ("cgm", "cgm"), ("mantenimiento", "mantenimiento"), ("arriendo", "arriendo"), ("internet", "internet")])
    contratante_nombre = models.CharField(max_length=255, null=True, blank=True)
    contratante_nit = models.CharField(max_length=20, null=True, blank=True)
    prestador_nombre = models.CharField(max_length=255, null=True, blank=True)
    prestador_nit = models.CharField(max_length=20, null=True, blank=True)
    contratante = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="contratante_id", null=True, blank=True, related_name="contratos_servicio_por_contratante_id")
    prestador = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="prestador_id", null=True, blank=True, related_name="contratos_servicio_por_prestador_id")
    inversionista = models.ForeignKey("clientes.Cliente", on_delete=models.SET_NULL, db_column="inversionista_id", null=True, blank=True, related_name="contratos_servicio_por_inversionista_id")
    cgm_codigo_sic = models.CharField(max_length=20, null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    periodicidad_pago = models.CharField(max_length=10, choices=[("mensual", "mensual"), ("bimestral", "bimestral"), ("trimestral", "trimestral"), ("semestral", "semestral"), ("anual", "anual")], null=True, blank=True)
    indice_indexacion = models.CharField(max_length=50, null=True, blank=True)
    estado = models.CharField(max_length=13, choices=[("vigente", "vigente"), ("vencido", "vencido"), ("terminado", "terminado"), ("en_renovacion", "en_renovacion")], default="vigente")
    fecha_firma_contrato = models.DateField(null=True, blank=True)
    fecha_inicio_om = models.DateField(null=True, blank=True)
    renovacion_automatica = models.BooleanField(null=True, blank=True)
    fecha_indexacion = models.DateField(null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    estado_pago = models.CharField(max_length=20, null=True, blank=True)
    plan_datos_gb = models.CharField(max_length=50, null=True, blank=True)
    velocidad_mbps = models.IntegerField(null=True, blank=True)
    tipo_conexion = models.CharField(max_length=50, null=True, blank=True)
    linea_servicio = models.CharField(max_length=100, null=True, blank=True)
    id_router = models.CharField(max_length=100, null=True, blank=True)
    numero_kit = models.CharField(max_length=100, null=True, blank=True)
    latencia_ms = models.IntegerField(null=True, blank=True)
    wifi_seguridad = models.CharField(max_length=50, null=True, blank=True)
    wifi_password = models.CharField(max_length=100, null=True, blank=True)
    ubicacion_lat = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    ubicacion_lng = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_mensual = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    indexacion_anual = models.JSONField(null=True, blank=True)
    indexacion_mensual = models.JSONField(null=True, blank=True)
    inversionista_nombre = models.CharField(max_length=255, null=True, blank=True)
    portafolio = models.CharField(max_length=255, null=True, blank=True)
    codigo_sun_factory = models.CharField(max_length=50, null=True, blank=True)
    nombre_proyecto_ref = models.CharField(max_length=255, null=True, blank=True)
    tarifa_admin = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    tarifa_cgm = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_representacion = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    indexacion_cgm = models.JSONField(null=True, blank=True)
    indexacion_representacion = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "contratos_servicio"


class PagoServicio(Timer):
    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey("ContratoServicio", on_delete=models.CASCADE, db_column="contrato_id", related_name="pagos_servicio_por_contrato_id")
    mes = models.IntegerField()
    año = models.IntegerField()
    valor_pagado = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    estado = models.CharField(max_length=9, choices=[("pendiente", "pendiente"), ("revisado", "revisado"), ("aprobado", "aprobado")], default="pendiente")
    enlace_factura = models.CharField(max_length=1000, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "pagos_servicio"
        unique_together = [("año", "contrato", "mes")]


class Poliza(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="polizas")
    numero_poliza = models.CharField(max_length=100, null=True, blank=True)
    poliza_om = models.BooleanField(default=False)
    fecha_vencimiento = models.DateField(null=True, blank=True, db_index=True)
    valor_poliza = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    mano_obra = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    estructura = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    paneles = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    inversores = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    otros = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_total_proyecto = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    link_estudio_suelos = models.CharField(max_length=500, null=True, blank=True)
    ipp_base = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ipp_base_fecha = models.DateField(null=True, blank=True)
    ipp_provisional = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ipp_provisional_fecha = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    generacion_anual_p90_kwh = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    valor_lucro_cesante = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "polizas"
