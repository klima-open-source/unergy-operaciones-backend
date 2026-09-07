import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import func, extract
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from app.core.database import get_db
from app.api.v1.auth import get_current_user
from app.models import (
    Falla, FallaSeguimiento, FallaIntervalo, FallaInversor,
    FallaCatEstado, FallaCatPrioridad, FallaCatTipo, FallaCatCategoria, FallaCatResolucion,
)
from app.models.fallas import DEFAULT_SLA_HOURS
from app.models.proyectos import Proyecto, ProyectoInversionista
from app.models.usuarios import Usuario
from app.schemas.fallas import (
    FallaCreate, FallaUpdate, FallaOut, FallaListOut,
    FallaSeguimientoCreate, FallaSeguimientoOut,
    FallaCatalogos, FallaCatEstadoOut, FallaCatPrioridadOut, FallaCatTipoOut, FallaCatResolucionOut,
    FallaSLADashboard, FallaImpacto,
)
from app.services.fallas.consulta_publica import (
    GRUPOS, GRUPOS_CONSULTABLES, GRUPO_TODAS, DESCRIPCION_GRUPOS,
    grupo_de_estado, codigos_de_grupo, falla_publica, proyecto_publico,
)
from app.services.fallas.estructura import (
    ESTRUCTURA_FALLAS, get_categoria, validar_clasificacion, tipo_codigo,
    etiqueta_subtipo, es_subtipo_pendiente,
)
from app.services.fallas.titulo import titulo_falla
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/fallas", tags=["Fallas"])

_SEGS_LOAD = selectinload(Falla.seguimientos).options(
    selectinload(FallaSeguimiento.usuario),
    selectinload(FallaSeguimiento.estado_nuevo),
)

# Liviano: lo que la tabla/lista y el "hero" del drawer muestran de entrada.
# NO incluye seguimientos/intervalos/inversores -- eso solo hace falta al
# abrir el detalle de una falla puntual (ver GET /fallas/{id} con
# _FALLA_LOAD completo), no en cada fila de un listado de cientos de filas.
_FALLA_LOAD_LISTA = [
    selectinload(Falla.proyecto),
    selectinload(Falla.tipo).selectinload(FallaCatTipo.categoria),
    selectinload(Falla.estado),
    selectinload(Falla.prioridad),
    selectinload(Falla.resolucion),
    selectinload(Falla.registrado_por),
]

_FALLA_LOAD = [
    *_FALLA_LOAD_LISTA,
    selectinload(Falla.intervalos),
    selectinload(Falla.inversores_afectados),
    _SEGS_LOAD,
]


def _validar_clasificacion_payload(categoria_codigo, subtipo_codigo, inversores) -> None:
    """Valida el payload estructurado contra la estructura canónica; 422 si inválido."""
    inv_tipos = sorted({t for inv in (inversores or []) for t in (_inv_tipos(inv))})
    ok, err = validar_clasificacion(categoria_codigo, subtipo_codigo, inv_tipos)
    if not ok:
        raise HTTPException(422, f"Clasificación inválida: {err}")


def _inv_tipos(inv) -> list:
    """tipos de un inversor venga como dict (model_dump) o como objeto pydantic."""
    if isinstance(inv, dict):
        return inv.get("tipos") or []
    return getattr(inv, "tipos", None) or []


def _inv_get(inv, key):
    if isinstance(inv, dict):
        return inv.get(key)
    return getattr(inv, key, None)


def _sync_falla_inversores(falla: Falla, inversores: list | None, db: Session) -> None:
    """Reemplaza los inversores afectados de la falla (replace-all)."""
    if inversores is None:
        return
    db.query(FallaInversor).filter(FallaInversor.falla_id == falla.id).delete(synchronize_session=False)
    for inv in inversores:
        tipos = _inv_tipos(inv)
        db.add(FallaInversor(
            falla_id=falla.id,
            proyecto_inversor_id=_inv_get(inv, "proyecto_inversor_id"),
            nombre=_inv_get(inv, "nombre"),
            potencia_kw=_inv_get(inv, "potencia_kw"),
            tipos=tipos or [],
        ))


def _aplicar_clasificacion(falla: Falla, inversores: list | None, db: Session) -> None:
    """Deriva tipo_id/pendiente/flags/clasificacion a partir de las columnas ya
    asignadas en `falla` y la lista de inversores. Sincroniza falla_inversores.

    Asume que la clasificación ya fue validada. Para `inversores=None` (PATCH que no
    toca inversores) recalcula a partir de las filas existentes en BD.
    """
    categoria = falla.categoria_codigo
    cat = get_categoria(categoria) if categoria else None
    if not cat:
        return

    # Fuente de inversores para derivar: input nuevo o filas existentes.
    if inversores is None and categoria == "inversores":
        existentes = db.query(FallaInversor).filter(FallaInversor.falla_id == falla.id).all()
        inv_source = [
            {"proyecto_inversor_id": e.proyecto_inversor_id, "nombre": e.nombre,
             "potencia_kw": float(e.potencia_kw) if e.potencia_kw is not None else None,
             "tipos": e.tipos or []}
            for e in existentes
        ]
    else:
        inv_source = inversores or []

    inv_tipos_all = sorted({t for inv in inv_source for t in _inv_tipos(inv)})

    # pendiente_reclasificar deriva del subtipo (p.ej. desconexión sin identificar)
    falla.pendiente_reclasificar = es_subtipo_pendiente(categoria, falla.subtipo_codigo)
    # flag de comunicación de inversores (solo aplica a categoría inversores)
    falla.inversores_perdida_comunicacion = (
        ("perdida_comunicacion" in inv_tipos_all) if categoria == "inversores" else None
    )
    # frontera flags solo aplican a frontera
    if categoria != "frontera":
        falla.frontera_afecta_medicion = None
        falla.frontera_perdida_comunicacion = None

    # Mapeo a tipo de catálogo (para que vistas/analytics legacy muestren etiqueta)
    nuevo_tipo_id = None
    # Categorías de opción/equipo (red, frontera, eventos, generando sin datos):
    # el tipo sale del subtipo. Genérico a propósito: agregar una categoría a
    # ESTRUCTURA_FALLAS no debe exigir tocar esta lista.
    if cat["tipo"] in ("opcion", "equipo") and falla.subtipo_codigo:
        t = db.query(FallaCatTipo).filter_by(codigo=tipo_codigo(categoria, falla.subtipo_codigo)).first()
        if t:
            nuevo_tipo_id = t.id
    elif categoria == "inversores" and inv_tipos_all:
        t = db.query(FallaCatTipo).filter_by(codigo=tipo_codigo("inversores", inv_tipos_all[0])).first()
        if t:
            nuevo_tipo_id = t.id
    # SIEMPRE asignar (incluye None): en el camino estructurado el tipo_id nunca
    # debe quedar apuntando a un tipo legacy que contradiga la clasificación. Dejar
    # el valor previo era la causa de títulos como "Fusible de string quemado" en
    # fallas de red. Si el tipo estructurado no existe en el catálogo, tipo_id=None
    # y el título se arma al vuelo desde `clasificacion` (ver services/fallas/titulo.py).
    falla.tipo_id = nuevo_tipo_id

    # Snapshot estructurado (fuente para mostrar/auditar Y para armar el título
    # legible al vuelo -- reemplaza a tipo_libre, eliminado 2026-09-02 junto con
    # el backfill permanente que hacía falta para mantenerlo sincronizado).
    clasif = {"categoria": categoria, "categoria_etiqueta": cat["etiqueta"]}
    if falla.subtipo_codigo:
        clasif["subtipo"] = falla.subtipo_codigo
        clasif["subtipo_etiqueta"] = etiqueta_subtipo(categoria, falla.subtipo_codigo)
    if falla.subtipo_detalle:
        clasif["detalle"] = falla.subtipo_detalle
    if categoria == "frontera":
        clasif["afecta_medicion"] = bool(falla.frontera_afecta_medicion)
        clasif["perdida_comunicacion"] = bool(falla.frontera_perdida_comunicacion)
    if categoria == "inversores":
        clasif["inversores"] = [
            {
                "proyecto_inversor_id": _inv_get(inv, "proyecto_inversor_id"),
                "nombre": _inv_get(inv, "nombre"),
                "potencia_kw": _inv_get(inv, "potencia_kw"),
                "tipos": _inv_tipos(inv),
                "tipos_etiquetas": [etiqueta_subtipo("inversores", t) or t for t in _inv_tipos(inv)],
            }
            for inv in inv_source
        ]
    falla.clasificacion = clasif

    # Reemplaza filas de inversores afectados solo si vino input nuevo.
    _sync_falla_inversores(falla, inversores, db)


