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
# siguen en las columnas del contrato y en `ppa_tarifas`; el proyecto de un contrato
# de servicio, en su FK directa. `contrato_tarifas` entra en su propia rama, ya con
# quién la escribe y quién la lee: una tabla llenada una sola vez por el backfill se
# desactualiza con la primera edición. Las partes llegaron así (`ContratoParte`,
# rama `contratos-partes`, 2026-10-07).
# =====================================================================================


#: Marca de "este atajo no se tocó" (None es un valor válido: quitar la planta).
_SIN_CAMBIO = object()

#: Los atajos de tarifa de `Contrato` (no son columnas: salen de `tarifas`).
ATAJOS_DE_TARIFA = frozenset({
    "tarifa_base", "tarifa_representacion", "tarifa_cgm", "tarifa_admin", "tarifa_anual",
    "tarifa_mensual",
    "indexacion_representacion", "indexacion_cgm", "indexacion_anual", "indexacion_mensual",
})

#: Los atajos de planta e inversionista de `Contrato` (no son columnas).
ATAJOS_DE_PLANTA = frozenset({
    "proyecto", "proyecto_id", "inversionista", "inversionista_id", "inversionista_nombre",
})


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
    # Cargo adicional, no subservicio (`grupos.CARGOS`).
    ADMINISTRACION = grupos.ADMINISTRACION, "Administración"


class TipoIndexacion(models.TextChoices):
    """Con qué índice se actualiza la tarifa de un servicio. Vacío = no se indexa
    (la administración, un PPA de precio fijo)."""

    IPC = "ipc", "IPC"
    IPP = "ipp", "IPP"


