"""Modelos del dominio `mandatos`.

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

class MandatoCorreo(models.Model):
    id = models.BigAutoField(primary_key=True)
    message_id = models.CharField(max_length=998)
    fecha = models.DateTimeField(db_index=True)
    remitente = models.CharField(max_length=255)
    asunto = models.CharField(max_length=1000, null=True, blank=True)
    fuente = models.CharField(max_length=20)
    clasificacion = models.CharField(max_length=20)
    resultado = models.CharField(max_length=20)
    requiere_revision = models.BooleanField(db_index=True, default=False)
    detalle = models.JSONField(default=dict)  # la base ya trae '{}'::jsonb
    revertido = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "mandato_correos"
        unique_together = [("message_id",)]


class FinanzasMandato(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.CharField(max_length=255)
    tercero = models.CharField(max_length=255, default="")
    periodo = models.DateField()
    tipo = models.CharField(max_length=7, choices=[("ingreso", "ingreso"), ("costo", "costo")])
    cmu = models.CharField(max_length=20, null=True, blank=True)
    cmu_anterior = models.CharField(max_length=20, null=True, blank=True)
    estado = models.CharField(max_length=21, choices=[("sin_firma", "sin_firma"), ("firmado", "firmado"), ("con_comentarios", "con_comentarios"), ("corregido", "corregido"), ("enviado_inversionista", "enviado_inversionista")], default="sin_firma")
    comentario = models.TextField(null=True, blank=True)
    fecha_envio = models.DateField(null=True, blank=True)
    fecha_firma = models.DateField(null=True, blank=True)
    fecha_envio_inversionista = models.DateField(null=True, blank=True)
    drive_file_id = models.CharField(max_length=255, null=True, blank=True)
    drive_url = models.CharField(max_length=1000, null=True, blank=True)
    correo_ref = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        db_table = "finanzas_mandatos"
        unique_together = [("periodo", "proyecto", "tercero", "tipo")]


class LiquidacionMandato(Timer):
    id = models.BigAutoField(primary_key=True)
    liquidacion = models.ForeignKey("liquidaciones.Liquidacion", on_delete=models.DO_NOTHING, db_column="liquidacion_id", related_name="mandatos")
    inversionista = models.ForeignKey("proyectos.ProyectoInversionista", on_delete=models.DO_NOTHING, db_column="inversionista_id", null=True, blank=True, related_name="liquidacion_mandatos_por_inversionista_id")
    tipo = models.CharField(max_length=8, choices=[("ingresos", "ingresos"), ("costos", "costos")])
    numero_mandato = models.CharField(max_length=50, null=True, blank=True)
    consecutivo = models.IntegerField(null=True, blank=True)
    beneficiario_nombre = models.CharField(max_length=255, null=True, blank=True)
    beneficiario_nit = models.CharField(max_length=20, null=True, blank=True)
    periodo_inicio = models.DateField(null=True, blank=True)
    periodo_fin = models.DateField(null=True, blank=True)
    estado = models.CharField(max_length=17, choices=[("borrador", "borrador"), ("enviado_revisoria", "enviado_revisoria"), ("firmado", "firmado"), ("entregado", "entregado")], default="borrador")
    fecha_generacion = models.DateField(null=True, blank=True)
    fecha_envio_revisoria = models.DateField(null=True, blank=True)
    fecha_firma = models.DateField(null=True, blank=True)
    pa_aplica = models.BooleanField(default=False)
    categoria_contable = models.CharField(max_length=50, null=True, blank=True)
    total_ingresos_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_costos_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_retenciones_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    total_iva_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    valor_neto_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    observaciones = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "liquidacion_mandatos"


class LiquidacionMandatoLinea(Timer):
    id = models.BigAutoField(primary_key=True)
    mandato = models.ForeignKey("LiquidacionMandato", on_delete=models.DO_NOTHING, db_column="mandato_id", related_name="lineas")
    tipo_linea = models.CharField(max_length=26, choices=[("ingreso_bruto", "ingreso_bruto"), ("ajuste_xm", "ajuste_xm"), ("ajuste_unergy", "ajuste_unergy"), ("ajuste_comercializacion", "ajuste_comercializacion"), ("intereses", "intereses"), ("otro_ingreso", "otro_ingreso"), ("despacho", "despacho"), ("ventas_en_bolsa", "ventas_en_bolsa"), ("compras_en_bolsa", "compras_en_bolsa"), ("redistribucion_ingresos", "redistribucion_ingresos"), ("mantenimiento", "mantenimiento"), ("arriendo", "arriendo"), ("servicio_internet", "servicio_internet"), ("poliza_cumplimiento", "poliza_cumplimiento"), ("servicios_publicos_consumo", "servicios_publicos_consumo"), ("cambio_equipos_medida", "cambio_equipos_medida"), ("seguro", "seguro"), ("otro_costo", "otro_costo"), ("comercializacion", "comercializacion"), ("representacion", "representacion"), ("cgm", "cgm"), ("administracion", "administracion"), ("iva", "iva"), ("retencion_fuente", "retencion_fuente"), ("reteica", "reteica"), ("ica_opex", "ica_opex"), ("otro_impuesto", "otro_impuesto"), ("porcentaje_participacion", "porcentaje_participacion"), ("valor_a_pagar", "valor_a_pagar")])
    concepto = models.CharField(max_length=500)
    valor_cop = models.DecimalField(max_digits=18, decimal_places=2)
    porcentaje = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    base_calculo_cop = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    referencia_factura = models.CharField(max_length=255, null=True, blank=True)
    soporte_url = models.CharField(max_length=1000, null=True, blank=True)
    orden = models.IntegerField(default=0)

    class Meta:
        db_table = "liquidacion_mandato_lineas"