def _alarmas_post_guardado(falla_id: int, db: Session) -> None:
    """Hook de alarmas de comunicación tras crear/actualizar. Nunca rompe el flujo."""
    try:
        from app.services.fallas.alarmas import evaluar_alarmas_falla
        falla = db.query(Falla).options(selectinload(Falla.proyecto)).filter(Falla.id == falla_id).first()
        if falla and falla.categoria_codigo:
            evaluar_alarmas_falla(db, falla)
            db.commit()
    except Exception:
        db.rollback()
        logging.getLogger("fallas").exception("evaluar_alarmas_falla falló (no bloqueante)")


def _sync_intervalos(falla: Falla, intervalos: list | None, db: Session) -> None:
    """Reemplaza los intervalos de disparo de una falla con la lista recibida.
    `intervalos` es una lista de dicts {inicio, fin, nota}. Si es None, no se
    toca nada (no se enviaron). Si es [], se eliminan todos."""
    if intervalos is None:
        return
    # Borrar los existentes y recrear (replace-all). La lista suele ser pequeña.
    db.query(FallaIntervalo).filter(FallaIntervalo.falla_id == falla.id).delete(synchronize_session=False)
    for iv in intervalos:
        if not iv.get("inicio"):
            continue
        db.add(FallaIntervalo(
            falla_id=falla.id,
            inicio=iv["inicio"],
            fin=iv.get("fin"),
            nota=(iv.get("nota") or None),
        ))


def _get_or_404(id: int, db: Session) -> Falla:
    falla = db.query(Falla).options(*_FALLA_LOAD).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")
    return falla


def _integrity_error_a_http(e: IntegrityError) -> HTTPException:
    """Traduce un IntegrityError crudo de la BD a un error HTTP legible.

    Antes, un FK inexistente (proyecto_id, tipo_id, estado_id, prioridad_id,
    resolucion_id) volaba hasta el cliente como un 500 de
    Postgres sin mensaje claro -- ya documentado como deuda conocida en
    docs/API_FALLAS.md para los integradores externos de la API. Detecta el
    tipo de violación por texto del mensaje (portable entre Postgres y
    SQLite, útil para tests) en vez de por nombre de constraint (auditoría
    2026-09-02).

    codigo_legado se eliminó (auditoría 2026-09-02, era la llave de
    idempotencia de una migración puntual desde Apps Script; sin evidencia
    de uso activo hoy) -- si vuelve a existir un campo de idempotencia,
    agregar su rama acá de nuevo."""
    mensaje = str(e).lower()
    if "foreign key" in mensaje:
        return HTTPException(422, "Uno de los IDs enviados (proyecto_id/tipo_id/estado_id/prioridad_id/"
                                   "resolucion_id) no existe")
    return HTTPException(422, "No se pudo guardar la falla: violación de integridad en los datos enviados")


# Huella del integrador externo que reporta desbalances de tensión/reconectador
# en cero bajo el subtipo genérico "sin identificar" -- no vive en este repo, no
# se puede tocar su lógica (auditoría 2026-09-02). Usado tanto en el filtro
# ?pendiente_reclasificar=true de GET /fallas como en _bloquear_cierre_si_pendiente.
# Límite conocido: es indistinguible de una persona reportando ese mismo subtipo
# a mano -- hoy no hay ningún caso así en producción.
_BOT_DESCONEXION_CATEGORIA = "red"
_BOT_DESCONEXION_SUBTIPO = "desconexion_sin_identificar"


def _es_patron_bot_externo_desconexion(falla: Falla) -> bool:
    return (
        falla.alarma_monitoreo_id is None
        and falla.categoria_codigo == _BOT_DESCONEXION_CATEGORIA
        and falla.subtipo_codigo == _BOT_DESCONEXION_SUBTIPO
    )


def _bloquear_cierre_si_pendiente(falla: Falla, nuevo_estado: "FallaCatEstado | None", db: Session) -> None:
    """Impide cerrar una falla que sigue pendiente de reclasificar (causa sin
    confirmar) -- decisión de negocio 2026-09-02: mejor bloquear que dejar
    que se cierre y la causa real nunca se confirme (pasó con 840 de 851
    casos históricos). No aplica al patrón del bot externo -- bloquearlo
    ahí rompería su flujo de cierre automático, que no controlamos.

    Recibe `db` para revertir explícitamente el `setattr` en memoria que ya
    corrió sobre `falla` antes de este chequeo (update_falla/add_seguimiento
    aplican los cambios del payload al objeto ANTES de validar) -- sin el
    rollback, la sesión sigue viendo el estado nuevo en memoria aunque nunca
    se haya comiteado."""
    if not nuevo_estado or not nuevo_estado.es_estado_final:
        return
    if not falla.pendiente_reclasificar:
        return
    if _es_patron_bot_externo_desconexion(falla):
        return
    db.rollback()
    raise HTTPException(
        409,
        "Esta falla sigue pendiente de reclasificar (la causa real no está confirmada). "
        "Elegí el subtipo definitivo antes de cerrarla.",
    )


def _sincronizar_resolucion(falla: Falla, nuevo_estado: "FallaCatEstado | None") -> None:
    """Único punto que sincroniza fecha_resolucion + sla_cumplido con el estado
    de una falla. Antes esta regla estaba copiada por separado en update_falla
    y add_seguimiento -- si se corregía una copia y se olvidaba la otra,
    quedaban desincronizadas. Se llama siempre que estado_id cambia: al pasar
    a un estado final sella fecha_resolucion (si no la tenía) y calcula
    sla_cumplido contra el mismo límite que ya usa sla_dashboard
    (Falla.sla_limite_horas_efectivo: sla_limite_horas o el default por
    prioridad); al reabrir (estado no final) limpia ambos. sla_cumplido es
    siempre calculado, nunca manual -- ver FallaUpdate."""
    if not nuevo_estado:
        return
    if nuevo_estado.es_estado_final:
        if not falla.fecha_resolucion:
            falla.fecha_resolucion = datetime.now(timezone.utc)
        sla_hours = falla.sla_limite_horas_efectivo
        deadline = datetime(
            falla.fecha_identificacion.year,
            falla.fecha_identificacion.month,
            falla.fecha_identificacion.day,
            tzinfo=_COL_TZ,
        ) + timedelta(hours=sla_hours)
        falla.sla_cumplido = falla.fecha_resolucion <= deadline
    else:
        falla.fecha_resolucion = None
        falla.sla_cumplido = None


