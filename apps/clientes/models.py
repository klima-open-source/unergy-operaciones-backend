"""Modelos del dominio `clientes`.

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

class Cliente(Timer):
    id = models.BigAutoField(primary_key=True)
    razon_social_nombre = models.CharField(max_length=255)
    nit_cedula = models.CharField(max_length=20, null=True, blank=True)
    tipo_persona = models.CharField(max_length=8, choices=[("natural", "natural"), ("juridica", "juridica")], null=True, blank=True)
    representante_legal = models.CharField(max_length=255, null=True, blank=True)
    direccion = models.CharField(max_length=500, null=True, blank=True)
    ciudad = models.CharField(max_length=100, null=True, blank=True)
    departamento = models.CharField(max_length=100, null=True, blank=True)
    iva_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    retencion_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reteica_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reteiva_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    origen_tipo = models.CharField(max_length=30, null=True, blank=True)
    origen_detalle = models.CharField(max_length=255, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "clientes"
        unique_together = [("nit_cedula",)]


class ClienteDocumentoComercial(Timer):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey("Cliente", on_delete=models.DO_NOTHING, db_column="cliente_id", null=True, blank=True, related_name="documentos_comerciales")
    contrato_servicio = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_servicio_id", null=True, blank=True, related_name="cliente_documentos_comerciales_por_contrato_servicio_id")
    ppa_contrato = models.ForeignKey("contratos.Contrato", on_delete=models.CASCADE, db_column="ppa_contrato_id", null=True, blank=True, related_name="documentos_comerciales")
    tipo = models.CharField(max_length=20, choices=[("rut", "rut"), ("cedula_ciudadania", "cedula_ciudadania"), ("certificado_bancario", "certificado_bancario"), ("camara_comercio", "camara_comercio"), ("oferta", "oferta"), ("contrato", "contrato")])
    nombre = models.CharField(max_length=255)
    numero = models.CharField(max_length=100, null=True, blank=True)
    fecha = models.DateField(null=True, blank=True)
    estado = models.CharField(max_length=9, choices=[("borrador", "borrador"), ("enviado", "enviado"), ("aceptado", "aceptado"), ("firmado", "firmado"), ("rechazado", "rechazado")], default="borrador")
    archivo_url = models.CharField(max_length=1000, null=True, blank=True)
    archivo_nombre = models.CharField(max_length=500, null=True, blank=True)
    oportunidad = models.ForeignKey("comercial.Oportunidad", on_delete=models.DO_NOTHING, db_column="oportunidad_id", null=True, blank=True, related_name="cliente_documentos_comerciales_por_oportunidad_id")
    notas = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "cliente_documentos_comerciales"


class ClienteTasaServicio(Timer):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey("Cliente", on_delete=models.CASCADE, db_column="cliente_id", related_name="tasas_servicio")
    servicio = models.CharField(max_length=30)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", null=True, blank=True, related_name="cliente_tasa_servicio_por_proyecto_id")
    iva_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    retencion_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reteiva_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    reteica_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "cliente_tasa_servicio"
        unique_together = [("cliente", "proyecto", "servicio")]


class Contacto(Timer):
    id = models.BigAutoField(primary_key=True)
    cliente = models.ForeignKey("Cliente", on_delete=models.DO_NOTHING, db_column="cliente_id", related_name="contactos")
    nombre = models.CharField(max_length=255, null=True, blank=True)
    email = models.CharField(max_length=255)
    telefono = models.CharField(max_length=100, null=True, blank=True)
    tipo = models.CharField(max_length=11, choices=[("operacional", "operacional"), ("cgm", "cgm"), ("liquidacion", "liquidacion"), ("comercial", "comercial"), ("contable", "contable")])
    recibe_notificaciones = models.BooleanField(default=True)

    class Meta:
        db_table = "contactos"
        unique_together = [("cliente", "email", "tipo")]


class ProyectoAreaContacto(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="area_contactos")
    tipo = models.CharField(max_length=11, choices=[("operacional", "operacional"), ("cgm", "cgm"), ("liquidacion", "liquidacion"), ("comercial", "comercial"), ("contable", "contable")])
    cliente = models.ForeignKey("Cliente", on_delete=models.DO_NOTHING, db_column="cliente_id", related_name="proyecto_area_contacto_por_cliente_id")

    class Meta:
        db_table = "proyecto_area_contacto"
        unique_together = [("proyecto", "tipo")]
