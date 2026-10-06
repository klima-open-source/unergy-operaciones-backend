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
from django.db.models import Q, F

from apps.contratos.services import grupos
from apps.plataforma.models import Timer

# ContratoServicio dejó de tener tabla propia (`contratos_servicio`): es una fachada
# proxy sobre la única tabla `contratos`. Se define al final del archivo, después de
# `Contrato`, porque un modelo proxy necesita su clase base ya definida.


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


class AlertaAniversario(Timer):
    """Libro de avisos de aniversario ya enviados, uno por ventana.

    NO es una tabla generada: nace acá (2026-09-10) para que
    `contratos.alertas_representacion` sea idempotente y tolere corridas
    perdidas. Antes el disparo era coincidencia exacta (`dias in (30, 15)`), sin
    registro de nada: una corrida perdida perdía el aviso para siempre y dos
    corridas el mismo día mandaban dos correos.

    La existencia de la fila ES la marca de "ya avisé"; no hay columna de
    estado. Se escribe DESPUÉS de que el correo salió bien, así que un SMTP
    caído no consume el aviso: la ventana sigue cruzada y la corrida siguiente
    reintenta.

    `aniversario` va en la llave, y no basta `(contrato, dias_aviso)` como en
    `alertas` (la de PPA): un PPA tiene una sola `fecha_fin`, pero un contrato
    de representación cumple aniversario todos los años. Sin la fecha en la
    llave, el aviso saldría una única vez en la vida del contrato.
    """

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE,
        db_column="contrato_id", related_name="alertas_aniversario",
    )
    # La fecha concreta del aniversario avisado, no el año: es la que distingue
    # un aniversario del siguiente.
    aniversario = models.DateField()
    # El umbral cruzado (30 o 15), no los días reales que faltaban: es lo que
    # identifica la ventana. Los días reales van en el texto del correo.
    dias_aviso = models.IntegerField()

    class Meta:
        db_table = "contrato_alertas_aniversario"
        unique_together = [("contrato", "aniversario", "dias_aviso")]


# =====================================================================================
# Contratos unificados — plan `docs/refactor/08-plan-django-contratos.md`
#
# Una sola tabla de contratos (`contratos`), su vínculo con plantas
# (`contrato_proyectos`) y qué servicios cubre cada uno (`servicios`).
#
# Fuera de este alcance, a propósito (decisión de Sara, 2026-10-06): las tarifas
# siguen en las columnas del contrato y en `ppa_tarifas`; las partes, en
# comprador/vendedor/contratante/prestador; el proyecto de un contrato de servicio,
# en su FK directa. `contrato_partes` y `contrato_tarifas` entran en sus propias
# ramas, ya con quién las escribe y quién las lee: una tabla llenada una sola vez
# por el backfill se desactualiza con la primera edición.
# =====================================================================================


class GrupoContrato(models.TextChoices):
    """A qué grupo de Servicios pertenece un contrato. Los valores son los de
    `apps.contratos.services.grupos` (la fuente única del catálogo); las fachadas
    `PpaContrato` y `ContratoServicio` filtran por este campo."""

    PPA = grupos.PPA, "PPA"
    REPRESENTACION_CGM = grupos.REPRESENTACION_CGM, "Representación y CGM"
    OPERACION = grupos.OPERACION, "Operación"


class ServicioContrato(models.TextChoices):
    """Los servicios (subservicios del catálogo de `grupos`) que cubre un contrato."""

    COMPRA = grupos.COMPRA, "Compra de energía"
    VENTA = grupos.VENTA, "Venta de energía"
    REPRESENTACION = grupos.REPRESENTACION, "Representación"
    CGM = grupos.CGM, "CGM"
    MANTENIMIENTO = grupos.MANTENIMIENTO, "Mantenimiento"
    ARRIENDO = grupos.ARRIENDO, "Arriendo"
    INTERNET = grupos.INTERNET, "Internet"


class EstadoContrato(models.TextChoices):
    """Estado que decide una persona (no derivado de fechas). Es el vocabulario de
    `contratos_servicio` — `vigente`/`vencido` NO están: se calculan en
    `apps.contratos.services.vigencia` (ver migración 0006). Los PPA no tenían columna
    de estado; se les pone `firmado` por defecto, que no altera ninguna lógica PPA."""

    FIRMADO = "firmado", "Firmado"
    EN_RENOVACION = "en_renovacion", "En renovación"
    TERMINADO = "terminado", "Terminado"


