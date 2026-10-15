"""Modelos del dominio `proyectos`.

GENERADO por scripts/generar_modelos_django.py desde los metadatos de
SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los
TextChoices de las columnas de estado y los docstrings que explican el
modelo de datos. Revisar antes de portar la API del recurso.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.
"""

from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from apps.plataforma.models import Timer

class Proyecto(Timer):
    id = models.BigAutoField(primary_key=True)
    portafolio = models.ForeignKey("Portafolio", on_delete=models.SET_NULL, db_column="portafolio_id", null=True, blank=True, related_name="proyectos")
    nombre_comercial = models.CharField(max_length=255)
    sub_project = models.CharField(max_length=50, null=True, blank=True)
    topico_liquidaciones = models.CharField(max_length=100, null=True, blank=True)
    clasificacion_regulatoria = models.CharField(max_length=4, choices=[("AGP", "AGP"), ("AGPE", "AGPE"), ("AGGE", "AGGE"), ("GD", "GD"), ("DER", "DER"), ("otra", "otra")], null=True, blank=True)
    tipo_tecnologia = models.CharField(max_length=10, choices=[("solar", "solar"), ("eolica", "eolica"), ("hidraulica", "hidraulica"), ("biomasa", "biomasa"), ("otra", "otra")], null=True, blank=True)
    tipo_proyecto = models.CharField(max_length=19, choices=[("minigranja", "minigranja"), ("autoconsumo", "autoconsumo"), ("gd", "gd"), ("movilidad_electrica", "movilidad_electrica"), ("otro", "otro")], null=True, blank=True)
    # Potencia AC. Se llamó `potencia_instalada_kwp` hasta el 2026-09-10: el
    # nombre decía kWp (pico, DC) y el dato siempre fue AC, lo que costó dos
    # bugs de corrupción ya documentados (la edición manual copiaba acá la
    # capacidad DC; el backfill de Solenium hacía lo mismo). Es la ÚNICA
    # potencia AC del sistema: `proyecto_info_tecnica.potencia_ac_kw` guardaba
    # una copia espejada y se eliminó en la misma migración.
    potencia_ac_kw = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    potencia_con_cen_mw = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    produccion_especifica_kwh_kwp = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    codigo_cnd = models.CharField(max_length=50, null=True, blank=True)
    estado = models.CharField(max_length=13, choices=[("en_desarrollo", "en_desarrollo"), ("en_operacion", "en_operacion"), ("suspendido", "suspendido"), ("cancelado", "cancelado")], default="en_desarrollo")
    fecha_entrada_operacion = models.DateField(null=True, blank=True)
    fecha_fin_representacion = models.DateField(null=True, blank=True)
    fecha_inicio_comercializacion = models.DateField(null=True, blank=True)
    fecha_comercializacion_editada_manual = models.BooleanField(default=False)
    gen_mensual_promedio_mwh = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    gen_promedio_origen = models.CharField(max_length=10, null=True, blank=True)
    gen_promedio_dias = models.IntegerField(null=True, blank=True)
    gen_promedio_desde = models.DateField(null=True, blank=True)
    gen_promedio_hasta = models.DateField(null=True, blank=True)
    gen_promedio_actualizado_en = models.DateTimeField(null=True, blank=True)
    departamento = models.CharField(max_length=100, null=True, blank=True)
    municipio = models.CharField(max_length=100, null=True, blank=True)
    direccion_vereda = models.CharField(max_length=500, null=True, blank=True)
    latitud = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitud = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    altitud_msnm = models.IntegerField(null=True, blank=True)
    operador_red = models.ForeignKey("fronteras.OperadorRed", on_delete=models.DO_NOTHING, db_column="operador_red_id", null=True, blank=True, related_name="proyectos_por_operador_red_id")
    project_id_solenium = models.CharField(max_length=100, null=True, blank=True)
    project_id_solarview = models.CharField(max_length=100, null=True, blank=True)
    srv_operacion = models.BooleanField(default=False)
    srv_representacion = models.BooleanField(default=False)
    srv_cgm = models.BooleanField(default=False)
    srv_ppa = models.BooleanField(default=False)
    p90_mensual_kwh = models.JSONField(null=True, blank=True)
    p50_mensual_kwh = models.JSONField(null=True, blank=True)
    p99_mensual_kwh = models.JSONField(null=True, blank=True)
    codigo_tsf = models.CharField(max_length=100, null=True, blank=True)
    origina_code = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    sunfactory_project_id = models.IntegerField(null=True, blank=True)
    # La minigranja de originabot (`investment.Minifarm.id`). La guarda «importar
    # desde TSF»; para los demás se resuelve por `origina_code` la primera vez que
    # hace falta (sincronizar las plantas de un PPA).
    originabot_minifarm_id = models.IntegerField(null=True, blank=True)
    # El proyecto y el terreno de esa minigranja en originabot. Se guardan la
    # primera vez que hacen falta (traer las cuentas de cobro de arriendos, que
    # originabot lista por terreno) para no pedir la minigranja de cada planta en
    # cada consulta. Ver `apps/arriendos/services/cuentas_cobro_originabot.py`.
    originabot_proyecto_id = models.IntegerField(null=True, blank=True)
    originabot_terreno_id = models.IntegerField(null=True, blank=True)
    # ECS (Estudio de Conexión Simplificado): el insumo principal del punto de
    # conexión en TSF (servicio `requests`, expuesto por originabot en
    # /api/connection/supplies-info/). Lo escribe «Sincronizar con TSF» desde la
    # ficha del proyecto. Los enlaces son los que da TSF: si son firmados, caducan.
    ecs_estado = models.CharField(max_length=100, null=True, blank=True)
    ecs_codigo_insumo = models.CharField(max_length=50, null=True, blank=True)
    ecs_vencimiento = models.DateField(null=True, blank=True)
    ecs_archivo_url = models.TextField(null=True, blank=True)
    ecs_conformidad_url = models.TextField(null=True, blank=True)
    fase_construccion = models.CharField(max_length=40, null=True, blank=True)
    fecha_estimada_energizacion = models.DateField(null=True, blank=True)
    avance_obra_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    origen = models.CharField(max_length=20, null=True, blank=True, default="manual")
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "proyectos"

        # OJO: esto estaba escrito como CUATRO asignaciones seguidas a
        # `unique_together` (sub_project, project_id_solarview,
        # sunfactory_project_id y project_id_solenium). En Python eso no las
        # acumula: cada una pisa a la anterior y solo sobrevive la ultima. Las
        # tres primeras parecian declaradas y no lo estaban.
        #
        # **La base SI las tiene** -- verificado contra produccion el
        # 2026-09-15: `proyectos_sub_project_key`,
        # `uq_proyectos_project_id_solarview` e
        # `ix_proyectos_sunfactory_project_id` existen como indices unicos.
        # Django adopto un esquema que ya venia con ellos, asi que el modelo
        # miente pero los datos estan protegidos.
        #
        # No se agregan al modelo a proposito: Django no sabe que ya existen y
        # generaria operaciones para CREARLAS de nuevo, dejando dos
        # restricciones redundantes sobre la misma columna. Adoptarlas bien es
        # una migracion aparte, con `state_operations`, y no es urgente.
        unique_together = [("project_id_solenium",)]

        constraints = [
            # Los dos codigos de Sun Factory, sin proteccion hasta ahora. Es el
            # hueco por el que paso el duplicado de "Astrea 1 (Calipso)" (ids
            # 274 y 275, mismo codigo_tsf): la sincronizacion cruza por estos
            # campos para no duplicar, pero nada impedia que se crearan dos
            # filas con el mismo valor. Detectarlo despues no es protegerlo.
            #
            # `deleted_at IS NULL` en la condicion: un merge deja al perdedor
            # con soft-delete, y su codigo tiene que quedar libre. Sin eso, el
            # proyecto borrado seguiria ocupando el codigo para siempre.
            #
            # `codigo_tsf` lo escribe una PERSONA al crear un proyecto a mano
            # (esta en CAMPOS_CREACION del serializer); `origina_code` llega de
            # Sun Factory como su `base_name`. Los dos identifican la misma
            # planta y los dos se usan para emparejar.
            models.UniqueConstraint(
                fields=["codigo_tsf"],
                condition=models.Q(codigo_tsf__isnull=False, deleted_at__isnull=True),
                name="uq_proyectos_codigo_tsf_vivo",
            ),
            models.UniqueConstraint(
                fields=["origina_code"],
                condition=models.Q(origina_code__isnull=False, deleted_at__isnull=True),
                name="uq_proyectos_origina_code_vivo",
            ),
            models.UniqueConstraint(
                fields=["originabot_minifarm_id"],
                condition=models.Q(originabot_minifarm_id__isnull=False, deleted_at__isnull=True),
                name="uq_proyectos_originabot_minifarm_vivo",
            ),
        ]


