import enum
from datetime import datetime, date
from sqlalchemy import (BigInteger, String, Numeric, Boolean, Date,
                        DateTime, Integer, ForeignKey, Enum as SAEnum, Text, CheckConstraint,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from app.models.base import Base


class ClasificacionRegulatoriaEnum(str, enum.Enum):
    AGP = "AGP"
    AGPE = "AGPE"
    AGGE = "AGGE"
    GD = "GD"
    DER = "DER"
    otra = "otra"


class TipoTecnologiaEnum(str, enum.Enum):
    solar = "solar"
    eolica = "eolica"
    hidraulica = "hidraulica"
    biomasa = "biomasa"
    otra = "otra"


class EstadoProyectoEnum(str, enum.Enum):
    en_desarrollo = "en_desarrollo"
    en_operacion = "en_operacion"
    suspendido = "suspendido"
    cancelado = "cancelado"


# Cómo se lee cada estado en pantalla. Vive al lado del enum y no en la vista
# que lo muestra: las APIs que salen hacia afuera mandan la etiqueta junto al
# slug para que quien integre no hardcodee su propio mapa de español, que se
# desalinearía el día que se agregue un estado.
ESTADO_PROYECTO_LABELS = {
    "en_desarrollo": "En desarrollo",
    "en_operacion": "En operación",
    "suspendido": "Suspendido",
    "cancelado": "Cancelado",
}


class TipoProyectoEnum(str, enum.Enum):
    minigranja = "minigranja"
    autoconsumo = "autoconsumo"
    gd = "gd"
    movilidad_electrica = "movilidad_electrica"
    otro = "otro"


class Portafolio(Base):
    __tablename__ = "portafolios"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proyectos: Mapped[list["Proyecto"]] = relationship("Proyecto", back_populates="portafolio")


class Proyecto(Base):
    __tablename__ = "proyectos"
    __table_args__ = (
        # Frontera tenia esta misma proteccion contra typos de digitacion
        # (ej. latitud=950 en vez de 9.50) antes de que sus columnas se
        # consolidaran aca (2026-08-25, ver migracion 094) -- se traslada
        # para no perderla ahora que Proyecto es la fuente unica. Rango de
        # altitud generoso (Colombia va de ~0 a ~5800 msnm) para no
        # bloquear variacion real.
        CheckConstraint("latitud IS NULL OR (latitud >= -90 AND latitud <= 90)", name="ck_proyectos_latitud_rango"),
        CheckConstraint("longitud IS NULL OR (longitud >= -180 AND longitud <= 180)", name="ck_proyectos_longitud_rango"),
        CheckConstraint("altitud_msnm IS NULL OR (altitud_msnm >= -100 AND altitud_msnm <= 6000)", name="ck_proyectos_altitud_msnm_rango"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portafolio_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("portafolios.id"), nullable=True, index=True)

    nombre_comercial: Mapped[str] = mapped_column(String(255), nullable=False)
    sub_project: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)
    # Tópico de la planta en la API de LIQUIDACIONES, cuando difiere del que usa
    # la API de generación (`sub_project`). Los dos sistemas de Unergy no siempre
    # nombran igual la misma planta: p. ej. la que aquí es `leyenda` allá es
    # `mgs18`, y consultar generación con `mgs18` devuelve cero registros. Sin
    # este campo esas plantas quedan fuera del AC Power total, que es el divisor
    # de la prorrata del reparto. Vacío = se usa `sub_project`.
    topico_liquidaciones: Mapped[str | None] = mapped_column(String(100), nullable=True)

    clasificacion_regulatoria: Mapped[str | None] = mapped_column(SAEnum(ClasificacionRegulatoriaEnum, name="clasificacion_regulatoria_enum"), nullable=True)
    tipo_tecnologia: Mapped[str | None] = mapped_column(SAEnum(TipoTecnologiaEnum, name="tipo_tecnologia_enum"), nullable=True)
    tipo_proyecto: Mapped[str | None] = mapped_column(SAEnum(TipoProyectoEnum, name="tipo_proyecto_enum"), nullable=True)

    potencia_instalada_kwp: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    potencia_con_cen_mw: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    produccion_especifica_kwh_kwp: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    codigo_cnd: Mapped[str | None] = mapped_column(String(50), nullable=True)
    estado: Mapped[str] = mapped_column(SAEnum(EstadoProyectoEnum, name="estado_proyecto_enum"), nullable=False, default="en_desarrollo")
    fecha_entrada_operacion: Mapped[date | None] = mapped_column(Date, nullable=True)
    fecha_fin_representacion: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Fecha de inicio de comercialización = primer día con generación real de energía.
    # Se autoderiva de la API de generación (app.services.comercializacion) y se
    # persiste aquí; una planta con esta fecha se considera comercializando y entra
    # a Cumplimiento. Editable a mano (marca fecha_comercializacion_editada_manual).
    fecha_inicio_comercializacion: Mapped[date | None] = mapped_column(Date, nullable=True)
    # El operador fijó la fecha de comercialización a mano → el backfill/job diario
    # no la vuelve a pisar.
    fecha_comercializacion_editada_manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Generación mensual promedio ───────────────────────────────────────────
    # Cuánta energía entrega esta planta en un mes típico, en MWh. Se calcula UNA
    # vez desde la API de generación de Unergy (app/services/gen_promedio.py) y
    # se persiste acá, para que las vistas de contratos no dependan de esa API en
    # cada consulta: con esto alcanza con leer la BD.
    #
    # Las plantas sin histórico (recién energizadas, sin sub_project) se cargan a
    # mano; por eso hace falta saber de dónde salió cada valor —ver
    # `gen_promedio_origen`— y no pisar lo manual al recalcular. Es el mismo
    # patrón que `fecha_comercializacion_editada_manual`.
    gen_mensual_promedio_mwh: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    # 'api' = derivado del histórico · 'manual' = lo puso una persona.
    gen_promedio_origen: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Cuántos días CON LECTURA entraron al promedio, de los 30 de la ventana. Un
    # promedio hecho sobre 27 días no vale lo mismo que uno sobre 30, y sin este
    # número no hay forma de saberlo.
    gen_promedio_dias: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gen_promedio_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    gen_promedio_hasta: Mapped[date | None] = mapped_column(Date, nullable=True)
    gen_promedio_actualizado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    departamento: Mapped[str | None] = mapped_column(String(100), nullable=True)
    municipio: Mapped[str | None] = mapped_column(String(100), nullable=True)
    direccion_vereda: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitud: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitud: Mapped[float | None] = mapped_column(Numeric(9, 6), nullable=True)
    # Sin equivalente en Frontera antes de 2026-08-25 -- se agrega junto con
    # la consolidacion de latitud/longitud (auditoria de integridad de
    # Fronteras) para no dejar la altitud como el unico dato de ubicacion
    # que solo vivia en Frontera.
    altitud_msnm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Vínculo estructurado al catálogo (mismo patrón que Frontera.operador_red_id).
    # Se sincroniza con las fronteras del proyecto: si el proyecto no tiene
    # valor, se rellena desde la primera frontera que sí lo tenga; si se edita
    # en el proyecto, se rellena hacia las fronteras que todavía no lo tengan.
    # Nunca se pisa un valor ya diligenciado en ningún lado (ver proyectos.py).
    operador_red_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("operadores_red.id"), nullable=True, index=True)
    project_id_solenium: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # ID del proyecto en la API nueva de SolarView -- NO coincide con
    # project_id_solenium (esquema de IDs completamente distinto entre las
    # dos APIs). Se usa solo desde Reporte de Energía por ahora (Fase 1 de
    # la migración); los demás módulos siguen con project_id_solenium.
    project_id_solarview: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)

    # Etiqueta de comunidad energética (ortogonal a Rep/Energía: cualquier planta
    # puede o no pertenecer a una comunidad).
    es_comunidad_energetica: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")
    nombre_comunidad: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Servicios activos
    srv_operacion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    srv_representacion: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    srv_cgm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    srv_ppa: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    srv_promotor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    srv_rec: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # P50/P90/P99 monthly simulation (JSON arrays of 12 kWh values, index 0 = enero)
    p90_mensual_kwh = mapped_column(JSONB, nullable=True)
    p50_mensual_kwh = mapped_column(JSONB, nullable=True)
    p99_mensual_kwh = mapped_column(JSONB, nullable=True)
    codigo_tsf: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ── IDs de liquidación ──────────────────────────────────────────────────────

    # ── Pipeline TSF / próximos a energizarse ───────────────────────────────────
    # Correlación con originabotdb.minifarm_project.name / base_name de Sun Factory.
    origina_code: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    # ID interno de Sun Factory (sunfactory.sole.tech) para el proyecto en el
    # pipeline de construcción. Distinto de `project_id_solenium` (API de
    # generación, data.sole.tech) -- espacios de IDs separados aunque ambos
    # productos son de Solenium. Estable aunque Sun Factory renombre el proyecto;
    # es la llave que usa sync_tsf_projects() para no crear duplicados.
    sunfactory_project_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    # Fase del pipeline de construcción (complementa `estado`, que se queda en
    # 'en_desarrollo' mientras la planta no opera). Etiquetas: en_construccion |
    # pruebas | proximo_energizar | energizado.
    fase_construccion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Fecha tentativa de energización -- siempre la que trae Sun Factory (solo lectura).
    fecha_estimada_energizacion: Mapped[date | None] = mapped_column(Date, nullable=True)
    # % de avance de obra (Sun Factory).
    avance_obra_pct: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Proyección de generación mensual (MWh), editable por operaciones.
    # Origen del registro: 'manual' (alta normal) | 'tsf_sync' (auto-importado).
    origen: Mapped[str | None] = mapped_column(String(20), default="manual", nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relaciones
    portafolio: Mapped["Portafolio | None"] = relationship("Portafolio", back_populates="proyectos")
    info_tecnica: Mapped["ProyectoInfoTecnica | None"] = relationship("ProyectoInfoTecnica", back_populates="proyecto", uselist=False)
    inversores: Mapped[list["ProyectoInversor"]] = relationship("ProyectoInversor", back_populates="proyecto", uselist=True)
    area_contactos: Mapped[list["ProyectoAreaContacto"]] = relationship("ProyectoAreaContacto", back_populates="proyecto", cascade="all, delete-orphan", uselist=True)
    inversionistas: Mapped[list["ProyectoInversionista"]] = relationship("ProyectoInversionista", back_populates="proyecto", uselist=True)
    fronteras: Mapped[list["Frontera"]] = relationship("Frontera", back_populates="proyecto", uselist=True)
    operador: Mapped["OperadorRed | None"] = relationship("OperadorRed", back_populates="proyectos")
    fallas: Mapped[list["Falla"]] = relationship("Falla", back_populates="proyecto", uselist=True)
    generaciones: Mapped[list["GeneracionDiaria"]] = relationship("GeneracionDiaria", back_populates="proyecto", uselist=True)
    mantenimientos: Mapped[list["Mantenimiento"]] = relationship("Mantenimiento", back_populates="proyecto", uselist=True)
    liquidaciones: Mapped[list["Liquidacion"]] = relationship("Liquidacion", back_populates="proyecto", uselist=True)
    asic_solicitudes: Mapped[list["AsicSolicitud"]] = relationship("AsicSolicitud", back_populates="proyecto", uselist=True)
    rec_procesos: Mapped[list["RecProceso"]] = relationship("RecProceso", back_populates="proyecto", uselist=True)
    promotor_seguimientos: Mapped[list["PromoterSeguimiento"]] = relationship("PromoterSeguimiento", back_populates="proyecto", uselist=True)
    contratos_servicio: Mapped[list["ContratoServicio"]] = relationship("ContratoServicio", back_populates="proyecto", uselist=True)
    ppa_contratos: Mapped[list["PPAContrato"]] = relationship("PPAContrato", secondary="ppa_contrato_proyectos", uselist=True, viewonly=True)

    @property
    def operador_red_legal(self) -> str | None:
        """Nombre legal del operador de red (catálogo operadores_red). Primero
        el vínculo propio del proyecto; si no lo tiene, el de la primera
        frontera VIVA que sí lo tenga (caso de datos aún no sincronizados) --
        una frontera borrada no debe seguir prestando su operador.
        Requiere precargar `operador` y `fronteras.operador` (selectinload)
        para no golpear la BD por cada proyecto."""
        if self.operador:
            return self.operador.nombre_legal
        for f in self.fronteras:
            if f.deleted_at is None and f.operador:
                return f.operador.nombre_legal
        return None


class TipoTrackerEnum(str, enum.Enum):
    p1 = "1P"
    p2 = "2P"


# Este enum guarda el VALOR en Postgres ("1P"/"2P"), no el nombre del miembro
# ("p1"/"p2" -- "1P" no es un identificador Python válido). Mismo patrón que
# ClaseCtEnum/ClasePtEnum en app/models/fronteras.py.
def _tracker_por_valor(enum_cls):
    return [m.value for m in enum_cls]


class ProyectoInfoTecnica(Base):
    __tablename__ = "proyecto_info_tecnica"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proyecto_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("proyectos.id"), unique=True, index=True, nullable=False)

    # Datos eléctricos generales
    voltaje_red: Mapped[str | None] = mapped_column(String(50), nullable=True)
    potencia_ac_kw: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    capacidad_instalada_kwp: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    tipo_tracker: Mapped[str | None] = mapped_column(SAEnum(TipoTrackerEnum, name="tipo_tracker_enum", values_callable=_tracker_por_valor), nullable=True)

    # Paneles
    cantidad_total_paneles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    potencia_panel_kwp: Mapped[str | None] = mapped_column(String(100), nullable=True)
    marca_paneles: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Inversores
    cantidad_inversores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    potencia_inversores_kwp: Mapped[str | None] = mapped_column(String(100), nullable=True)
    marca_inversores: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cantidad_strings: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Marcas de equipos
    marca_transformador: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marca_reconectador_rele: Mapped[str | None] = mapped_column(String(500), nullable=True)
    marca_totalizador: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marca_seguidor_solar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marca_medidores_frontera: Mapped[str | None] = mapped_column(String(255), nullable=True)
    marca_modem_reconectador: Mapped[str | None] = mapped_column(String(500), nullable=True)
    marca_modems_frontera: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_modem_reconectador: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Ubicación
    url_ubicacion: Mapped[str | None] = mapped_column(Text, nullable=True)

    # CCTV y seguridad
    cctv_estado: Mapped[str | None] = mapped_column(Text, nullable=True)
    marca_cctv: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seguridad_fisica: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tiene_internet: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proyecto: Mapped["Proyecto"] = relationship("Proyecto", back_populates="info_tecnica")