def _gen_codigo(db: Session) -> str:
    year = datetime.now(timezone.utc).year
    max_id = db.query(func.max(Falla.id)).scalar() or 0
    return f"FAL-{year}-{max_id + 1:05d}"


FALLA_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
DRIVE_ROOT_FOLDER_ID = "0AD_e3wIWHByDUk9PVA"

def _get_drive_service():
    import json, os
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise HTTPException(500, "Google Drive no configurado (falta GOOGLE_SERVICE_ACCOUNT_JSON)")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    res = service.files().list(
        q=q, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    folder = service.files().create(
        body=meta, fields="id",
        supportsAllDrives=True
    ).execute()
    return folder["id"]

# Average energy price COP/kWh for economic impact estimation
_PRECIO_ENERGIA_COP_KWH = 800.0

# Solar capacity factor for kWh loss estimation
_SOLAR_CAPACITY_FACTOR = 0.18


def _estimar_perdida_falla(potencia_kwp, horas_fuera: float) -> tuple[float, float]:
    """Estima (kWh perdidos, impacto COP) de una falla. `solar_hours` aproxima las
    horas productivas como ~50% del downtime (≈12 h solares por 24 h). Función pura
    y testeable; alimenta el reporte SLA/económico, por eso conviene fijarla con tests.
    """
    solar_hours = min(horas_fuera, (horas_fuera / 24) * 12) if horas_fuera > 0 else 0
    kwh_perdidos = round(potencia_kwp * _SOLAR_CAPACITY_FACTOR * solar_hours, 3) if potencia_kwp else 0.0
    impacto_cop = round(kwh_perdidos * _PRECIO_ENERGIA_COP_KWH, 2)
    return kwh_perdidos, impacto_cop


@router.get("/sla-dashboard", response_model=FallaSLADashboard)
def sla_dashboard(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """SLA monitoring dashboard with risk, overdue, and compliance metrics."""
    now = datetime.now(timezone.utc)

    # Get all open fallas with their priority info
    open_fallas = (
        db.query(Falla, FallaCatPrioridad.nivel)
        .join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        .join(FallaCatPrioridad, Falla.prioridad_id == FallaCatPrioridad.id)
        .filter(Falla.deleted_at.is_(None), ~FallaCatEstado.es_estado_final)
        .all()
    )

    en_riesgo = 0
    vencido = 0
    for falla, nivel in open_fallas:
        # nivel viene del join (no de falla.prioridad, no eager-loaded acá) --
        # mismo cálculo que Falla.sla_limite_horas_efectivo, inline para no
        # forzar un lazy-load de prioridad por fila.
        sla_hours = falla.sla_limite_horas or DEFAULT_SLA_HOURS.get(nivel, 72)
        sla_deadline = datetime(
            falla.fecha_identificacion.year,
            falla.fecha_identificacion.month,
            falla.fecha_identificacion.day,
            tzinfo=_COL_TZ,
        ) + timedelta(hours=sla_hours)

        if now > sla_deadline:
            vencido += 1
        elif now > sla_deadline - timedelta(hours=sla_hours * 0.2):
            # Within last 20% of SLA window = at risk
            en_riesgo += 1

    # Average resolution time for resolved fallas in last 90 days
    resolved_fallas = (
        db.query(Falla)
        .join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        .filter(
            Falla.deleted_at.is_(None),
            FallaCatEstado.es_estado_final == True,
            Falla.fecha_resolucion.isnot(None),
            Falla.updated_at >= now - timedelta(days=90),
        )
        .all()
    )

    total_hours = 0.0
    count_resolved = 0
    sla_met = 0
    sla_evaluated = 0
    for f in resolved_fallas:
        if f.fecha_resolucion and f.fecha_identificacion:
            start = datetime(
                f.fecha_identificacion.year,
                f.fecha_identificacion.month,
                f.fecha_identificacion.day,
                tzinfo=_COL_TZ,
            )
            hours = (f.fecha_resolucion - start).total_seconds() / 3600
            total_hours += hours
            count_resolved += 1

        if f.sla_cumplido is not None:
            sla_evaluated += 1
            if f.sla_cumplido:
                sla_met += 1

    promedio = round(total_hours / count_resolved, 1) if count_resolved else None
    cumplimiento = round(sla_met / sla_evaluated * 100, 1) if sla_evaluated else None

    return FallaSLADashboard(
        fallas_en_riesgo_sla=en_riesgo,
        fallas_sla_vencido=vencido,
        promedio_tiempo_resolucion_horas=promedio,
        cumplimiento_sla_pct=cumplimiento,
    )


@router.get("/catalogos", response_model=FallaCatalogos)
def get_catalogos(db: Session = Depends(get_db), _=Depends(get_current_user)):
    estados = db.query(FallaCatEstado).order_by(FallaCatEstado.orden).all()
    prioridades = db.query(FallaCatPrioridad).order_by(FallaCatPrioridad.nivel).all()
    tipos = (
        db.query(FallaCatTipo)
        .options(selectinload(FallaCatTipo.categoria))
        .filter(FallaCatTipo.activa == True)
        .order_by(FallaCatTipo.etiqueta)
        .all()
    )
    resoluciones = db.query(FallaCatResolucion).order_by(FallaCatResolucion.etiqueta).all()
    return {"estados": estados, "prioridades": prioridades, "tipos": tipos, "resoluciones": resoluciones}


@router.get("/estructura")
def get_estructura(_=Depends(get_current_user)):
    """Estructura canónica del reporte jerárquico (sistema → opciones/equipos/tipos).
    Fuente única que consumen el form web y la app móvil."""
    return {"categorias": ESTRUCTURA_FALLAS}



@router.get("/stats/resumen")
def stats_resumen(db: Session = Depends(get_db), _=Depends(get_current_user)):
    today = date.today()
    alert_cutoff = today - timedelta(days=7)

    def _count(*filters):
        q = db.query(func.count(Falla.id)).join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        for f in filters:
            q = q.filter(f)
        return q.scalar() or 0

    total_activas = _count(~FallaCatEstado.es_estado_final)
    en_revision   = _count(FallaCatEstado.codigo == "en_gestion")
    resueltas_mes = _count(FallaCatEstado.es_estado_final == True, Falla.updated_at >= today.replace(day=1))
    sla_base = _count(FallaCatEstado.es_estado_final == True,
                      Falla.updated_at >= today - timedelta(days=30),
                      Falla.sla_cumplido.isnot(None))
    sla_ok   = _count(FallaCatEstado.es_estado_final == True,
                      Falla.updated_at >= today - timedelta(days=30),
                      Falla.sla_cumplido == True)
    alerta   = _count(~FallaCatEstado.es_estado_final, Falla.fecha_identificacion <= alert_cutoff)

    return {
        "total_activas": total_activas,
        "en_revision": en_revision,
        "resueltas_mes": resueltas_mes,
        "cumplimiento_sla_pct": round(sla_ok / sla_base * 100) if sla_base else None,
        "alerta_7_dias": alerta,
    }


_COL_TZ = timezone(timedelta(hours=-5))


def _col_day_start_utc() -> datetime:
    """Inicio del día actual en hora de Colombia (UTC-5), expresado en UTC."""
    now_col = datetime.now(_COL_TZ)
    start_col = now_col.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_col.astimezone(timezone.utc)


@router.get("/actividad-hoy")
def actividad_hoy(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Fallas creadas hoy y fallas que cambiaron de estado hoy (hora Colombia).

    Respuesta:
      { fecha,
        creadas: [FallaOut],
        cambios_estado: [{ falla: FallaOut, estado_anterior, estado_nuevo, hora }] }
    El `estado_anterior` se deduce del seguimiento de cambio inmediatamente previo.
    """
    day_start = _col_day_start_utc()
    hoy_str = datetime.now(_COL_TZ).date().isoformat()

    creadas = (
        db.query(Falla)
        .filter(Falla.deleted_at.is_(None), Falla.created_at >= day_start)
        .options(*_FALLA_LOAD)
        .order_by(Falla.created_at.desc())
        .all()
    )

    segs_hoy = (
        db.query(FallaSeguimiento)
        .filter(
            FallaSeguimiento.estado_nuevo_id.isnot(None),
            FallaSeguimiento.created_at >= day_start,
        )
        .all()
    )
    falla_ids = {s.falla_id for s in segs_hoy}

    cambios: list[dict] = []
    if falla_ids:
        # Historial de cambios de estado de esas fallas (ordenado) para deducir el anterior.
        hist = (
            db.query(FallaSeguimiento)
            .filter(
                FallaSeguimiento.falla_id.in_(falla_ids),
                FallaSeguimiento.estado_nuevo_id.isnot(None),
            )
            .options(selectinload(FallaSeguimiento.estado_nuevo))
            .order_by(FallaSeguimiento.falla_id, FallaSeguimiento.created_at)
            .all()
        )
        por_falla: dict[int, list] = {}
        for s in hist:
            por_falla.setdefault(s.falla_id, []).append(s)

        fallas = (
            db.query(Falla)
            .filter(Falla.id.in_(falla_ids), Falla.deleted_at.is_(None))
            .options(*_FALLA_LOAD)
            .all()
        )
        fallas_map = {f.id: f for f in fallas}

        def _estado_dict(estado):
            if not estado:
                return None
            return {"codigo": estado.codigo, "etiqueta": estado.etiqueta}

        for fid, segs in por_falla.items():
            falla = fallas_map.get(fid)
            if not falla:
                continue
            today_positions = [i for i, s in enumerate(segs) if s.created_at >= day_start]
            if not today_positions:
                continue
            last_i = today_positions[-1]
            ultimo = segs[last_i]
            anterior = segs[last_i - 1] if last_i > 0 else None
            cambios.append({
                "falla": FallaOut.model_validate(falla),
                "estado_anterior": _estado_dict(anterior.estado_nuevo if anterior else None),
                "estado_nuevo": _estado_dict(ultimo.estado_nuevo),
                "hora": ultimo.created_at.isoformat(),
            })
        cambios.sort(key=lambda c: c["hora"], reverse=True)

    return {
        "fecha": hoy_str,
        "creadas": [FallaOut.model_validate(f) for f in creadas],
        "cambios_estado": cambios,
    }


@router.get("", response_model=PaginatedResponse[FallaListOut])
def list_fallas(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=5000),
    page_size: int | None = Query(None, ge=1, le=5000),
    q: str | None = None,
    buscar: str | None = None,
    estado_id: int | None = None,
    estado_codigo: str | None = None,
    prioridad_id: int | None = None,
    prioridad_codigo: str | None = None,
    tipo_codigo: str | None = None,
    proyecto_id: int | None = None,
    cliente_id: int | None = None,
    solo_alerta: bool = False,
    solo_activas: bool = False,
    activa_en_fecha: date | None = None,
    fecha_programada_desde: date | None = None,
    fecha_programada_hasta: date | None = None,
    con_fecha_programada: bool = False,
    pendiente_reclasificar: bool | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    effective_size = page_size or size
    search = q or buscar
    estado_joined = False

    query = db.query(Falla).filter(Falla.deleted_at.is_(None)).options(*_FALLA_LOAD_LISTA)

    if search:
        query = query.filter(Falla.descripcion.ilike(f"%{search}%") | Falla.codigo_interno.ilike(f"%{search}%"))
    if estado_id:
        query = query.filter(Falla.estado_id == estado_id)
    if estado_codigo:
        query = query.join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        estado_joined = True
        query = query.filter(FallaCatEstado.codigo == estado_codigo)
    if prioridad_id:
        query = query.filter(Falla.prioridad_id == prioridad_id)
    if prioridad_codigo:
        query = (query.join(FallaCatPrioridad, Falla.prioridad_id == FallaCatPrioridad.id)
                      .filter(FallaCatPrioridad.codigo == prioridad_codigo))
    if tipo_codigo:
        query = (query.join(FallaCatTipo, Falla.tipo_id == FallaCatTipo.id)
                      .filter(FallaCatTipo.codigo == tipo_codigo))
    if proyecto_id:
        query = query.filter(Falla.proyecto_id == proyecto_id)
    if cliente_id:
        # Proyectos donde este cliente es inversionista vigente (fecha_fin nula o futura).
        hoy = date.today()
        client_project_ids = (
            db.query(ProyectoInversionista.proyecto_id)
            .filter(
                ProyectoInversionista.cliente_id == cliente_id,
                (ProyectoInversionista.fecha_fin.is_(None)) | (ProyectoInversionista.fecha_fin >= hoy),
            )
            .subquery()
        )
        query = query.filter(Falla.proyecto_id.in_(client_project_ids))
    if activa_en_fecha:
        # "Activa a la fecha X" (no "activa ahora mismo") -- para mostrar,
        # en el detalle de un día ya clasificado, las fallas que estaban
        # abiertas EN ESE MOMENTO, no las que están abiertas hoy consultando
        # en vivo (ver Reporte de Energía -> "Fallas activas del proyecto").
        # fecha_resolucion NULL solo cuenta como "sigue abierta" si el estado
        # REAL tampoco es final -- si no, una falla cerrada hace meses sin
        # fecha_resolucion (dato legacy, ver A4/backfill_sla_cumplido) coló
        # como "activa" para cualquier fecha que se consultara.
        if not estado_joined:
            query = query.join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
            estado_joined = True
        query = query.filter(
            Falla.fecha_identificacion <= activa_en_fecha,
            (Falla.fecha_resolucion.is_(None) & ~FallaCatEstado.es_estado_final)
            | (func.date(Falla.fecha_resolucion) >= activa_en_fecha),
        )
    if solo_activas:
        if not estado_joined:
            query = query.join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
            estado_joined = True
        query = query.filter(~FallaCatEstado.es_estado_final)
    if solo_alerta:
        alert_cutoff = date.today() - timedelta(days=7)
        if not estado_joined:
            query = query.join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        query = query.filter(~FallaCatEstado.es_estado_final, Falla.fecha_identificacion <= alert_cutoff)
    if fecha_programada_desde:
        query = query.filter(Falla.fecha_programada >= fecha_programada_desde)
    if fecha_programada_hasta:
        query = query.filter(Falla.fecha_programada <= fecha_programada_hasta)
    if con_fecha_programada:
        query = query.filter(Falla.fecha_programada.isnot(None))
    if pendiente_reclasificar is not None:
        query = query.filter(Falla.pendiente_reclasificar == pendiente_reclasificar)
        if pendiente_reclasificar:
            # Cola de pendientes REALES: excluye el patrón de un bot externo
            # (no vive en este repo, no se puede tocar su lógica) que reporta
            # diagnósticos eléctricos específicos -- desbalance de tensión,
            # reconectador en cero, frontera/inversores sin datos -- pero
            # siempre bajo el subtipo genérico red.desconexion_sin_identificar,
            # así que nunca se resuelve solo vía el flujo normal de
            # reclasificación (verificado: 833 de 851 casos reales, todas sin
            # alarma_monitoreo_id -- ese campo solo lo llenaba el motor MGS
            # interno, cuya creación automática de fallas se eliminó el
            # 2026-09-02; queda como marca de las fallas históricas que creó).
            # Auditoría 2026-09-02. Mismas constantes que _bloquear_cierre_si_pendiente.
            query = query.filter(~(
                Falla.alarma_monitoreo_id.is_(None)
                & (Falla.categoria_codigo == _BOT_DESCONEXION_CATEGORIA)
                & (Falla.subtipo_codigo == _BOT_DESCONEXION_SUBTIPO)
            ))

    total = query.count()
    items = query.order_by(Falla.created_at.desc()).offset((page - 1) * effective_size).limit(effective_size).all()
    return {"items": items, "total": total, "page": page, "size": effective_size, "pages": -(-total // effective_size)}


_notif_logger = logging.getLogger("fallas.notificacion")


def _enviar_notificacion(
    falla,
    accion: str,
    usuario_nombre: str,
    db,
) -> dict:
    """
    Envía email de notificación y retorna el resultado.
    Nunca lanza excepción — la falla se guarda siempre.
    Retorna: {"ok": bool, "enviados": [...], "errores": [...], "sin_correos": bool}
    """
    from app.services.email_service import send_falla_notification_email
    from app.services.contactos import get_contactos
    from app.core.config import settings
    from datetime import datetime, timezone

    correos = get_contactos(db, "operacional", proyecto_id=falla.proyecto_id)
    ts = datetime.now(timezone.utc).isoformat()

    if not correos:
        _notif_logger.warning(
            "[%s] usuario=%s falla=%s accion=%s — SIN correos operacionales para proyecto %s",
            ts, usuario_nombre, falla.codigo_interno, accion, falla.proyecto_id,
        )
        return {"ok": False, "enviados": [], "errores": ["Sin correos operacionales configurados para este cliente"], "sin_correos": True}

    resultado = send_falla_notification_email(
        to_emails=correos,
        codigo_falla=falla.codigo_interno,
        proyecto_nombre=falla.proyecto.nombre_comercial if falla.proyecto else str(falla.proyecto_id),
        descripcion=falla.descripcion or "",
        estado_codigo=falla.estado.codigo if falla.estado else "",
        estado_etiqueta=falla.estado.etiqueta if falla.estado else "",
        prioridad_etiqueta=falla.prioridad.etiqueta if falla.prioridad else "",
        tipo_nombre=titulo_falla(falla),
        fecha_identificacion=str(falla.fecha_identificacion or ""),
        hora_identificacion=str(falla.hora_identificacion or ""),
        fecha_programada=str(falla.fecha_programada or ""),
        registrado_por=usuario_nombre,
        accion=accion,
        proyecto_id=falla.proyecto_id,
    )
    resultado["sin_correos"] = False

    if resultado.get("ok"):
        _notif_logger.info(
            "[%s] usuario=%s falla=%s accion=%s — ENVIADO a: %s",
            ts, usuario_nombre, falla.codigo_interno, accion, resultado["enviados"],
        )
    else:
        _notif_logger.error(
            "[%s] usuario=%s falla=%s accion=%s — ERROR: %s",
            ts, usuario_nombre, falla.codigo_interno, accion, resultado["errores"],
        )

    return resultado


@router.post("", response_model=FallaOut, status_code=201)
def create_falla(
    data: FallaCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    dump = data.model_dump()
    fotos = dump.pop("fotos_urls", None)
    intervalos = dump.pop("intervalos", None)
    inversores = dump.pop("inversores", None)  # no es columna → se procesa aparte
    generar_impacto = dump.pop("generar_impacto", False)  # dispara MantenimientoImpacto

    # Camino estructurado: validar antes de crear nada.
    categoria_codigo = dump.get("categoria_codigo")
    if categoria_codigo:
        _validar_clasificacion_payload(categoria_codigo, dump.get("subtipo_codigo"), inversores)

    falla = Falla(
        **dump,
        codigo_interno=f"TMP-{uuid.uuid4().hex[:12]}",
        registrado_por_id=current_user.id,
        fotos_urls=fotos if fotos else None,
    )
    db.add(falla)
    try:
        db.flush()  # asigna falla.id por autoincremento (evita colisiones de código)
    except IntegrityError as e:
        db.rollback()
        raise _integrity_error_a_http(e)
    falla.codigo_interno = f"FAL-{datetime.now(timezone.utc).year}-{falla.id:05d}"
    _sync_intervalos(falla, intervalos, db)
    if categoria_codigo:
        _aplicar_clasificacion(falla, inversores or [], db)
    db.commit()

    # Notificar a todos los coordinadores
    from app.api.v1.notificaciones import crear_notificacion
    from app.models.usuarios import RolEnum
    coordinadores = db.query(Usuario).filter(
        Usuario.rol == RolEnum.coordinador,
        Usuario.activo == True,
    ).all()
    proyecto_nombre = falla.proyecto.nombre_comercial if falla.proyecto else f"Proyecto {falla.proyecto_id}"
    for coord in coordinadores:
        crear_notificacion(
            db=db,
            usuario_id=coord.id,
            tipo="accion",
            titulo="Nueva falla registrada",
            mensaje=f"{falla.codigo_interno} — {proyecto_nombre}: {(falla.descripcion or '')[:80]}",
        )
    if coordinadores:
        db.commit()

    # Alarmas de comunicación (frontera / inversores / total) — no bloqueante
    _alarmas_post_guardado(falla.id, db)

    # Impacto de mantenimiento (opcional): si la falla derivó en una intervención,
    # crea el registro con energía perdida/impacto calculados. No bloqueante.
    if generar_impacto:
        _generar_impacto_mantenimiento(falla, current_user, db)

    return _get_or_404(falla.id, db)


def _generar_impacto_mantenimiento(falla: Falla, current_user, db: Session) -> None:
    """Crea un MantenimientoImpacto ligado a la falla usando su ventana temporal.

    Ventana: [fecha_ocurrencia, fecha_resolucion]; si falta la ocurrencia se usa
    fecha/hora de identificación, y si falta la resolución se cierra con la hora
    actual. Silenciosa ante errores: nunca debe tumbar la creación de la falla.
    """
    from app.models.mantenimiento_impacto import MantenimientoImpacto
    from app.services.impact_calculator import ImpactCalculator

    try:
        inicio = falla.fecha_ocurrencia
        if inicio is None and falla.fecha_identificacion:
            inicio = datetime.combine(
                falla.fecha_identificacion, falla.hora_identificacion or time(0, 0),
                tzinfo=_COL_TZ,
            )
        if inicio is None:
            return
        fin = falla.fecha_resolucion or datetime.now(_COL_TZ)
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=_COL_TZ)
        if fin.tzinfo is None:
            fin = fin.replace(tzinfo=_COL_TZ)
        if fin < inicio:
            fin = inicio

        m = MantenimientoImpacto(
            proyecto_id=falla.proyecto_id,
            falla_id=falla.id,
            maintenance_type="unscheduled",  # nace de una falla → no programado
            start_time=inicio,
            end_time=fin,
            created_by=getattr(current_user, "id", None),
        )
        metrics = ImpactCalculator(db).calculate_impact(
            proyecto_id=m.proyecto_id, start=m.start_time, end=m.end_time,
        )
        m.expected_generation_kwh = metrics["expected_generation_kwh"]
        m.actual_generation_kwh = metrics["actual_generation_kwh"]
        m.lost_energy_kwh = metrics["lost_energy_kwh"]
        m.financial_impact_cop = metrics["financial_impact_cop"]
        m.ppa_penalty_risk_flag = metrics["ppa_penalty_risk_flag"]
        db.add(m)
        db.commit()
    except Exception:
        db.rollback()
        logging.getLogger(__name__).warning(
            "No se pudo generar impacto de mantenimiento para falla %s", falla.id,
            exc_info=True,
        )


# backfill_tipos_estructurados() / POST /backfill-tipos vivieron acá --
# eliminados 2026-09-02 junto con tipo_libre. Existían para mantener
# tipo_id/tipo_libre sincronizados con la clasificación estructurada; una
# verificación real contra producción (dry-run) mostró 0 correcciones sobre
# 5.086 fallas estructuradas -- ya no había nada que corregir, así que
# escanear la tabla completa en cada arranque (_run_fallas_tipo_backfill en
# main.py) dejó de tener sentido. Si `_aplicar_clasificacion()` cambia de
# forma que vuelva a desincronizar tipo_id, el fix es una migración de
# Alembic puntual (mismo criterio que ya se aplicó para el propio
# tipo_migration), no un job permanente "por si acaso".


def backfill_sla_cumplido(db: Session, dry_run: bool = False) -> dict:
    """Recalcula sla_cumplido para TODAS las fallas ya resueltas (estado final
    con fecha_resolucion), incluidas las que ya tenían un valor manual puesto
    -- ese subconjunto era justo el sesgo que se elimina al volver el campo
    100% calculado (ver _sincronizar_resolucion). Idempotente: solo cuenta
    como corregida si el valor cambia."""
    fallas = (
        db.query(Falla)
        .join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
        .options(selectinload(Falla.prioridad))
        .filter(
            Falla.deleted_at.is_(None),
            FallaCatEstado.es_estado_final.is_(True),
            Falla.fecha_resolucion.isnot(None),
        )
        .all()
    )
    cambiadas = []
    for f in fallas:
        anterior = f.sla_cumplido
        sla_hours = f.sla_limite_horas_efectivo
        deadline = datetime(
            f.fecha_identificacion.year, f.fecha_identificacion.month, f.fecha_identificacion.day,
            tzinfo=_COL_TZ,
        ) + timedelta(hours=sla_hours)
        f.sla_cumplido = f.fecha_resolucion <= deadline
        if f.sla_cumplido != anterior:
            cambiadas.append({
                "codigo": f.codigo_interno,
                "sla_cumplido_anterior": anterior,
                "sla_cumplido_nuevo": f.sla_cumplido,
            })
    if dry_run:
        db.rollback()
    elif cambiadas:
        db.commit()
    return {
        "dry_run": dry_run,
        "total_resueltas": len(fallas),
        "corregidas": len(cambiadas),
        "detalle": cambiadas[:200],
    }


@router.post("/backfill-sla")
def backfill_sla_endpoint(
    dry_run: bool = Query(True, description="Solo previsualizar sin escribir"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Recalcula sla_cumplido para todas las fallas ya resueltas, ahora que el
    campo es siempre calculado (antes se podía fijar a mano). Con dry_run=true
    solo reporta qué cambiaría."""
    return backfill_sla_cumplido(db, dry_run=dry_run)


# ─────────────────────────────────────────────────────────────────────────────
# Consulta pública por proyecto (consumidores externos con API Key)
# ─────────────────────────────────────────────────────────────────────────────

def _resolver_proyecto(db: Session, proyecto_id, api_id_unergy, nombre) -> Proyecto:
    """Resuelve la planta por id interno, llave de la API de Unergy o nombre.

    Exige exactamente una llave. El match por nombre lo hace `_id_por_nombre` de
    proyectos.py -- el mismo que respalda GET /proyectos/buscar: exacto sobre el
    nombre normalizado (tolera tildes/mayúsculas/guiones, NO es difuso), con 409
    y la lista de candidatos si el nombre es ambiguo.

    Se reusa en vez de reimplementarse. Una versión propia con prefiltro ILIKE
    sobre el texto crudo es sensible a tildes: "Santa Fe 2" no traía a "Santa Fé
    2" como candidata, así que un nombre ambiguo se resolvía como único y la
    integración se llevaba las fallas de la planta equivocada sin enterarse --
    justo el caso que el 409 existe para atrapar.
    """
    llaves = [k for k in (proyecto_id, api_id_unergy, nombre) if k not in (None, "")]
    if len(llaves) != 1:
        raise HTTPException(422, "Indique exactamente una de: proyecto_id, api_id_unergy, nombre.")

    if proyecto_id is not None:
        proyecto = db.query(Proyecto).filter(
            Proyecto.id == proyecto_id, Proyecto.deleted_at.is_(None)).first()
        if not proyecto:
            raise HTTPException(404, f"No existe un proyecto con id {proyecto_id}.")
        return proyecto

    if api_id_unergy:
        proyecto = db.query(Proyecto).filter(
            Proyecto.sub_project == api_id_unergy, Proyecto.deleted_at.is_(None)).first()
        if not proyecto:
            raise HTTPException(404, f"No existe un proyecto con api_id_unergy '{api_id_unergy}'.")
        return proyecto

    from app.api.v1.proyectos import _id_por_nombre
    return db.get(Proyecto, _id_por_nombre(db, nombre))


@router.get("/por-proyecto")
def fallas_por_proyecto(
    proyecto_id: int | None = Query(
        None, description="Id interno de la planta en la Plataforma de Operaciones."),
    api_id_unergy: str | None = Query(
        None, description="Llave de la planta en la API de generacion de Unergy "
                          "(el `sub_project`). Alternativa a proyecto_id."),
    nombre: str | None = Query(
        None, description="Nombre exacto de la planta (sin distinguir tildes ni "
                          "mayusculas). Si es ambiguo devuelve 409."),
    estado: str = Query(
        "vigente",
        description="Cubeta de estado a consultar: vigente | programado | terminado | todas."),
    desde: date | None = Query(
        None, description="Filtra por fecha de identificacion >= esta fecha (YYYY-MM-DD)."),
    hasta: date | None = Query(
        None, description="Filtra por fecha de identificacion <= esta fecha (YYYY-MM-DD)."),
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fallas de una planta, agrupadas en tres estados de cara al consumidor.

    Los estados internos son seis (`abierta`, `en_gestion`, `en_espera`,
    `programado`, `cerrada`, `sin_solucion`). Aca se traducen a las tres
    cubetas que pidio la integracion:

      · **vigente**    -> la falla sigue viva (abierta / en gestion / en espera)
      · **programado** -> hay intervencion agendada (ver `fecha_programada`)
      · **terminado**  -> ya se cerro (`cerrada` o `sin_solucion`)

    `todas` trae las tres. El `resumen` siempre trae el conteo de las tres
    cubetas, sin importar cual se haya filtrado, para que el consumidor sepa
    que mas hay sin pedir otra pagina.

    El identificador de la planta puede ser `proyecto_id`, `api_id_unergy`
    (el `sub_project`, la misma llave de /comercial/proyectos-operando) o
    `nombre` exacto. Va exactamente uno.
    """
    grupo = (estado or "").strip().lower()
    if grupo not in GRUPOS_CONSULTABLES:
        raise HTTPException(
            422,
            f"estado '{estado}' no es valido. Use uno de: "
            f"{', '.join(GRUPOS_CONSULTABLES)}.",
        )
    if desde and hasta and desde > hasta:
        raise HTTPException(422, "El parametro 'desde' no puede ser posterior a 'hasta'.")

    proyecto = _resolver_proyecto(db, proyecto_id, api_id_unergy, nombre)

    catalogo_estados = db.query(FallaCatEstado).order_by(FallaCatEstado.orden).all()

    base = db.query(Falla).filter(
        Falla.proyecto_id == proyecto.id,
        Falla.deleted_at.is_(None),
    )
    if desde:
        base = base.filter(Falla.fecha_identificacion >= desde)
    if hasta:
        base = base.filter(Falla.fecha_identificacion <= hasta)

    # Resumen de las tres cubetas sobre el MISMO universo filtrado por fecha,
    # en una sola consulta agrupada (no una por cubeta).
    conteo_por_codigo = dict(
        base.join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
            .with_entities(FallaCatEstado.codigo, func.count(Falla.id))
            .group_by(FallaCatEstado.codigo)
            .all()
    )
    resumen = {g: 0 for g in GRUPOS}
    for e in catalogo_estados:
        resumen[grupo_de_estado(e.codigo, e.es_estado_final)] += conteo_por_codigo.get(e.codigo, 0)
    resumen["total"] = sum(resumen[g] for g in GRUPOS)

    if grupo == GRUPO_TODAS:
        codigos = [e.codigo for e in catalogo_estados]
    else:
        codigos = codigos_de_grupo(catalogo_estados, grupo)

    query = (base.options(*_FALLA_LOAD_LISTA)
                 .join(FallaCatEstado, Falla.estado_id == FallaCatEstado.id)
                 .filter(FallaCatEstado.codigo.in_(codigos)))

    total = query.count()
    items = (query.order_by(Falla.fecha_identificacion.desc(), Falla.id.desc())
                  .offset((page - 1) * size).limit(size).all())

    return {
        "proyecto": proyecto_publico(proyecto),
        "estado_consultado": grupo,
        "estados_incluidos": codigos,
        "significado_estados": DESCRIPCION_GRUPOS,
        "filtro_fechas": {"desde": desde, "hasta": hasta},
        "resumen": resumen,
        "total": total,
        "page": page,
        "size": size,
        "pages": -(-total // size) if total else 0,
        "items": [falla_publica(f) for f in items],
    }


@router.get("/{id}", response_model=FallaOut)
def get_falla(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _get_or_404(id, db)


@router.patch("/{id}", response_model=FallaOut)
def update_falla(
    id: int,
    data: FallaUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    falla = db.query(Falla).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")
    dump = data.model_dump(exclude_unset=True)

    # Los intervalos de disparo se sincronizan aparte (no son una columna).
    sync_ints = "intervalos" in dump
    intervalos = dump.pop("intervalos", None)
    # Inversores afectados tampoco son columna.
    inversores_touched = "inversores" in dump
    inversores = dump.pop("inversores", None)

    for k, v in dump.items():
        setattr(falla, k, v)

    if sync_ints:
        _sync_intervalos(falla, intervalos or [], db)

    # Reclasificación / edición estructurada: si se tocó la categoría o los
    # inversores, revalidar y recalcular tipo_id/flags/clasificación.
    estructura_touched = (
        inversores_touched
        or any(k in dump for k in (
            "categoria_codigo", "subtipo_codigo", "subtipo_detalle",
            "frontera_afecta_medicion", "frontera_perdida_comunicacion",
        ))
    )
    if "categoria_codigo" in dump and dump["categoria_codigo"] is None:
        # Limpiar la clasificación estructurada explícitamente (categoria_codigo: null).
        # El setattr de arriba ya puso falla.categoria_codigo en None -- sin este bloque,
        # el guard de abajo (`estructura_touched and falla.categoria_codigo`) nunca es
        # cierto para este caso y todo lo derivado (clasificacion, subtipo_codigo,
        # banderas, pendiente_reclasificar) quedaba congelado con el valor viejo,
        # contradiciendo el categoria_codigo=null recién guardado (auditoría 2026-09-02).
        falla.subtipo_codigo = None
        falla.subtipo_detalle = None
        falla.pendiente_reclasificar = False
        falla.frontera_afecta_medicion = None
        falla.frontera_perdida_comunicacion = None
        falla.inversores_perdida_comunicacion = None
        falla.clasificacion = None
        if "tipo_id" not in dump:
            falla.tipo_id = None
        db.flush()
        db.query(FallaInversor).filter(FallaInversor.falla_id == falla.id).delete(synchronize_session=False)
    elif estructura_touched and falla.categoria_codigo:
        db.flush()  # asegura falla.id para sincronizar inversores
        # Validar: para inversores solo si llegó lista nueva (si no, los datos
        # existentes ya eran válidos). Para red/frontera/eventos validar subtipo.
        if falla.categoria_codigo == "inversores":
            if inversores_touched:
                _validar_clasificacion_payload(falla.categoria_codigo, falla.subtipo_codigo, inversores)
        else:
            _validar_clasificacion_payload(falla.categoria_codigo, falla.subtipo_codigo, None)
        _aplicar_clasificacion(falla, inversores if inversores_touched else None, db)

    # Sellar fecha+hora de solución y calcular sla_cumplido automáticamente al
    # cerrar la falla (estado final); al reabrir se limpian ambos. Ver
    # _sincronizar_resolucion -- único punto de esta regla.
    if "estado_id" in dump and dump["estado_id"] is not None:
        nuevo_estado = db.get(FallaCatEstado, dump["estado_id"])
        _bloquear_cierre_si_pendiente(falla, nuevo_estado, db)
        _sincronizar_resolucion(falla, nuevo_estado)

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise _integrity_error_a_http(e)

    # Reevaluar alarmas de comunicación tras el cambio — no bloqueante
    _alarmas_post_guardado(id, db)

    return _get_or_404(id, db)


@router.post("/{id}/notificar")
def notificar_falla(
    id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """
    Envía la notificación por correo para la falla indicada.
    Se llama desde el frontend tras guardar cuando notificacion=True.
    Retorna: {"ok", "enviados", "errores", "sin_correos"}
    """
    falla = _get_or_404(id, db)
    accion = "cerrada" if falla.estado and falla.estado.es_estado_final else "creada"
    return _enviar_notificacion(
        falla=falla,
        accion=accion,
        usuario_nombre=current_user.nombre,
        db=db,
    )


@router.delete("/{id}", status_code=204)
def delete_falla(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    falla = db.query(Falla).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")
    falla.deleted_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/{id}/seguimientos", response_model=FallaSeguimientoOut, status_code=201)
def add_seguimiento(
    id: int,
    data: FallaSeguimientoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    falla = db.query(Falla).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")

    seg = FallaSeguimiento(
        falla_id=id,
        usuario_id=current_user.id,
        nota=data.nota,
        estado_nuevo_id=data.estado_nuevo_id,
    )
    if data.estado_nuevo_id:
        nuevo_estado = db.get(FallaCatEstado, data.estado_nuevo_id)
        _bloquear_cierre_si_pendiente(falla, nuevo_estado, db)
        falla.estado_id = data.estado_nuevo_id
        _sincronizar_resolucion(falla, nuevo_estado)

    db.add(seg)
    db.commit()
    db.refresh(seg)

    return (
        db.query(FallaSeguimiento)
        .options(
            selectinload(FallaSeguimiento.usuario),
            selectinload(FallaSeguimiento.estado_nuevo),
        )
        .filter(FallaSeguimiento.id == seg.id)
        .first()
    )


# ── Feature 4: Impact on generation ─────────────────────────────────────────
@router.get("/{id}/impacto", response_model=FallaImpacto)
def get_falla_impacto(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """
    Estimate generation loss and economic impact for a falla based on
    project capacity and downtime duration.
    """
    falla = db.query(Falla).options(selectinload(Falla.proyecto)).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")

    proyecto = falla.proyecto
    potencia_kwp = float(proyecto.potencia_instalada_kwp or 0)

    # Calculate downtime hours
    start = datetime(
        falla.fecha_identificacion.year,
        falla.fecha_identificacion.month,
        falla.fecha_identificacion.day,
        tzinfo=_COL_TZ,
    )
    if falla.hora_identificacion:
        start = start.replace(
            hour=falla.hora_identificacion.hour,
            minute=falla.hora_identificacion.minute,
        )

    end = falla.fecha_resolucion or datetime.now(timezone.utc)
    horas_fuera = max(0, (end - start).total_seconds() / 3600)

    kwh_perdidos, impacto_cop = _estimar_perdida_falla(potencia_kwp, horas_fuera)

    # Persist the estimate back to the falla if not already set
    if falla.kwh_perdidos_estimado is None:
        falla.kwh_perdidos_estimado = kwh_perdidos
        falla.impacto_economico_cop = impacto_cop
        db.commit()

    return FallaImpacto(
        falla_id=falla.id,
        proyecto_nombre=proyecto.nombre_comercial,
        potencia_instalada_kwp=potencia_kwp or None,
        horas_fuera=round(horas_fuera, 1),
        kwh_perdidos_estimado=kwh_perdidos,
        impacto_economico_cop=impacto_cop,
    )


# ── Feature 6: File attachments for fallas → Google Drive ────────────────────

def _fotos_as_objects(raw_list: list) -> list[dict]:
    """Normaliza la lista almacenada en fotos_urls a objetos con campos completos.
    Soporta tanto el formato legado (strings de URL) como el nuevo (dicts)."""
    result = []
    for item in raw_list:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            # Formato legado: "url#nombre"
            if "#" in item:
                url_part, nombre_part = item.rsplit("#", 1)
            else:
                url_part, nombre_part = item, item.split("/")[-1]
            result.append({
                "id": url_part.split("/d/")[-1].split("/")[0] if "/d/" in url_part else uuid.uuid4().hex,
                "nombre": nombre_part,
                "url": url_part,
                "tamaño": None,
                "tipo_mime": None,
                "created_at": None,
            })
    return result


@router.get("/{id}/archivos")
def get_falla_archivos(
    id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    falla = db.query(Falla).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")
    return _fotos_as_objects(falla.fotos_lista)


@router.delete("/{id}/archivos/{archivo_id}")
def delete_falla_archivo(
    id: int,
    archivo_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    falla = db.query(Falla).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")

    items = _fotos_as_objects(falla.fotos_lista)
    nueva_lista = [i for i in items if i.get("id") != archivo_id]
    if len(nueva_lista) == len(items):
        raise HTTPException(404, "Archivo no encontrado")

    # Intentar eliminar de Drive (no crítico si falla -- el registro se quita
    # de la BD igual, pero queda log para poder limpiar huérfanos en Drive)
    try:
        service = _get_drive_service()
        service.files().delete(fileId=archivo_id, supportsAllDrives=True).execute()
    except Exception:
        logging.getLogger("fallas").warning(
            "No se pudo borrar de Drive el archivo %s de la falla %s (queda huérfano en Drive)",
            archivo_id, falla.codigo_interno, exc_info=True,
        )

    falla.fotos_urls = nueva_lista if nueva_lista else None
    db.commit()
    return {"status": "ok"}


@router.post("/{id}/archivos")
@router.post("/{id}/attachments")
async def upload_falla_attachment(
    id: int,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    falla = db.query(Falla).options(selectinload(Falla.proyecto)).filter(Falla.id == id, Falla.deleted_at.is_(None)).first()
    if not falla:
        raise HTTPException(404, "Falla no encontrada")

    contenido = await archivo.read()
    tamaño = len(contenido)
    if tamaño > FALLA_MAX_FILE_SIZE:
        raise HTTPException(400, "El archivo supera el límite de 20 MB")

    import io
    from googleapiclient.http import MediaIoBaseUpload

    try:
        service = _get_drive_service()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error iniciando Drive: {e}")

    # Estructura: Raíz → Proyecto → Código falla
    proyecto_nombre = falla.proyecto.nombre_comercial if falla.proyecto else f"Proyecto {falla.proyecto_id}"
    try:
        proyecto_folder_id = _get_or_create_folder(service, proyecto_nombre, DRIVE_ROOT_FOLDER_ID)
        falla_folder_id    = _get_or_create_folder(service, falla.codigo_interno or f"FAL-{id}", proyecto_folder_id)
    except Exception as e:
        raise HTTPException(500, f"Error accediendo carpeta Drive: {e}")

    nombre_original = archivo.filename or f"archivo_{uuid.uuid4().hex}"
    tipo_mime = archivo.content_type or "application/octet-stream"
    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype=tipo_mime)
    file_meta = {"name": nombre_original, "parents": [falla_folder_id]}
    try:
        uploaded = service.files().create(
            body=file_meta, media_body=media, fields="id, webViewLink",
            supportsAllDrives=True
        ).execute()
    except Exception as e:
        raise HTTPException(500, f"Error subiendo archivo a Drive: {e}")

    file_id  = uploaded["id"]
    view_url = uploaded.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")

    nuevo_archivo = {
        "id": file_id,
        "nombre": nombre_original,
        "url": view_url,
        "tamaño": tamaño,
        "tipo_mime": tipo_mime,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Normalizar lista existente (por si hay strings legados) y agregar nuevo
    items = _fotos_as_objects(falla.fotos_lista)
    items.append(nuevo_archivo)
    falla.fotos_urls = items
    db.commit()

    return nuevo_archivo