class Portafolio(Timer):
    """Una agrupación de plantas (para los informes «Por portafolio»).

    Un proyecto está en UN portafolio: `proyectos.portafolio_id` (Sara, 15-oct,
    después de probar varios por planta). La tabla `portafolio_proyectos` de ese
    intento queda en la base, fuera del modelo, hasta que `volver_a_portafolio_unico`
    copie sus asignaciones a la columna; luego se borra.
    """

    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=255)

    class Meta:
        db_table = "portafolios"
        unique_together = [("nombre",)]


class ProyectoInfoTecnica(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="info_tecnica")
    voltaje_red = models.CharField(max_length=50, null=True, blank=True)
    # La potencia AC NO vive acá: es `Proyecto.potencia_ac_kw`. Esta tabla tenía
    # una copia (`potencia_ac_kw`) que se mantenía espejada a mano en dos
    # lugares y coincidía en 101 de 101 proyectos -- se eliminó el 2026-09-10.
    # Lo que sí es de acá es la capacidad PICO (DC), que es otro dato.
    capacidad_instalada_kwp = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    tipo_tracker = models.CharField(max_length=2, choices=[("1P", "1P"), ("2P", "2P")], null=True, blank=True)
    cantidad_total_paneles = models.IntegerField(null=True, blank=True)
    potencia_panel_kwp = models.CharField(max_length=100, null=True, blank=True)
    marca_paneles = models.CharField(max_length=255, null=True, blank=True)
    cantidad_inversores = models.IntegerField(null=True, blank=True)
    potencia_inversores_kwp = models.CharField(max_length=100, null=True, blank=True)
    marca_inversores = models.CharField(max_length=255, null=True, blank=True)
    cantidad_strings = models.IntegerField(null=True, blank=True)
    marca_transformador = models.CharField(max_length=255, null=True, blank=True)
    marca_reconectador_rele = models.CharField(max_length=500, null=True, blank=True)
    marca_totalizador = models.CharField(max_length=255, null=True, blank=True)
    marca_seguidor_solar = models.CharField(max_length=255, null=True, blank=True)
    marca_medidores_frontera = models.CharField(max_length=255, null=True, blank=True)
    marca_modem_reconectador = models.CharField(max_length=500, null=True, blank=True)
    marca_modems_frontera = models.CharField(max_length=255, null=True, blank=True)
    ip_modem_reconectador = models.CharField(max_length=100, null=True, blank=True)
    url_ubicacion = models.TextField(null=True, blank=True)
    cctv_estado = models.TextField(null=True, blank=True)
    marca_cctv = models.CharField(max_length=255, null=True, blank=True)
    seguridad_fisica = models.CharField(max_length=255, null=True, blank=True)
    tiene_internet = models.BooleanField(null=True, blank=True)

    class Meta:
        db_table = "proyecto_info_tecnica"