class RolParte(models.TextChoices):
    """El papel de un cliente en un contrato. Los mismos cuatro para todos los
    contratos (decisión de Sara, 2026-10-07); la pantalla los nombra según el tipo
    (en un arriendo, el prestador es el arrendador y el contratante el arrendatario).
    El inversionista NO es una parte: es de quién es la planta, y va a
    `contrato_proyectos` en la rama de inversionistas."""

    COMPRADOR = "comprador", "Comprador"
    VENDEDOR = "vendedor", "Vendedor"
    CONTRATANTE = "contratante", "Contratante"
    PRESTADOR = "prestador", "Prestador"


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
    periodicidad_pago = models.CharField(
        max_length=10, choices=PeriodicidadPago.choices, null=True, blank=True
    )
    indice_indexacion = models.CharField(max_length=60, null=True, blank=True)
    renovacion_automatica = models.BooleanField(default=False)

    # Las partes (comprador, vendedor, contratante, prestador) viven en
    # `contrato_partes`; su nombre y NIT, en la ficha del cliente. Los atajos de
    # lectura con los nombres de siempre están al final de la clase. El inversionista
    # (y la planta) salen de `contrato_proyectos`: ver sus atajos más abajo.

    # --- Específicas de compraventa de energía (PPA), antes en ppa_contratos ---
    # venta | compra (antes ppa_contratos.tipo_contrato).
    tipo_contrato = models.CharField(
        max_length=20, choices=Compraventa.choices, null=True, blank=True,
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
    # La planta y el inversionista ya no son columnas que Django lea: viven en
    # `contrato_proyectos` (planta y, en representación, la participación). Ver los
    # atajos `proyecto` / `inversionista` más abajo. Las columnas `proyecto_id`,
    # `inversionista_id` e `inversionista_nombre` siguen en la base, sin escribirse,
    # hasta `contratos/0023` (paso 4c).
    # Las tarifas viven en el servicio (base e índice) y en `tarifas` (valor por
    # periodo): `tarifa_base`, `tarifa_representacion`, `tarifa_cgm`, `tarifa_admin`,
    # `tarifa_mensual` y los cuatro `indexacion_*` son atajos (ver `ATAJOS_DE_TARIFA`),
    # igual que `tarifa_anual` (la base de mantenimiento y arriendo). Las columnas
    # siguen en la base, sin escribirse, hasta que el paso 3 de tarifas las borre.
    fecha_inicio_om = models.DateField(null=True, blank=True)
    fecha_indexacion = models.DateField(null=True, blank=True)
    responsable_iva = models.BooleanField(default=False)
    estado_pago = models.CharField(max_length=20, null=True, blank=True)
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

    # --- Vínculo con originabot (TSF). Solo PPA -----------------------------------
    # Operaciones crea y configura el PPA; originabot (`investment.PPA`) tiene que
    # saber qué minigranjas cubre. Ver apps/ppa/services/originabot_sync.py.
    originabot_ppa_id = models.IntegerField(null=True, blank=True)
    # `sincronizado` | `pendiente` (falta token de Unergy Auth o el off taker) | `error`
    originabot_sync_estado = models.CharField(max_length=20, null=True, blank=True)
    originabot_sync_detalle = models.TextField(null=True, blank=True)
    originabot_sync_at = models.DateTimeField(null=True, blank=True)
    # Lo que falta llevar a originabot si la última sincronización no pudo:
    # {"agregar": [proyecto_id, …], "quitar": [proyecto_id, …]}.
    originabot_pendiente = models.JSONField(null=True, blank=True)
    # Arriendo: el contrato de arrendamiento (`LeasingContract`) de TSF del que
    # sale este. Lo escribe «Sincronizar con TSF»; es la llave para no duplicarlo.
    originabot_arriendo_id = models.IntegerField(null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True)

    # --- Las partes: atajos con los nombres de siempre --------------------------
    #
    # `contrato.comprador_nombre`, `contrato.contratante_id`… se siguen leyendo igual,
    # pero salen de `contrato_partes` y de la ficha del cliente. Para no hacer una
    # consulta por contrato en un listado: `.prefetch_related(contrato_partes.CON_PARTES)`.
    #
    # Escribir `contrato.comprador_id = 5` (o `comprador = cliente`) deja la parte
    # pendiente y `save()` la guarda en `contrato_partes`. El nombre y el NIT NO se
    # escriben: son los de la ficha. Asignarlos da error a propósito.

    def _partes_por_rol(self) -> dict:
        cache = self.__dict__.get("_partes_cache")
        if cache is None:
            filas = getattr(self, "_prefetched_objects_cache", {}).get("partes")
            if filas is None:
                filas = (self.partes.select_related("cliente").order_by("id")
                         if self.pk else [])
            cache = {}
            for fila in filas:
                cache.setdefault(fila.rol, fila.cliente)
            self.__dict__["_partes_cache"] = cache
        return cache

    def _parte(self, rol):
        pendientes = self.__dict__.get("_partes_pendientes", {})
        if rol in pendientes:
            return pendientes[rol]
        return self._partes_por_rol().get(rol)

    def _poner_parte(self, rol, cliente) -> None:
        self.__dict__.setdefault("_partes_pendientes", {})[rol] = cliente

    # --- La planta y el inversionista: atajos sobre `contrato_proyectos` -----------
    #
    # `contrato.proyecto_id`, `contrato.inversionista`… se leen igual que cuando eran
    # columnas, pero salen de la fila de `contrato_proyectos` de un contrato de
    # servicio (una sola) y de su participación. Para un listado:
    # `.prefetch_related(contrato_proyectos.CON_PLANTA)`. En un PPA valen None: sus
    # plantas son `proyectos_vinculados`, varias.
    #
    # Escribirlos deja el valor pendiente y `save()` lo lleva a `contrato_proyectos`.
    # El inversionista tiene que participar en la planta (`InversionistaFueraDeLaPlanta`).
    # `inversionista_nombre` es el de la ficha del cliente: asignarlo no hace nada.

    def _vinculo(self):
        """La fila de `contrato_proyectos` de este contrato de servicio, o None."""
        if "_vinculo_cache" not in self.__dict__:
            filas = getattr(self, "_prefetched_objects_cache", {}).get("proyectos_vinculados")
            if filas is None:
                filas = (self.proyectos_vinculados.select_related(
                    "proyecto", "proyecto_inversionista__inversionista__cliente",
                ) if self.pk and self.grupo != GrupoContrato.PPA else [])
            filas = list(filas)
            self.__dict__["_vinculo_cache"] = (
                filas[0] if len(filas) == 1 and self.grupo != GrupoContrato.PPA else None
            )
        return self.__dict__["_vinculo_cache"]

    def _pendiente(self, clave):
        return self.__dict__.get("_planta_pendiente", {}).get(clave, _SIN_CAMBIO)

    def _poner_pendiente(self, clave, valor) -> None:
        self.__dict__.setdefault("_planta_pendiente", {})[clave] = valor

    @property
    def proyecto_id(self):
        valor = self._pendiente("proyecto_id")
        if valor is not _SIN_CAMBIO:
            return valor
        vinculo = self._vinculo()
        return vinculo.proyecto_id if vinculo else None

    @proyecto_id.setter
    def proyecto_id(self, valor):
        self._poner_pendiente("proyecto_id", int(valor) if valor not in (None, "") else None)

    @property
    def proyecto(self):
        pid = self.proyecto_id
        if pid is None:
            return None
        vinculo = self._vinculo()
        if vinculo is not None and vinculo.proyecto_id == pid:
            return vinculo.proyecto
        from apps.proyectos.models import Proyecto

        return Proyecto.objects.filter(pk=pid).first()

    @proyecto.setter
    def proyecto(self, proyecto):
        self.proyecto_id = proyecto.pk if proyecto is not None else None

    @property
    def inversionista_id(self):
        valor = self._pendiente("inversionista_id")
        if valor is not _SIN_CAMBIO:
            return valor
        vinculo = self._vinculo()
        participacion = vinculo.proyecto_inversionista if vinculo else None
        return participacion.inversionista.cliente_id if participacion else None

    @inversionista_id.setter
    def inversionista_id(self, valor):
        self._poner_pendiente("inversionista_id", int(valor) if valor not in (None, "") else None)

    @property
    def inversionista(self):
        cid = self.inversionista_id
        if cid is None:
            return None
        vinculo = self._vinculo()
        participacion = vinculo.proyecto_inversionista if vinculo else None
        if participacion is not None and participacion.inversionista.cliente_id == cid:
            return participacion.inversionista.cliente
        from apps.clientes.models import Cliente

        return Cliente.objects.filter(pk=cid).first()

    @inversionista.setter
    def inversionista(self, cliente):
        self.inversionista_id = cliente.pk if cliente is not None else None

    @property
    def inversionista_nombre(self):
        cliente = self.inversionista
        return cliente.razon_social_nombre if cliente else None

    @inversionista_nombre.setter
    def inversionista_nombre(self, valor):
        pass  # sale de la ficha del cliente; los llamadores viejos lo siguen asignando

    def save(self, *args, **kwargs):
        # Los atajos de las partes, la planta y el inversionista no son columnas:
        # fuera de `update_fields`. Si solo se tocaron atajos, la lista queda vacía y
        # Django no escribe la fila.
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = [
                f for f in kwargs["update_fields"]
                if f not in _ATAJOS_DE_PARTES and f not in ATAJOS_DE_PLANTA
                and f not in ATAJOS_DE_TARIFA
            ]
        from apps.contratos.services import contrato_partes, contrato_proyectos, servicios, tarifas

        if self.grupo == GrupoContrato.REPRESENTACION_CGM:
            inversionista = self._pendiente("inversionista_id")
            if inversionista not in (_SIN_CAMBIO, None):
                contrato_proyectos.validar_inversionista(self.proyecto_id, inversionista)
        super().save(*args, **kwargs)
        # Qué servicios cubre, desde el ÚNICO lugar que los escribe: así ningún
        # camino de escritura (API, CRM, admin) puede dejarlos desalineados con
        # `tipo_contrato` o `servicio_aplica`.
        servicios.registrar(self)
        tarifas.aplicar(self)
        pendiente = self.__dict__.pop("_planta_pendiente", {})
        contrato_proyectos.sincronizar(
            self,
            proyecto_id=pendiente.get("proyecto_id", _SIN_CAMBIO),
            inversionista_id=pendiente.get("inversionista_id", _SIN_CAMBIO),
        )
        self.__dict__.pop("_vinculo_cache", None)
        pendientes = self.__dict__.pop("_partes_pendientes", None)
        if pendientes:
            contrato_partes.asignar(self, pendientes)

    def refresh_from_db(self, *args, **kwargs):
        for cache in ("_vinculo_cache", "_planta_pendiente", "_partes_cache", "_partes_pendientes",
                      "_tarifas_pendientes"):
            self.__dict__.pop(cache, None)
        super().refresh_from_db(*args, **kwargs)

    class Meta:
        db_table = "contratos"
        constraints = [
            # Un PPA de originabot corresponde a un solo contrato vivo: dos que
            # apuntaran al mismo se pisarían las minigranjas.
            models.UniqueConstraint(
                fields=["originabot_arriendo_id"],
                condition=Q(originabot_arriendo_id__isnull=False),
                name="uq_contratos_originabot_arriendo",
            ),
            models.UniqueConstraint(
                fields=["originabot_ppa_id"],
                condition=Q(originabot_ppa_id__isnull=False, deleted_at__isnull=True),
                name="uq_contratos_originabot_ppa_vivo",
            ),
            models.CheckConstraint(
                name="ck_contratos_fechas",
                condition=(
                    Q(fecha_fin__isnull=True)
                    | Q(fecha_inicio__isnull=True)
                    | Q(fecha_fin__gte=F("fecha_inicio"))
                ),
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


def _atajo_de_parte(rol: str, campo: str | None):
    """`comprador` / `comprador_id` / `comprador_nombre` / `comprador_nit`, de una parte."""

    def leer(self):
        cliente = self._parte(rol)
        if campo is None or cliente is None:
            return cliente
        return getattr(cliente, campo)

    if campo == "razon_social_nombre" or campo == "nit_cedula":
        return property(leer)  # de la ficha: no se escribe aquí

    def escribir(self, valor):
        if campo is None or valor is None:
            self._poner_parte(rol, valor)
        else:
            from apps.clientes.models import Cliente

            self._poner_parte(rol, Cliente.objects.get(pk=valor))

    return property(leer, escribir)


#: Los nombres de siempre de las partes, ahora atajos sobre `contrato_partes`.
_ATAJOS_DE_PARTES = set()
for _rol in RolParte.values:
    for _sufijo, _campo in (("", None), ("_id", "id"),
                            ("_nombre", "razon_social_nombre"), ("_nit", "nit_cedula")):
        setattr(Contrato, f"{_rol}{_sufijo}", _atajo_de_parte(_rol, _campo))
        _ATAJOS_DE_PARTES.add(f"{_rol}{_sufijo}")


def _atajo_de_tarifa(nombre: str):
    """`contrato.tarifa_cgm`, `contrato.indexacion_representacion`…, sobre el servicio
    y sus `tarifas`.

    Escribirlo deja el valor pendiente y `save()` lo lleva a su servicio
    (`services/tarifas.py::aplicar`)."""

    def leer(self):
        pendientes = self.__dict__.get("_tarifas_pendientes") or {}
        if nombre in pendientes:
            return pendientes[nombre]
        from apps.contratos.services import tarifas

        return tarifas.leer(self, nombre)

    def escribir(self, valor):
        self.__dict__.setdefault("_tarifas_pendientes", {})[nombre] = valor

    return property(leer, escribir)


for _nombre in ATAJOS_DE_TARIFA:
    setattr(Contrato, _nombre, _atajo_de_tarifa(_nombre))


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
    # Esquema de Sara (2026-10-14): la base y el índice son del servicio, no de cada
    # periodo. La unidad la da el servicio (`services/tarifas.py::UNIDAD`).
    tarifa_base = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    tipo_indexacion = models.CharField(
        max_length=3, choices=TipoIndexacion.choices, null=True, blank=True,
    )

    class Meta:
        db_table = "servicios"
        constraints = [
            models.UniqueConstraint(
                fields=["contrato", "servicio"], name="uq_servicios_contrato_servicio"
            ),
            models.CheckConstraint(
                name="ck_servicios_tarifa_base",
                condition=Q(tarifa_base__isnull=True) | Q(tarifa_base__gte=0),
            ),
        ]


class Tarifa(Timer):
    """El valor de un servicio en cada periodo: una fila por año, o por año y mes.

    Esquema de Sara (2026-10-14): `servicio`, `anio`, `mes`, `valor`. La base pactada
    y el índice con que se actualiza están en el servicio (`Servicio.tarifa_base`,
    `Servicio.tipo_indexacion`); aquí va lo que vale cada periodo.

    `mes` vacío: el servicio se cobra por año (representación, CGM, internet…). Lleno:
    el precio cambia mes a mes (los PPA, que antes vivían en `ppa_tarifas`). El
    índice aplicado en un periodo no se guarda: sale de comparar con el anterior."""

    id = models.BigAutoField(primary_key=True)
    servicio = models.ForeignKey(
        "contratos.Servicio", on_delete=models.CASCADE, db_column="servicio_id",
        related_name="tarifas",
    )
    anio = models.IntegerField()
    mes = models.IntegerField(null=True, blank=True)
    valor = models.DecimalField(max_digits=18, decimal_places=6)

    class Meta:
        db_table = "tarifas"
        ordering = ["servicio_id", "anio", models.F("mes").asc(nulls_first=True)]
        constraints = [
            # Un valor por periodo. Dos índices parciales: en un UNIQUE con `mes`, dos
            # filas del mismo año sin mes no chocarían (los NULL son distintos).
            models.UniqueConstraint(
                fields=["servicio", "anio"], condition=Q(mes__isnull=True),
                name="uq_tarifas_servicio_anio",
            ),
            models.UniqueConstraint(
                fields=["servicio", "anio", "mes"], condition=Q(mes__isnull=False),
                name="uq_tarifas_servicio_anio_mes",
            ),
            models.CheckConstraint(
                name="ck_tarifas_mes", condition=Q(mes__isnull=True) | Q(mes__range=(1, 12)),
            ),
            models.CheckConstraint(name="ck_tarifas_valor", condition=Q(valor__gte=0)),
        ]


class ContratoParte(Timer):
    """Un cliente y el papel que juega en un contrato: una fila por parte.

    La misma tabla para todos los contratos — PPA, representación/CGM, operación,
    arriendo—; lo que cambia por tipo es qué papeles lleva. Un contrato puede tener
    dos filas del mismo papel (un terreno con varios dueños), así que la unicidad es
    por contrato, papel y cliente.

    El nombre y el NIT se leen siempre de la ficha del cliente: aquí no se copian.

    Hoy la escribe solo `Contrato.save()`, derivándola de las columnas
    comprador/vendedor/contratante/prestador (`services/contrato_partes.py`); la leen
    las consultas de «en qué contratos participa este cliente». Que las pantallas
    escriban aquí directamente, y quitar esas columnas, es el paso siguiente."""

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="partes",
    )
    cliente = models.ForeignKey(
        "clientes.Cliente", on_delete=models.CASCADE, db_column="cliente_id",
        related_name="partes_de_contratos",
    )
    rol = models.CharField(max_length=20, choices=RolParte.choices)

    class Meta:
        db_table = "contrato_partes"
        constraints = [
            models.UniqueConstraint(
                fields=["contrato", "rol", "cliente"], name="uq_contrato_partes",
            ),
        ]
        indexes = [models.Index(fields=["cliente", "rol"], name="ix_contrato_partes_cliente")]


class ContratoProyecto(models.Model):
    """Qué planta cubre un contrato, y en representación, qué participación.

    Una fila por contrato y planta, para TODOS los contratos: los PPA (que pueden
    cubrir varias) y los de servicio (una). En un contrato de representación,
    `proyecto_inversionista` dice a qué inversionista de esa planta representamos;
    en los demás va vacío: quién está en el contrato son sus partes
    (`contrato_partes`), no los inversionistas (Sara, 2026-10-13).

    La base exige que la participación sea de la misma planta: llave compuesta
    `(proyecto_inversionista_id, proyecto_id)` → `proyecto_inversionistas (id,
    proyecto_id)`, diferida al COMMIT para que la fusión de proyectos pueda mover
    las dos tablas en la misma transacción (`contratos/0017`).

    Mientras convivan con `contratos.proyecto_id` e `inversionista_id`, las filas
    de los contratos de servicio las deriva `Contrato.save()`
    (`services/contrato_proyectos.py`)."""

    id = models.BigAutoField(primary_key=True)
    contrato = models.ForeignKey(
        "contratos.Contrato", on_delete=models.CASCADE, db_column="contrato_id",
        related_name="proyectos_vinculados",
    )
    proyecto = models.ForeignKey(
        "proyectos.Proyecto", on_delete=models.CASCADE, db_column="proyecto_id",
        related_name="contratos_ppa",
    )
    proyecto_inversionista = models.ForeignKey(
        # Borrar la participación deja el contrato pendiente de asignar, no lo bloquea.
        "proyectos.ProyectoInversionista", on_delete=models.SET_NULL,
        db_column="proyecto_inversionista_id", null=True, blank=True,
        related_name="contratos_vinculados",
    )

    class Meta:
        db_table = "contrato_proyectos"
        constraints = [
            models.UniqueConstraint(fields=["contrato", "proyecto"], name="uq_contrato_proyectos"),
        ]


#: Columnas de `contratos` que solo tienen sentido en un PPA (venían de
#: `ppa_contratos`). Los serializers de `ContratoServicio` las excluyen para que su
#: API siga teniendo exactamente los campos de antes de la tabla única: con
#: `exclude` y la tabla ancha se colaban en la lectura y, peor, en la escritura.
COLUMNAS_SOLO_PPA = (
    "numero_codigo_contrato", "nombre_interno", "tipo_contrato",
    "periodicidad_indexacion", "periodo_indexacion_base",
    "valor_indexacion_base", "cantidad_minima_kwh_mes", "cantidad_maxima_kwh_mes",
    "periodicidad_facturacion", "tiempo_pago", "condiciones_pago", "gescon_codigo",
    "gescon_fecha_inicio", "gescon_fecha_fin", "gescon_precio",
    "gescon_cantidades_kwh", "codigo_sic", "es_comunidad_energetica",
    "fecha_entrada_comunidad", "nombre_comunidad", "deleted_at",
)


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
