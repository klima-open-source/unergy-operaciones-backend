from pydantic import BaseModel, field_validator
from typing import Optional, Any
from datetime import datetime, date, time


class FallaCatEstadoOut(BaseModel):
    id: int
    codigo: str
    etiqueta: str
    orden: int
    es_estado_final: bool
    model_config = {"from_attributes": True}


class FallaCatPrioridadOut(BaseModel):
    id: int
    codigo: str
    etiqueta: str
    nivel: int
    model_config = {"from_attributes": True}


class FallaCatCategoriaOut(BaseModel):
    id: int
    codigo: str
    etiqueta: str
    icono: Optional[str]
    color_hex: Optional[str]
    orden: int
    model_config = {"from_attributes": True}


class FallaCatTipoOut(BaseModel):
    id: int
    codigo: str
    etiqueta: str
    descripcion: Optional[str]
    categoria: FallaCatCategoriaOut
    model_config = {"from_attributes": True}


class FallaCatResolucionOut(BaseModel):
    id: int
    codigo: str
    etiqueta: str
    model_config = {"from_attributes": True}


class UsuarioResumen(BaseModel):
    id: int
    nombre: str
    email: str
    model_config = {"from_attributes": True}


class ProyectoResumen(BaseModel):
    id: int
    nombre_comercial: str
    sub_project: Optional[str] = None
    model_config = {"from_attributes": True}


class FallaIntervaloIn(BaseModel):
    """Intervalo de disparo enviado desde el cliente (al crear o editar)."""
    inicio: datetime
    fin: Optional[datetime] = None
    nota: Optional[str] = None


class FallaIntervaloOut(BaseModel):
    id: int
    falla_id: int
    inicio: datetime
    fin: Optional[datetime]
    nota: Optional[str]
    duracion_horas: Optional[float] = None
    created_at: datetime
    model_config = {"from_attributes": True}


class FallaInversorIn(BaseModel):
    """Inversor afectado dentro de un reporte estructurado de categoría 'inversores'."""
    proyecto_inversor_id: Optional[int] = None
    nombre: Optional[str] = None
    potencia_kw: Optional[float] = None
    tipos: list[str] = []


class FallaInversorOut(BaseModel):
    id: int
    proyecto_inversor_id: Optional[int] = None
    nombre: Optional[str] = None
    potencia_kw: Optional[float] = None
    tipos: list[str] = []
    model_config = {"from_attributes": True}

    @field_validator("tipos", mode="before")
    @classmethod
    def none_to_list(cls, v):
        return v if v is not None else []


class FallaCreate(BaseModel):
    proyecto_id: int
    tipo_id: Optional[int] = None
    estado_id: int
    prioridad_id: int
    resolucion_id: Optional[int] = None
    descripcion: str
    fecha_identificacion: date
    hora_identificacion: Optional[time] = None
    fecha_ocurrencia: Optional[datetime] = None
    fecha_resolucion: Optional[datetime] = None
    sla_limite_horas: Optional[int] = None
    fotos_urls: Optional[list[str]] = None
    notificacion: bool = False
    alarma_monitoreo_id: Optional[int] = None
    kwh_perdidos_estimado: Optional[float] = None
    impacto_economico_cop: Optional[float] = None
    causa_raiz: Optional[str] = None
    acciones_correctivas: Optional[str] = None
    fecha_programada: Optional[date] = None
    intervalos: Optional[list[FallaIntervaloIn]] = None
    # ── Reporte estructurado (jerárquico por activo) ─────────────────────────
    categoria_codigo: Optional[str] = None
    subtipo_codigo: Optional[str] = None
    subtipo_detalle: Optional[str] = None
    frontera_afecta_medicion: Optional[bool] = None
    frontera_perdida_comunicacion: Optional[bool] = None
    inversores: Optional[list[FallaInversorIn]] = None


class FallaUpdate(BaseModel):
    tipo_id: Optional[int] = None
    estado_id: Optional[int] = None
    prioridad_id: Optional[int] = None
    resolucion_id: Optional[int] = None
    descripcion: Optional[str] = None
    fecha_identificacion: Optional[date] = None
    hora_identificacion: Optional[time] = None
    fecha_ocurrencia: Optional[datetime] = None
    fecha_resolucion: Optional[datetime] = None
    sla_limite_horas: Optional[int] = None
    # sla_cumplido NO es editable -- es siempre calculado (ver
    # _sincronizar_resolucion en app/api/v1/fallas.py), nunca manual.
    fotos_urls: Optional[list[str]] = None
    notificacion: Optional[bool] = None
    kwh_perdidos_estimado: Optional[float] = None
    impacto_economico_cop: Optional[float] = None
    causa_raiz: Optional[str] = None
    acciones_correctivas: Optional[str] = None
    fecha_programada: Optional[date] = None
    intervalos: Optional[list[FallaIntervaloIn]] = None
    # ── Reporte estructurado / reclasificación ───────────────────────────────
    categoria_codigo: Optional[str] = None
    subtipo_codigo: Optional[str] = None
    subtipo_detalle: Optional[str] = None
    frontera_afecta_medicion: Optional[bool] = None
    frontera_perdida_comunicacion: Optional[bool] = None
    pendiente_reclasificar: Optional[bool] = None
    inversores: Optional[list[FallaInversorIn]] = None


class FallaSeguimientoCreate(BaseModel):
    nota: Optional[str] = None
    estado_nuevo_id: Optional[int] = None