class ProyectoInversor(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="inversores")
    nombre = models.CharField(max_length=120, null=True, blank=True)
    potencia_nominal_kw = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    orden = models.IntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        db_table = "proyecto_inversores"
        unique_together = [("nombre", "proyecto")]


class Inversionista(Timer):
    """Un cliente en su papel de inversionista: una fila por cliente.

    `clientes` es la ficha de cualquier tercero (comprador, arrendador, operador
    de red…); no todo cliente es inversionista. Esta tabla dice cuáles sí, y es a
    la que apunta su participación en cada planta (`proyecto_inversionistas`).
    El nombre y el NIT se leen de la ficha del cliente: aquí no se copian.

    Diagrama de Sara (2026-10-13). La escribe `ProyectoInversionista.save()` al
    registrar una participación con un cliente que todavía no era inversionista.
    Las participaciones anteriores se vincularon con `poblar_inversionistas`
    (borrado en el paso 3, `proyectos/0010`; está en el historial de git)."""

    id = models.BigAutoField(primary_key=True)
    cliente = models.OneToOneField(
        "clientes.Cliente", on_delete=models.PROTECT, db_column="cliente_id",
        related_name="inversionista",
    )

    class Meta:
        db_table = "inversionistas"


class Patrimonio(Timer):
    """Un patrimonio autónomo (fiducia) por el que un inversionista tiene su parte de
    una planta. Modelo de Sara (2026-10-14).

    El DUEÑO de la participación es el inversionista (Ayura, Solenium…); al
    patrimonio es a quien se le factura y se le liquida. Por eso el patrimonio no es
    un inversionista: la participación apunta a él (`ProyectoInversionista.patrimonio`).

    Jerarquía de dos niveles: un patrimonio GENERAL (la fiduciaria: Fiduciaria
    Bancolombia, Fideicomisos BBVA) no tiene `padre`; un SUBPATRIMONIO (P.A. Valle
    de Gandalf, código 17434) apunta a su general. Una planta se puede tener por un
    subpatrimonio o directo por un general.

    `cliente` es su ficha (razón social, NIT): lo que se factura apunta a
    `clientes`. Quién lo contrató no se guarda: son los inversionistas de sus
    participaciones."""

    id = models.BigAutoField(primary_key=True)
    nombre = models.CharField(max_length=255)
    codigo = models.CharField(max_length=20, null=True, blank=True)
    padre = models.ForeignKey(
        "self", on_delete=models.PROTECT, db_column="padre_id", null=True, blank=True,
        related_name="subpatrimonios",
    )
    cliente = models.ForeignKey(
        "clientes.Cliente", on_delete=models.PROTECT, db_column="cliente_id",
        null=True, blank=True, related_name="patrimonios",
    )

    class Meta:
        db_table = "patrimonios"
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["codigo"], condition=Q(codigo__isnull=False),
                name="uq_patrimonios_codigo",
            ),
            models.CheckConstraint(
                name="ck_patrimonios_padre", condition=~Q(padre=F("id")),
            ),
        ]