class PeriodicidadPago(models.TextChoices):
    MENSUAL = "mensual", "Mensual"
    BIMESTRAL = "bimestral", "Bimestral"
    TRIMESTRAL = "trimestral", "Trimestral"
    SEMESTRAL = "semestral", "Semestral"
    ANUAL = "anual", "Anual"


class Compraventa(models.TextChoices):
    """Sentido de un contrato de compraventa de energía (PPA)."""

    VENTA = "venta", "Venta"
    COMPRA = "compra", "Compra"


class Contrato(Timer):
    """Acuerdo entre partes sobre una o varias plantas — la ÚNICA tabla de contratos.

    Reemplaza a `ppa_contratos` y `contratos_servicio`: un PPA, un contrato de
    representación/CGM o uno de operación es una fila acá, y `grupo` dice cuál.
    Qué servicios cubre va en `servicios` (`contrato.servicios`); las plantas de un
    PPA, en `contrato_proyectos` (`contrato.proyectos_vinculados`).

    Es una tabla ANCHA y TRANSITORIA: lleva las columnas de las dos tablas viejas
    (nullable las que no aplican al grupo) para que las fachadas `PpaContrato` y
    `ContratoServicio` sigan sirviendo a sus lectores sin cambios. Las ramas de
    tarifas, clientes e inversionistas la van adelgazando (plan 08, §2)."""

    id = models.BigAutoField(primary_key=True)
    # Explícito, y no deducido de `servicio_aplica` vacío o lleno: un contrato con
    # ese campo mal puesto desaparecía de su pantalla o aparecía en la otra. El
    # CHECK `ck_contratos_grupo` lo mantiene coherente con `servicio_aplica`.
    grupo = models.CharField(max_length=20, choices=GrupoContrato.choices)
    estado = models.CharField(
        max_length=13, choices=EstadoContrato.choices, default=EstadoContrato.FIRMADO
    )
    numero_contrato = models.CharField(max_length=120, null=True, blank=True)
    # Identificador propio del PPA (contratos_servicio usa `numero_contrato`; los dos
    # coexisten porque una fila es de un tipo o del otro).
    numero_codigo_contrato = models.CharField(max_length=100, null=True, blank=True)
    nombre_interno = models.CharField(max_length=255, null=True, blank=True)
    fecha_firma_contrato = models.DateField(null=True, blank=True)
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)
    tarifa_base = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    periodicidad_pago = models.CharField(
        max_length=10, choices=PeriodicidadPago.choices, null=True, blank=True
    )
    indice_indexacion = models.CharField(max_length=60, null=True, blank=True)
    renovacion_automatica = models.BooleanField(default=False)

    # --- Partes por nombre/NIT, copiadas del cliente vinculado (`clientes.partes`) ---
    comprador_nombre = models.CharField(max_length=255, null=True, blank=True)
    comprador_nit = models.CharField(max_length=20, null=True, blank=True)
    vendedor_nombre = models.CharField(max_length=255, null=True, blank=True)
    vendedor_nit = models.CharField(max_length=20, null=True, blank=True)
    contratante_nombre = models.CharField(max_length=255, null=True, blank=True)
    contratante_nit = models.CharField(max_length=20, null=True, blank=True)
    prestador_nombre = models.CharField(max_length=255, null=True, blank=True)
    prestador_nit = models.CharField(max_length=20, null=True, blank=True)
    inversionista_nombre = models.CharField(max_length=255, null=True, blank=True)

    # --- Específicas de compraventa de energía (PPA), antes en ppa_contratos ---
    responsable = models.ForeignKey(
        "ppa.PpaResponsable", on_delete=models.SET_NULL, db_column="responsable_id",
        null=True, blank=True, related_name="contratos",
    )
    # venta | compra (antes ppa_contratos.tipo_contrato).
    tipo_contrato = models.CharField(
        max_length=20, choices=Compraventa.choices, null=True, blank=True,
    )
    comprador = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="comprador_id",
        null=True, blank=True, related_name="ppa_contratos_por_comprador_id",
    )
    vendedor = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="vendedor_id",
        null=True, blank=True, related_name="ppa_contratos_por_vendedor_id",
    )
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
    es_comunidad_energetica = models.BooleanField(null=True, blank=True)
    fecha_entrada_comunidad = models.DateField(null=True, blank=True)
    nombre_comunidad = models.CharField(max_length=255, null=True, blank=True)

    # --- Específicas de contratos de servicio, antes en contratos_servicio ---
    # El servicio "principal" de la fila (un solo valor). Lo que el contrato cubre de
    # verdad está en `servicios` (un contrato de representación + CGM tiene las dos);
    # esta columna se conserva para los ~15 filtros que la leen, y se escribe junto con
    # `servicios` desde un solo lugar del código (plan 08, §3).
    servicio_aplica = models.CharField(
        max_length=14,
        choices=[("representacion", "representacion"), ("cgm", "cgm"),
                 ("mantenimiento", "mantenimiento"), ("arriendo", "arriendo"),
                 ("internet", "internet")],
        null=True, blank=True,
    )
    contratante = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="contratante_id",
        null=True, blank=True, related_name="contratos_servicio_por_contratante_id",
    )
    prestador = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="prestador_id",
        null=True, blank=True, related_name="contratos_servicio_por_prestador_id",
    )
    inversionista = models.ForeignKey(
        "clientes.Cliente", on_delete=models.SET_NULL, db_column="inversionista_id",
        null=True, blank=True, related_name="contratos_servicio_por_inversionista_id",
    )
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id",
        null=True, blank=True, related_name="contratos_servicio_por_proyecto_id",
    )
    # Tarifas escalares e indexación JSON: siguen siendo LA tarifa hasta la rama de
    # tarifas, que las pasa a `tarifas` colgando de `servicios` (plan 08, §2).
    tarifa_admin = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    tarifa_cgm = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_representacion = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    tarifa_mensual = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    indexacion_anual = models.JSONField(null=True, blank=True)
    indexacion_mensual = models.JSONField(null=True, blank=True)
    indexacion_cgm = models.JSONField(null=True, blank=True)
    indexacion_representacion = models.JSONField(null=True, blank=True)
    cgm_codigo_sic = models.CharField(max_length=20, null=True, blank=True)
    fecha_inicio_om = models.DateField(null=True, blank=True)
    fecha_indexacion = models.DateField(null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    estado_pago = models.CharField(max_length=20, null=True, blank=True)
    portafolio = models.CharField(max_length=255, null=True, blank=True)
    codigo_sun_factory = models.CharField(max_length=50, null=True, blank=True)
    nombre_proyecto_ref = models.CharField(max_length=255, null=True, blank=True)
    ubicacion_lat = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    ubicacion_lng = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    # Conectividad (internet/Starlink)
    plan_datos_gb = models.CharField(max_length=50, null=True, blank=True)
    velocidad_mbps = models.IntegerField(null=True, blank=True)
    tipo_conexion = models.CharField(max_length=50, null=True, blank=True)
    linea_servicio = models.CharField(max_length=100, null=True, blank=True)
    id_router = models.CharField(max_length=100, null=True, blank=True)
    numero_kit = models.CharField(max_length=100, null=True, blank=True)
    latencia_ms = models.IntegerField(null=True, blank=True)
    wifi_seguridad = models.CharField(max_length=50, null=True, blank=True)
    wifi_password = models.CharField(max_length=100, null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "contratos"
        constraints = [
            models.CheckConstraint(
                name="ck_contratos_fechas",
                condition=(
                    Q(fecha_fin__isnull=True)
                    | Q(fecha_inicio__isnull=True)
                    | Q(fecha_fin__gte=F("fecha_inicio"))
                ),
            ),
            models.CheckConstraint(
                name="ck_contratos_tarifa",
                condition=Q(tarifa_base__isnull=True) | Q(tarifa_base__gte=0),
            ),
            # El grupo y `servicio_aplica` dicen lo mismo, o la fila no entra.
            models.CheckConstraint(
                name="ck_contratos_grupo",
                condition=(
                    (Q(grupo=GrupoContrato.PPA) & Q(servicio_aplica__isnull=True))
                    | (
                        Q(grupo=GrupoContrato.REPRESENTACION_CGM)
                        & Q(servicio_aplica__in=grupos.SUBSERVICIOS[grupos.REPRESENTACION_CGM])
                    )
                    | (
                        Q(grupo=GrupoContrato.OPERACION)
                        & Q(servicio_aplica__in=grupos.SUBSERVICIOS[grupos.OPERACION])
                    )
                ),
            ),
        ]


class Servicio(Timer):
    """Un servicio que cubre un contrato: una fila por contrato y por servicio.

    Un contrato de representación + CGM tiene dos filas; uno de mantenimiento, una;
    un PPA, una (`compra` o `venta`). Reemplaza a deducir el servicio por las
    tarifas cargadas (`grupos.subservicios_de`): ahora se registra.

    Cuelga del CONTRATO, no de `contrato_proyectos`: un contrato de servicio tiene
    un solo proyecto y la compra/venta de un PPA es del contrato entero. La rama de
    inversionistas lo mueve cuando `contrato_proyectos` apunte a
    `proyecto_inversionistas`; la de tarifas le cuelga `tarifas` (plan 08, §2)."""

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="servicios",
    )
    servicio = models.CharField(max_length=20, choices=ServicioContrato.choices)

    class Meta:
        db_table = "servicios"
        constraints = [
            models.UniqueConstraint(
                fields=["contrato", "servicio"], name="uq_servicios_contrato_servicio"
            ),
        ]


class ContratoProyecto(models.Model):
    """Qué plantas cubre un PPA (N:M). Reemplaza a `ppa_contrato_proyectos` con el
    mismo significado. El proyecto de un contrato de servicio sigue en su FK directa;
    unificarlo acá va con la rama de inversionistas (plan 08, §2)."""

    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="proyectos_vinculados",
    )
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id",
        related_name="contratos_ppa",
    )
    pk = models.CompositePrimaryKey("contrato_id", "proyecto_id")

    class Meta:
        db_table = "contrato_proyectos"