class FallaSeguimientoOut(BaseModel):
    id: int
    falla_id: int
    nota: Optional[str]
    estado_nuevo: Optional[FallaCatEstadoOut]
    usuario: UsuarioResumen
    created_at: datetime
    model_config = {"from_attributes": True}


class FallaListOut(BaseModel):
    """Versión liviana de FallaOut para el listado (GET /fallas) -- sin
    seguimientos/intervalos/inversores_afectados, que la tabla no muestra y
    que si se declararan acá forzarían un lazy-load por fila (list_fallas
    ya no los precarga, ver _FALLA_LOAD_LISTA en fallas.py). El detalle
    completo se pide aparte con GET /fallas/{id} (FallaOut)."""
    id: int
    codigo_interno: str
    proyecto_id: int
    proyecto: ProyectoResumen
    tipo: Optional[FallaCatTipoOut]
    estado: FallaCatEstadoOut
    prioridad: FallaCatPrioridadOut
    resolucion: Optional[FallaCatResolucionOut]
    registrado_por: UsuarioResumen
    descripcion: str
    fecha_identificacion: date
    hora_identificacion: Optional[time]
    fecha_ocurrencia: Optional[datetime]
    fecha_resolucion: Optional[datetime]
    sla_limite_horas: Optional[int]
    sla_limite_horas_efectivo: Optional[int] = None
    sla_cumplido: Optional[bool]
    tiene_fotos: bool = False
    fotos_lista: list[Any] = []
    notificacion: bool = False
    alarma_monitoreo_id: Optional[int] = None
    kwh_perdidos_estimado: Optional[float] = None
    impacto_economico_cop: Optional[float] = None
    causa_raiz: Optional[str] = None
    acciones_correctivas: Optional[str] = None
    fecha_programada: Optional[date] = None
    dias_abierta: Optional[int] = None
    tiempo_afectacion_horas: Optional[float] = None
    sla_limite_dias: Optional[int] = None
    categoria_codigo: Optional[str] = None
    subtipo_codigo: Optional[str] = None
    subtipo_detalle: Optional[str] = None
    clasificacion: Optional[Any] = None
    pendiente_reclasificar: bool = False
    frontera_afecta_medicion: Optional[bool] = None
    frontera_perdida_comunicacion: Optional[bool] = None
    inversores_perdida_comunicacion: Optional[bool] = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("fotos_lista", mode="before")
    @classmethod
    def coerce_fotos_lista(cls, v):
        import json as _json
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                result = _json.loads(v)
                return result if isinstance(result, list) else []
            except Exception:
                return []
        return []


class FallaOut(BaseModel):
    id: int
    codigo_interno: str
    proyecto_id: int
    proyecto: ProyectoResumen
    tipo: Optional[FallaCatTipoOut]
    estado: FallaCatEstadoOut
    prioridad: FallaCatPrioridadOut
    resolucion: Optional[FallaCatResolucionOut]
    registrado_por: UsuarioResumen
    descripcion: str
    fecha_identificacion: date
    hora_identificacion: Optional[time]
    fecha_ocurrencia: Optional[datetime]
    fecha_resolucion: Optional[datetime]
    sla_limite_horas: Optional[int]
    sla_limite_horas_efectivo: Optional[int] = None
    sla_cumplido: Optional[bool]
    tiene_fotos: bool = False
    fotos_lista: list[Any] = []
    notificacion: bool = False
    alarma_monitoreo_id: Optional[int] = None
    kwh_perdidos_estimado: Optional[float] = None
    impacto_economico_cop: Optional[float] = None
    causa_raiz: Optional[str] = None
    acciones_correctivas: Optional[str] = None
    fecha_programada: Optional[date] = None
    dias_abierta: Optional[int] = None
    tiempo_afectacion_horas: Optional[float] = None
    sla_limite_dias: Optional[int] = None
    # ── Reporte estructurado ──────────────────────────────────────────────────
    categoria_codigo: Optional[str] = None
    subtipo_codigo: Optional[str] = None
    subtipo_detalle: Optional[str] = None
    clasificacion: Optional[Any] = None
    pendiente_reclasificar: bool = False
    frontera_afecta_medicion: Optional[bool] = None
    frontera_perdida_comunicacion: Optional[bool] = None
    inversores_perdida_comunicacion: Optional[bool] = None
    inversores_afectados: list[FallaInversorOut] = []
    seguimientos: list[FallaSeguimientoOut] = []
    intervalos: list[FallaIntervaloOut] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}

    @field_validator("fotos_lista", mode="before")
    @classmethod
    def coerce_fotos_lista(cls, v):
        """Normaliza fotos_lista: acepta list[dict], list[str] o JSON-string."""
        import json as _json
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                result = _json.loads(v)
                return result if isinstance(result, list) else []
            except Exception:
                return []
        return []

    @field_validator("seguimientos", "intervalos", "inversores_afectados", mode="before")
    @classmethod
    def none_to_list(cls, v):
        return v if v is not None else []


class FallaCatalogos(BaseModel):
    estados: list[FallaCatEstadoOut]
    prioridades: list[FallaCatPrioridadOut]
    tipos: list[FallaCatTipoOut]
    resoluciones: list[FallaCatResolucionOut]


class FallaSLADashboard(BaseModel):
    fallas_en_riesgo_sla: int
    fallas_sla_vencido: int
    promedio_tiempo_resolucion_horas: Optional[float]
    cumplimiento_sla_pct: Optional[float]


class FallaImpacto(BaseModel):
    falla_id: int
    proyecto_nombre: str
    potencia_instalada_kwp: Optional[float]
    horas_fuera: float
    kwh_perdidos_estimado: float
    impacto_economico_cop: float