class ProyectoInversionista(Timer):
    """La participación de un inversionista en una planta, con su porcentaje.

    Apunta al inversionista (diagrama de Sara, 2026-10-13); el cliente se alcanza a
    través de él. Un mismo inversionista no está dos veces en la misma planta.

    `patrimonio` (opcional): el patrimonio autónomo por el que el inversionista
    tiene su parte. El inversionista sigue siendo el dueño; al patrimonio se le
    factura y liquida (2026-10-14). Reemplaza a la bandera `es_patrimonio_autonomo`,
    que nadie llenaba ni leía (`proyectos/0012`).

    `cliente` y `cliente_id` siguen existiendo como atajos de lectura y escritura
    --la API de inversionistas del proyecto recibe y devuelve `cliente_id`--: al
    asignar un cliente, `save()` busca o crea su inversionista. En consultas a la
    base el camino es `inversionista__cliente…`; `cliente_id` NO es una columna."""

    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="inversionistas")
    inversionista = models.ForeignKey(
        "Inversionista", on_delete=models.PROTECT, db_column="inversionista_id",
        related_name="participaciones",
    )
    porcentaje_participacion = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    patrimonio = models.ForeignKey(
        "Patrimonio", on_delete=models.PROTECT, db_column="patrimonio_id",
        null=True, blank=True, related_name="participaciones",
    )
    fecha_inicio = models.DateField(null=True, blank=True)
    fecha_fin = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "proyecto_inversionistas"
        constraints = [
            models.UniqueConstraint(
                fields=["proyecto", "inversionista"], name="uq_proyecto_inversionistas",
            ),
            # Redundante con la PK, a propósito: es el destino de la llave compuesta
            # de `contrato_proyectos`, que exige que la participación sea de la
            # misma planta que el contrato (`contratos/0017`).
            models.UniqueConstraint(
                fields=["id", "proyecto"], name="uq_proyecto_inversionistas_id_proyecto",
            ),
        ]

    @property
    def cliente_id(self):
        pendiente = getattr(self, "_cliente_pendiente", None)
        if pendiente is not None:
            return pendiente
        return self.inversionista.cliente_id if self.inversionista_id else None

    @cliente_id.setter
    def cliente_id(self, valor):
        self._cliente_pendiente = valor

    @property
    def cliente(self):
        if getattr(self, "_cliente_pendiente", None) is not None:
            from apps.clientes.models import Cliente

            return Cliente.objects.get(pk=self._cliente_pendiente)
        return self.inversionista.cliente if self.inversionista_id else None

    @cliente.setter
    def cliente(self, cliente):
        self._cliente_pendiente = cliente.pk if cliente is not None else None

    def save(self, *args, **kwargs):
        pendiente = getattr(self, "_cliente_pendiente", None)
        if pendiente is not None:
            self.inversionista, _ = Inversionista.objects.get_or_create(cliente_id=pendiente)
            self._cliente_pendiente = None
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = (
                    set(kwargs["update_fields"]) - {"cliente", "cliente_id"}
                ) | {"inversionista"}
        es_nueva = self._state.adding
        super().save(*args, **kwargs)
        if es_nueva:
            from apps.contratos.services.contrato_proyectos import completar_pendientes_de_planta

            completar_pendientes_de_planta(self)