class ContratoServicioCorrespondencia(models.Model):
    """Id viejo (`contratos_servicio.id`) -> id nuevo (`contratos.id`).

    Los PPA conservan su id en `contratos`; los contratos de servicio reciben uno
    nuevo (los ids 1-36 chocan). Esta tabla la llena el backfill y la usa la
    migración del corte para reescribir las FK de los satélites de servicio
    (decisiones 1-3 del plan 08). Es una TABLA y no un script para poder
    auditarla después. Se borra en el deploy 3, con las tablas viejas."""

    id_viejo = models.BigIntegerField(primary_key=True)
    contrato = models.OneToOneField(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="correspondencia_servicio",
    )

    class Meta:
        db_table = "contratos_servicio_correspondencia"


class ContratoServicioManager(models.Manager):
    """Solo los contratos de servicio: todo `grupo` que no sea `ppa`."""

    def get_queryset(self):
        return super().get_queryset().exclude(grupo=GrupoContrato.PPA)


class ContratoServicio(Contrato):
    """Fachada de solo-servicio sobre la única tabla `contratos` (modelo PROXY).

    `contratos_servicio` dejó de existir: un contrato de servicio (representación, CGM,
    mantenimiento, arriendo, internet) es una fila de `contratos` con `servicio_aplica`
    no nulo. Conserva el nombre y la API de los lectores — servicio_aplica, tarifa_admin/
    cgm/representacion, indexacion_*, proyecto, contratante/prestador, estado, etc. son
    columnas de `contratos`. El `estado` guarda el valor de servicio
    (firmado/en_renovacion/terminado) tal cual, no el de PPA."""

    objects = ContratoServicioManager()

    def save(self, *args, **kwargs):
        # El grupo sale de `servicio_aplica`, siempre: quien crea o edita un contrato
        # de servicio no tiene que acordarse de él (y el CHECK rechaza un desacuerdo).
        self.grupo = grupos.grupo_de(self.servicio_aplica) or self.grupo
        super().save(*args, **kwargs)

    class Meta:
        proxy = True
        verbose_name = "Contrato de servicio"