class ProyectoInversor(Base):
    __tablename__ = "proyecto_inversores"
    __table_args__ = (
        # `nombre` es el identificador real de cada inversor (con qué se
        # reporta una falla) -- sin este constraint, una condicion de
        # carrera en el extinto backfill_inversores_minigranjas() (dos
        # llamadas cercanas veian "0 inversores" antes de que la primera
        # confirmara) duplicaba el set completo de 5 en silencio. Confirmado
        # en produccion 2026-08-27: 6 proyectos con hasta 3 copias identicas
        # (MGS 0032 El Paso Norte, 15 filas en vez de 5), ya limpiados; todo
        # el mecanismo de backfill/seed automatico se eliminó ese mismo día.
        UniqueConstraint("proyecto_id", "nombre", name="uq_proyecto_inversor_nombre"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proyecto_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("proyectos.id"), nullable=False, index=True)
    nombre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    potencia_nominal_kw: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    orden: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    activo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proyecto: Mapped["Proyecto"] = relationship("Proyecto", back_populates="inversores")



class ProyectoInversionista(Base):
    __tablename__ = "proyecto_inversionistas"
    __table_args__ = (
        CheckConstraint("porcentaje_participacion >= 0 AND porcentaje_participacion <= 100", name="ck_inversionista_pct_rango"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    proyecto_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("proyectos.id"), nullable=False, index=True)
    cliente_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("clientes.id"), nullable=False, index=True)
    porcentaje_participacion: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    es_patrimonio_autonomo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fecha_inicio: Mapped[date | None] = mapped_column(Date, nullable=True)
    fecha_fin: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    proyecto: Mapped["Proyecto"] = relationship("Proyecto", back_populates="inversionistas")
    cliente: Mapped["Cliente"] = relationship("Cliente", back_populates="participaciones")

    @property
    def cliente_nombre(self) -> str:
        return self.cliente.razon_social_nombre if self.cliente else ""


class ProyectoPendienteIgnorado(Base):
    """Candidato de /proyectos/pendientes (Sun Factory/Quoia/Solenium) marcado
    a propósito como "no aplica" para que deje de aparecer -- ej. un medidor
    de prueba, o algo que ya se revisó y no corresponde a un proyecto real."""

    __tablename__ = "proyectos_pendientes_ignorados"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clave: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    ignorado_por_usuario_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("usuarios.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