class ProyectoPendienteIgnorado(models.Model):
    id = models.BigAutoField(primary_key=True)
    clave = models.CharField(max_length=120)
    ignorado_por_usuario = models.ForeignKey("plataforma.Usuario", on_delete=models.DO_NOTHING, db_column="ignorado_por_usuario_id", null=True, blank=True, related_name="proyectos_pendientes_ignorados_por_ignorado_por_usuario_id")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "proyectos_pendientes_ignorados"
        unique_together = [("clave",)]


class GestionRegistro(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="gestion_registros_por_proyecto_id")
    tipo = models.CharField(max_length=50, choices=[("pqr", "pqr"), ("preventivo", "preventivo"), ("correctivo", "correctivo")])  # la columna es varchar(50)
    titulo = models.CharField(max_length=500)
    descripcion = models.TextField(null=True, blank=True)
    created_by = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "gestion_registros"


class CostoVariable(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="costos_variables_por_proyecto_id")
    tipo_accion = models.CharField(max_length=50)
    tipo_equipo = models.CharField(max_length=100)
    monto = models.DecimalField(max_digits=18, decimal_places=2)
    fecha = models.DateField(db_index=True)
    descripcion = models.TextField()
    observaciones = models.TextField(null=True, blank=True)
    url_factura = models.CharField(max_length=500, null=True, blank=True)
    nombre_factura = models.CharField(max_length=255, null=True, blank=True)
    url_cotizacion = models.CharField(max_length=500, null=True, blank=True)
    nombre_cotizacion = models.CharField(max_length=255, null=True, blank=True)
    url_rut = models.CharField(max_length=500, null=True, blank=True)
    nombre_rut = models.CharField(max_length=255, null=True, blank=True)
    url_certificado_bancario = models.CharField(max_length=500, null=True, blank=True)
    nombre_certificado_bancario = models.CharField(max_length=255, null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "costos_variables"


class VerificacionCosto(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.DO_NOTHING, db_column="proyecto_id", related_name="verificacion_costos")
    costos_generador = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    costos_comercializador = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    ac_power = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = "verificacion_costos"


class GeneracionDiaria(Timer):
    id = models.BigAutoField(primary_key=True)
    proyecto = models.ForeignKey("Proyecto", on_delete=models.CASCADE, db_column="proyecto_id", related_name="generacion_diaria_por_proyecto_id")
    fecha = models.DateField()
    kwh_real = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    kwh_p90 = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    kwh_autoconsumo = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuente = models.CharField(max_length=50, default="manual")
    notas = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "generacion_diaria"
        unique_together = [("fecha", "proyecto")]
