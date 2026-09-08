from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError
from app.core.database import get_db
from app.api.v1.auth import get_current_user
from app.models import Proyecto
from app.utils.nombre_matching import mejor_candidato, normalizar
from app.models.proyectos import (
    ProyectoInversionista, ProyectoInfoTecnica,
    ProyectoInversor, ProyectoPendienteIgnorado,
)
from app.models.contactos import ProyectoAreaContacto
from app.models.clientes import Cliente
from app.models.fronteras import Frontera
from app.schemas.proyectos import (
    ProyectoCreate, ProyectoUpdate, ProyectoOut, ProyectoListaResponse,
    ProyectoInversionistaCreate, ProyectoInversionistaUpdate, ProyectoInversionistaOut,
    ProyectoInfoTecnicaCreate, ProyectoInfoTecnicaOut,
    ProyectoInversorCreate, ProyectoInversorUpdate, ProyectoInversorOut,
    ProyectoAreaContactoSet, ProyectoAreaContactoOut,
    ProyectoPendienteOut, ProyectoPendienteConfirmar,
)
from app.schemas.common import PaginatedResponse
from app.services.mgs.gaia_client import GaiaClient
from app.services.operadores_red_sync import sincronizar_operador_red
from app.services import gen_promedio
from app.services.proyectos_pendientes import _generacion_real_por_frt, resolver_pendientes
from app.services.proyectos_backfill_unergy import sincronizar_datos_unergy_si_aplica
from app.services.tsf_sync import sincronizar_ubicacion_tsf_si_aplica
from app.services.audit import registrar_borrado
from app.services.proyectos_backfill_solenium import sincronizar_info_tecnica_solenium_si_aplica
from app.services.proyectos_backfill_solarview import sincronizar_project_id_solarview_si_aplica

router = APIRouter(prefix="/proyectos", tags=["Proyectos"])


def _get_proyecto_or_404(id: int, db: Session) -> Proyecto:
    p = (
        db.query(Proyecto)
        .options(
            selectinload(Proyecto.inversionistas).selectinload(ProyectoInversionista.cliente),
            selectinload(Proyecto.info_tecnica),
            selectinload(Proyecto.inversores),
            selectinload(Proyecto.area_contactos),
            selectinload(Proyecto.ppa_contratos),
            selectinload(Proyecto.operador),
            selectinload(Proyecto.fronteras).selectinload(Frontera.operador),
        )
        .filter(Proyecto.id == id)
        .first()
    )
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")
    return p


# ── Proyectos ─────────────────────────────────────────────────────────────────

SERVICIO_FILTER_MAP = {
    "operacion": Proyecto.srv_operacion,
    "representacion": Proyecto.srv_representacion,
    "cgm": Proyecto.srv_cgm,
    "ppa": Proyecto.srv_ppa,
    "promotor": Proyecto.srv_promotor,
    "rec": Proyecto.srv_rec,
}


@router.get("", response_model=PaginatedResponse[ProyectoOut])
def list_proyectos(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=500),
    q: str | None = None,
    estado: str | None = None,
    tipo_proyecto: str | None = None,
    portafolio_id: int | None = None,
    servicio: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = db.query(Proyecto).filter(Proyecto.deleted_at.is_(None)).options(
        selectinload(Proyecto.inversionistas).selectinload(ProyectoInversionista.cliente),
        selectinload(Proyecto.info_tecnica),
        selectinload(Proyecto.inversores),
        selectinload(Proyecto.area_contactos),
        selectinload(Proyecto.ppa_contratos),
        selectinload(Proyecto.operador),
        selectinload(Proyecto.fronteras).selectinload(Frontera.operador),
    )
    if q:
        query = query.filter(Proyecto.nombre_comercial.ilike(f"%{q}%"))
    if estado:
        query = query.filter(Proyecto.estado == estado)
    if tipo_proyecto:
        query = query.filter(Proyecto.tipo_proyecto == tipo_proyecto)
    if portafolio_id:
        query = query.filter(Proyecto.portafolio_id == portafolio_id)
    if servicio and servicio in SERVICIO_FILTER_MAP:
        query = query.filter(SERVICIO_FILTER_MAP[servicio] == True)
    total = query.count()
    items = query.order_by(Proyecto.nombre_comercial).offset((page - 1) * size).limit(size).all()
    return {"items": items, "total": total, "page": page, "size": size, "pages": -(-total // size)}


def _buscar_duplicado_por_nombre(
    db: Session, nombre_comercial: str | None, tipo_proyecto: str | None = None,
) -> Proyecto | None:
    """Busca un proyecto existente con nombre parecido, via solapamiento de
    tokens + similitud de texto (mismo algoritmo de app/utils/nombre_matching.py
    que se usa para reconciliar Quoia/Solenium/GESCON) -- detecta parecidos
    aunque no sea un caso de "un nombre contenido en el otro" (p. ej. "AGGE
    Extractora Monterrey" vs "AGGE Frontera Monterrey").

    Si se pasa tipo_proyecto, solo compara contra proyectos del mismo tipo --
    reduce falsos positivos entre proyectos de naturaleza distinta que
    comparten palabras en el nombre.

    Esto es deliberadamente permisivo (puede marcar como "parecidos" dos fases
    reales distintas de un mismo desarrollo, p. ej. "Chinú Sur" y "Chinú Sur 2"):
    es aceptable porque el aviso no bloquea -- la persona puede confirmar
    "crear de todos modos" con un clic si de verdad es un proyecto distinto.
    """
    if not nombre_comercial:
        return None
    q = db.query(Proyecto).filter(Proyecto.deleted_at.is_(None))
    if tipo_proyecto:
        q = q.filter(Proyecto.tipo_proyecto == tipo_proyecto)
    candidatos = [(c, [c.nombre_comercial]) for c in q.all()]
    item, _score = mejor_candidato(nombre_comercial, candidatos)
    return item


@router.post("", response_model=ProyectoOut, status_code=201)
def create_proyecto(
    data: ProyectoCreate,
    forzar: bool = Query(False, description="true: crear igual aunque exista un proyecto con nombre muy parecido"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    payload = data.model_dump()
    _verificar_unicos(db, payload)

    if not forzar:
        duplicado = _buscar_duplicado_por_nombre(db, payload.get("nombre_comercial"), payload.get("tipo_proyecto"))
        if duplicado:
            # detail estructurado (no un string plano como los demás 409 de este
            # archivo): el frontend lo usa para ofrecer "crear de todos modos"
            # (reintenta con forzar=true) en vez de solo mostrar un toast, ya que
            # a diferencia de un choque de columna UNIQUE, este es un aviso, no
            # un error real de datos.
            raise HTTPException(
                409,
                {
                    "mensaje": (
                        f"Ya existe un proyecto con un nombre muy parecido: "
                        f"'{duplicado.nombre_comercial}' (ID {duplicado.id})."
                    ),
                    "duplicado_nombre": True,
                    "candidato_id": duplicado.id,
                    "candidato_nombre": duplicado.nombre_comercial,
                },
            )

    proyecto = Proyecto(**payload)
    db.add(proyecto)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409,
            "No se pudo guardar: algún valor único (p. ej. API ID Unergy, topic slug, "
            "ID de Solenium o de Sun Factory) ya está en uso por otro proyecto.",
        )
    db.refresh(proyecto)
    sincronizar_datos_unergy_si_aplica(proyecto, db)
    sincronizar_ubicacion_tsf_si_aplica(proyecto, db)
    sincronizar_info_tecnica_solenium_si_aplica(proyecto, db)
    sincronizar_project_id_solarview_si_aplica(proyecto, db)
    return _get_proyecto_or_404(proyecto.id, db)


# ── Proyectos pendientes (Sun Factory + Quoia) ──────────────────────────────
# Deben ir antes de /{id} para no chocar -- aunque acá no aplica porque son
# rutas de 2+ segmentos, se deja el mismo orden por consistencia con el resto.

@router.get("/pendientes", response_model=list[ProyectoPendienteOut])
def listar_proyectos_pendientes(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Candidatos de Sun Factory/Quoia sin reflejar en `proyectos`,
    o ya existentes pero con estado/fase desincronizados. Nunca se escriben
    solos -- ver /pendientes/{clave}/confirmar."""
    return resolver_pendientes(db)


@router.post("/pendientes/{clave}/confirmar", response_model=ProyectoOut, status_code=201)
def confirmar_proyecto_pendiente(
    clave: str,
    body: ProyectoPendienteConfirmar,
    forzar: bool = Query(False, description="true: crear igual aunque exista un proyecto con nombre muy parecido"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    pendientes = resolver_pendientes(db)
    item = next((p for p in pendientes if p["clave"] == clave), None)
    if not item:
        raise HTTPException(404, "Ese candidato ya no aparece como pendiente (puede que ya se haya resuelto).")

    overrides = body.model_dump(exclude_unset=True)
    potencia_ac_kw = overrides.get("potencia_ac_kw", item.get("potencia_ac_kw"))
    capacidad_instalada_kwp = overrides.get("capacidad_instalada_kwp", item.get("capacidad_instalada_kwp"))

    if item["tipo_sugerencia"] == "crear":
        payload = {
            "nombre_comercial": overrides.get("nombre_comercial") or item["nombre_sugerido"],
            "tipo_proyecto": overrides.get("tipo_proyecto") or item.get("tipo_proyecto_sugerido"),
            "estado": item.get("estado_sugerido") or "en_desarrollo",
            "municipio": overrides.get("municipio") or item.get("municipio"),
            "departamento": overrides.get("departamento") or item.get("departamento"),
            "latitud": item.get("latitud"),
            "longitud": item.get("longitud"),
            "fase_construccion": item.get("fase_construccion_sugerida"),
            "origina_code": item.get("origina_code"),
            "codigo_tsf": item.get("codigo_tsf"),
            "sunfactory_project_id": item.get("sunfactory_project_id"),
            "sub_project": item.get("sub_project"),
            "project_id_solenium": item.get("project_id_solenium"),
            "origen": "pendientes",
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        # Mismo chequeo que create_proyecto -- sin esto, dos candidatos "pendientes"
        # distintos (p. ej. Sun Factory listó el mismo proyecto dos veces, como pasó
        # con Monterrubio) se podían confirmar por separado y crear proyectos
        # duplicados sin ningún aviso (caso real: "Astrea 1 (Calipso)", IDs 274/275).
        if not forzar:
            duplicado = _buscar_duplicado_por_nombre(db, payload.get("nombre_comercial"), payload.get("tipo_proyecto"))
            if duplicado:
                raise HTTPException(
                    409,
                    {
                        "mensaje": (
                            f"Ya existe un proyecto con un nombre muy parecido: "
                            f"'{duplicado.nombre_comercial}' (ID {duplicado.id})."
                        ),
                        "duplicado_nombre": True,
                        "candidato_id": duplicado.id,
                        "candidato_nombre": duplicado.nombre_comercial,
                    },
                )

        proyecto = Proyecto(**payload)
        db.add(proyecto)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "No se pudo crear: algún código/ID único ya está en uso por otro proyecto.")
        db.refresh(proyecto)
        proyecto_id = proyecto.id
    else:
        proyecto = db.query(Proyecto).filter(Proyecto.id == item["proyecto_id"]).first()
        if not proyecto:
            raise HTTPException(404, "El proyecto vinculado ya no existe")
        if item.get("estado_sugerido") and proyecto.estado != "en_operacion" and item["estado_sugerido"] == "en_operacion":
            proyecto.estado = "en_operacion"
        if item.get("fase_construccion_sugerida"):
            proyecto.fase_construccion = item["fase_construccion_sugerida"]
        # Backfill de vínculos y ubicación -- solo si el proyecto todavía no los tenía.
        for campo in (
            "origina_code", "codigo_tsf", "sunfactory_project_id", "sub_project",
            "project_id_solenium", "municipio", "departamento", "latitud", "longitud",
        ):
            if getattr(proyecto, campo) is None and item.get(campo) is not None:
                setattr(proyecto, campo, item[campo])
        db.commit()
        proyecto_id = proyecto.id

    sincronizar_datos_unergy_si_aplica(proyecto, db)
    sincronizar_ubicacion_tsf_si_aplica(proyecto, db)
    sincronizar_info_tecnica_solenium_si_aplica(proyecto, db)
    sincronizar_project_id_solarview_si_aplica(proyecto, db)

    if potencia_ac_kw is not None or capacidad_instalada_kwp is not None:
        it = db.query(ProyectoInfoTecnica).filter_by(proyecto_id=proyecto_id).first()
        if not it:
            it = ProyectoInfoTecnica(proyecto_id=proyecto_id)
            db.add(it)
        if it.potencia_ac_kw is None and potencia_ac_kw is not None:
            it.potencia_ac_kw = potencia_ac_kw
        if it.capacidad_instalada_kwp is None and capacidad_instalada_kwp is not None:
            it.capacidad_instalada_kwp = capacidad_instalada_kwp
        # Mismo espejo que upsert_info_tecnica() (fix 2026-08-19):
        # proyectos.potencia_instalada_kwp guarda la potencia AC pese al
        # nombre, no la DC -- sin este espejo, un proyecto creado o
        # vinculado desde acá quedaba con potencia_instalada_kwp en NULL
        # para siempre, a menos que alguien volviera a editar Información
        # técnica a mano después (auditoría de Proyectos 2026-08-27,
        # hallazgo #3: 35 proyectos con este vacío, la mayoría creados por
        # este mismo camino de "confirmar pendiente").
        if proyecto.potencia_instalada_kwp is None and it.potencia_ac_kw is not None:
            proyecto.potencia_instalada_kwp = it.potencia_ac_kw
        db.commit()

    return _get_proyecto_or_404(proyecto_id, db)


@router.post("/pendientes/{clave}/ignorar", status_code=204)
def ignorar_proyecto_pendiente(
    clave: str,
    db: Session = Depends(get_db),
    usuario=Depends(get_current_user),
):
    if db.query(ProyectoPendienteIgnorado).filter(ProyectoPendienteIgnorado.clave == clave).first():
        return
    db.add(ProyectoPendienteIgnorado(clave=clave, ignorado_por_usuario_id=usuario.id))
    db.commit()


# ── Generación mensual promedio ───────────────────────────────────────────────
# El promedio se calcula desde la API de generación de Unergy y se PERSISTE en el
# proyecto, para que las vistas de contratos no dependan de esa API en cada
# consulta. Ver app/services/gen_promedio.py.
#
# Van declaradas ANTES de /{id}: ese path param está tipado `int`, así que si
# fueran después, FastAPI intentaría convertir "gen-promedio" a entero y
# devolvería 422 en vez de resolver la ruta.


@router.post("/gen-promedio/recalcular")
async def recalcular_gen_promedio(
    dias: int = Query(gen_promedio.DIAS_POR_DEFECTO, ge=7, le=365,
                      description="Largo de la ventana móvil, en días corridos hacia atrás"),
    dry_run: bool = Query(True, description="Solo previsualizar sin escribir"),
    force: bool = Query(False, description="Pisar también los promedios cargados a mano"),
    proyecto_id: list[int] | None = Query(None, description="Limitar a estos proyectos"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Recalcula `gen_mensual_promedio_mwh` desde el histórico de generación.

    Idempotente y seguro de repetir. Por defecto **no escribe** (`dry_run=true`)
    y **no pisa** los valores cargados a mano: una planta sin histórico se
    diligencia con `PATCH /proyectos/{id}` y el recálculo la respeta.

    La respuesta trae `sin_datos`, `saltados` y `fallidos` con nombre y motivo:
    esa es la lista de trabajo de lo que hay que cargar a mano. Un proyecto que
    no se pudo calcular tiene que verse, no desaparecer del reporte.

    Tarda: consulta la API de generación planta por planta (de a 8 en paralelo).
    """
    return await gen_promedio.recalcular(
        db, dias=dias, dry_run=dry_run, force=force, proyecto_ids=proyecto_id,
    )


@router.get("/gen-promedio")
def listar_gen_promedio(
    solo_faltantes: bool = Query(False, description="Solo los que no tienen promedio cargado"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """El promedio de cada proyecto, con su origen y su antigüedad.

    Es la vista para saber qué falta por cargar a mano sin tener que disparar un
    recálculo contra la API de generación.
    """
    filas = []
    for p in gen_promedio.proyectos_objetivo(db):
        valor = float(p.gen_mensual_promedio_mwh) if p.gen_mensual_promedio_mwh is not None else None
        if solo_faltantes and valor is not None:
            continue
        filas.append({
            "id": p.id,
            "nombre_comercial": p.nombre_comercial,
            "sub_project": p.sub_project,
            "gen_mensual_promedio_mwh": valor,
            "gen_promedio_origen": p.gen_promedio_origen,
            "gen_promedio_dias": p.gen_promedio_dias,
            "gen_promedio_desde": p.gen_promedio_desde,
            "gen_promedio_hasta": p.gen_promedio_hasta,
            "gen_promedio_actualizado_en": p.gen_promedio_actualizado_en,
            # Sin identificador de monitoreo la API no lo resuelve: carga manual sí o sí.
            "requiere_carga_manual": not p.sub_project,
        })
    return {
        "total": len(filas),
        "con_promedio": sum(1 for f in filas if f["gen_mensual_promedio_mwh"] is not None),
        "items": filas,
    }


# ── Consulta simple: lista liviana + detalle por nombre ───────────────────────
# Superficie de solo lectura para una persona con cuenta que consume la API
# directo (script, Excel, curl, LLM) -- ver docs/API_PROYECTOS.md. El flujo
# previsto es: /lista -> tomar el `id` -> /{id}. /buscar es el atajo para cuando
# ya se sabe el nombre del proyecto.
#
# Ambas van declaradas ANTES de /{id}: ese path param está tipado `int`, así que
# si fueran después, FastAPI intentaría convertir "lista" a entero y devolvería
# 422 en vez de resolver la ruta correcta.


def _clave_nombre(texto: str | None) -> str:
    """Forma comparable de un nombre: sin tildes, en minúsculas, sin caracteres
    no alfanuméricos y con los espacios colapsados.

    Reusa `normalizar` de app/utils/nombre_matching.py (la misma cadena que
    reconcilia nombres contra Quoia/Solenium/GESCON), que convierte los
    caracteres raros en espacios pero no colapsa los espacios internos --
    de ahí el split/join.
    """
    return " ".join(normalizar(texto or "").split())


@router.get("/lista", response_model=ProyectoListaResponse)
def listar_proyectos_simple(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Todos los proyectos vigentes en una sola llamada, con los campos justos
    para identificarlos y quedarse con el `id`.

    Sin paginar y sin filtros a propósito: es el listado de entrada. Para
    filtrar o paginar está `GET /proyectos` (que además trae el objeto completo
    de cada proyecto, y es el que consume el frontend).
    """
    filas = (
        db.query(
            Proyecto.id,
            Proyecto.nombre_comercial,
            Proyecto.estado,
            Proyecto.tipo_proyecto,
            Proyecto.municipio,
            Proyecto.departamento,
            Proyecto.potencia_instalada_kwp,
            Proyecto.sub_project,
            Proyecto.codigo_tsf,
        )
        .filter(Proyecto.deleted_at.is_(None))
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    items = [
        {
            "id": f.id,
            "nombre_comercial": f.nombre_comercial,
            "estado": f.estado,
            "tipo_proyecto": f.tipo_proyecto,
            "municipio": f.municipio,
            "departamento": f.departamento,
            # Numeric -> Decimal; se pasa a float para que el JSON traiga un
            # número y no un string.
            "potencia_instalada_kwp": (
                float(f.potencia_instalada_kwp) if f.potencia_instalada_kwp is not None else None
            ),
            "sub_project": f.sub_project,
            "codigo_tsf": f.codigo_tsf,
        }
        for f in filas
    ]
    return {"total": len(items), "items": items}


def _id_por_nombre(db: Session, nombre: str) -> int:
    """Resuelve un nombre al ID de UN proyecto, o lanza un error accionable.

    Devuelve solo el id -- no la fila cargada -- para que cada quien traiga lo
    que necesita: `/proyectos/buscar` quiere el detalle completo con sus
    eager-loads, pero otros consumidores (ver `/fallas/por-proyecto`) solo
    necesitan identificar la planta y cargar esas 8 relaciones sería puro
    desperdicio en cada llamada.

    El match es exacto sobre el nombre normalizado: tolera mayúsculas, tildes,
    guiones y espacios de más, pero NO es difuso -- un nombre parcial no
    coincide. Es deliberado: quien consume esto es un script o una persona
    encadenando llamadas, y devolverle el proyecto equivocado en silencio es
    peor que devolverle un error. (El matcher permisivo `mejor_candidato` existe
    y se usa para avisar de duplicados al crear, pero acá no.)
    """
    clave = _clave_nombre(nombre)
    if not clave:
        raise HTTPException(422, "El parámetro 'nombre' no puede estar vacío.")

    filas = (
        db.query(Proyecto.id, Proyecto.nombre_comercial)
        .filter(Proyecto.deleted_at.is_(None))
        .all()
    )

    coincidencias = [f for f in filas if _clave_nombre(f.nombre_comercial) == clave]

    if not coincidencias:
        raise HTTPException(
            404,
            f"No existe un proyecto cuyo nombre coincida con '{nombre}'. "
            f"Consultá GET /api/v1/proyectos/lista para ver los nombres disponibles.",
        )

    if len(coincidencias) > 1:
        # `nombre_comercial` no tiene UNIQUE y en producción hay duplicados reales
        # (de ahí POST /proyectos/{ganador}/merge/{perdedor}). detail estructurado,
        # igual que el 409 de create_proyecto, para que quien llama pueda elegir
        # un id y reconsultar el detalle sin parsear un string.
        raise HTTPException(
            409,
            {
                "mensaje": (
                    f"Hay {len(coincidencias)} proyectos cuyo nombre coincide con "
                    f"'{nombre}'. Consultá el detalle por ID."
                ),
                "nombre_ambiguo": True,
                "candidatos": [
                    {"id": f.id, "nombre_comercial": f.nombre_comercial}
                    for f in coincidencias
                ],
            },
        )

    return coincidencias[0].id


def _resolver_por_nombre(db: Session, nombre: str) -> Proyecto:
    """El proyecto completo (con eager-loads) resuelto por nombre."""
    return _get_proyecto_or_404(_id_por_nombre(db, nombre), db)


@router.get("/buscar", response_model=ProyectoOut)
def buscar_proyecto_por_nombre(
    nombre: str = Query(..., min_length=1, description="Nombre del proyecto. Tolera mayúsculas, tildes, guiones y espacios de más, pero debe ser el nombre completo (no parcial)."),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Detalle de un proyecto buscándolo por nombre. Devuelve exactamente lo
    mismo que `GET /proyectos/{id}`.

    404 si ningún nombre coincide; 409 con la lista de candidatos si coincide
    más de un proyecto.
    """
    return _resolver_por_nombre(db, nombre)


@router.get("/{id}", response_model=ProyectoOut)
def get_proyecto(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _get_proyecto_or_404(id, db)


@router.get("/{id}/debug-generacion")
def debug_generacion(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)) -> dict:
    """Diagnóstico de solo lectura: ¿la(s) frontera(s) de generación de este
    proyecto tienen generación REAL hoy en Quoia? Usa el mismo método por nodo
    (no por frt_code -- ya sabemos que ese da 400 para algunos borders) que
    Proyectos pendientes, cacheado 1h. Sirve para verificar si un proyecto
    marcado `en_operacion` de verdad está comercializando energía."""
    fronteras = (
        db.query(Frontera)
        .filter(
            Frontera.proyecto_id == id,
            Frontera.deleted_at.is_(None),
            Frontera.tipo_frontera.in_(["generacion", "generacion_consumo"]),
        )
        .all()
    )
    if not fronteras:
        return {"tiene_frontera": False, "detalle": "Este proyecto no tiene frontera de generación registrada."}

    gaia = GaiaClient()
    if not gaia.enabled:
        raise HTTPException(status_code=502, detail="Credenciales de Quoia no configuradas.")
    try:
        borders = gaia.get_all_borders()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"No se pudo consultar Quoia: {exc}")
    generacion_real = _generacion_real_por_frt(gaia, borders)

    borders_by_code = {}
    for b in borders:
        gen = b.get("frt_generation") or {}
        code = (gen.get("frt_code") or "").strip().lower()
        if code:
            borders_by_code[code] = gen

    resultado = []
    for f in fronteras:
        codigo = (f.codigo_frontera or "").strip().lower()
        info = borders_by_code.get(codigo, {})
        resultado.append({
            "codigo_frontera": f.codigo_frontera,
            "tipo_frontera": f.tipo_frontera,
            "last_report_date": info.get("last_report_date"),
            "generacion_real_hoy": generacion_real.get(codigo, False),
        })
    return {"tiene_frontera": True, "fronteras": resultado}


# Columnas con restricción UNIQUE en el modelo Proyecto. Si se intenta asignar a un
# proyecto un valor ya usado por otro, Postgres lanza IntegrityError; sin manejo, eso
# sube como 500 sin detalle y el frontend solo muestra "Error" (ver bug API ID Unergy).
_UNIQUE_COLS = {
    "sub_project": "API ID Unergy",
    "project_id_solenium": "ID de Solenium (generación)",
    "sunfactory_project_id": "ID de Sun Factory",
}


def _verificar_unicos(db: Session, payload: dict, excluir_id: int | None = None) -> None:
    """Chequeo proactivo de columnas UNIQUE: da un mensaje accionable que nombra el
    proyecto en conflicto, en vez de un IntegrityError opaco (usado en create y update)."""
    for col, etiqueta in _UNIQUE_COLS.items():
        nuevo = payload.get(col)
        if nuevo in (None, ""):
            continue
        query = db.query(Proyecto).filter(getattr(Proyecto, col) == nuevo)
        if excluir_id is not None:
            query = query.filter(Proyecto.id != excluir_id)
        conflicto = query.first()
        if conflicto:
            raise HTTPException(
                409,
                f"El {etiqueta} '{nuevo}' ya está asignado al proyecto "
                f"'{conflicto.nombre_comercial}' (ID {conflicto.id}). "
                f"Cada {etiqueta} debe ser único.",
            )


@router.patch("/{id}", response_model=ProyectoOut)
def update_proyecto(id: int, data: ProyectoUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(Proyecto).filter(Proyecto.id == id).first()
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")

    payload = data.model_dump(exclude_unset=True)
    _verificar_unicos(db, payload, excluir_id=id)

    # Si el usuario edita la fecha de inicio de comercialización a mano, marca el
    # flag para que el backfill/job diario no la vuelva a pisar (salvo que él mismo
    # mande el flag explícito en el payload).
    if "fecha_inicio_comercializacion" in payload and "fecha_comercializacion_editada_manual" not in payload:
        p.fecha_comercializacion_editada_manual = True

    # Mismo criterio para el promedio de generación: si alguien lo escribe a mano
    # queda marcado 'manual' y el recálculo no lo pisa. Es el caso de las plantas
    # sin histórico, que es justamente para lo que existe la carga manual.
    if "gen_mensual_promedio_mwh" in payload and "gen_promedio_origen" not in payload:
        from datetime import datetime as _dt, timezone as _tz
        p.gen_promedio_origen = gen_promedio.ORIGEN_MANUAL
        p.gen_promedio_actualizado_en = _dt.now(_tz.utc)
        p.gen_promedio_dias = None
        p.gen_promedio_desde = None
        p.gen_promedio_hasta = None

    for k, v in payload.items():
        setattr(p, k, v)

    try:
        db.commit()
    except IntegrityError:
        # Backstop ante carreras o cualquier otra restricción única no cubierta arriba.
        db.rollback()
        raise HTTPException(
            409,
            "No se pudo guardar: algún valor único (p. ej. API ID Unergy o topic slug) "
            "ya está en uso por otro proyecto.",
        )
    if "operador_red_id" in payload:
        sincronizar_operador_red(db, p)
        db.commit()
    return _get_proyecto_or_404(id, db)


@router.post("/{id}/vincular-sunfactory/{sunfactory_project_id}", response_model=ProyectoOut)
def vincular_sunfactory(
    id: int,
    sunfactory_project_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Confirma que un proyecto ya existente (creado a mano, o por una corrida
    vieja del sync sin este campo) corresponde a este proyecto de Sun Factory.

    Se usa para resolver las `sugerencias_vinculo` que devuelve `sync_tsf_projects`
    cuando no encuentra un match exacto por id/código. Una vez confirmado, el
    sync ya reconoce este proyecto por su id estable de Sun Factory y no lo
    vuelve a duplicar, aunque le cambien el nombre después."""
    p = db.query(Proyecto).filter(Proyecto.id == id, Proyecto.deleted_at.is_(None)).first()
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")
    _verificar_unicos(db, {"sunfactory_project_id": sunfactory_project_id}, excluir_id=id)
    p.sunfactory_project_id = sunfactory_project_id
    db.commit()
    return _get_proyecto_or_404(id, db)


# Tablas hacia proyectos que NO tienen relationship en el modelo Proyecto (a
# diferencia de fallas/mantenimientos/etc, que sí lo tienen y ya se chequean
# abajo vía el ORM) -- hallazgo de la auditoría de Proyectos 2026-08-27: el
# guard de delete_proyecto() solo cubría 11 relaciones. Las primeras 6 tienen
# FK ON DELETE CASCADE, así que Postgres las borraba en cascada sin ningún
# aviso (ej. historial de generación diaria, paneles contables ya calculados,
# registros de conexión CND). Todas usan proyecto_id -- ojo con arr_documento,
# que tiene DOS columnas de proyecto: arr_proyecto_id (FK a arr_proyectos, el
# módulo de Arriendos, sin relación con esto) y proyecto_id (FK real a
# proyectos.id, nullable, la que importa acá). No incluye cumplimiento_mensual
# (ON DELETE SET NULL, no CASCADE -- huérfana, no se pierde el dato) ni
# generacion_diaria (SÍ tiene relationship: Proyecto.generaciones, chequeada
# abajo con el resto).
#
# proyecto_informe_om (agregada 2026-08-27, revisión de esa tabla; fusionó
# con proyecto_inicio_operacion el 2026-08-31): FK en NO ACTION, no CASCADE
# -- el riesgo no es perder el dato en silencio, sino un IntegrityError sin
# capturar (500 crudo) al borrar un proyecto que ya tiene ficha de Puesta en
# Marcha.
_TABLAS_CASCADE_SIN_RELATIONSHIP = [
    ("panel_contable", "proyecto_id"),
    ("panel_consecutivo", "proyecto_id"),
    ("clasificacion_energia_mensual", "proyecto_id"),
    ("clasificacion_liquidacion", "proyecto_id"),
    ("registro_conexion", "proyecto_id"),
    ("arr_documento", "proyecto_id"),
    ("proyecto_informe_om", "proyecto_id"),
]


def _tabla_cascade_bloqueante(db: Session, proyecto_id: int) -> str | None:
    """Nombre de la primera tabla (de _TABLAS_CASCADE_SIN_RELATIONSHIP) que
    sí tiene filas para este proyecto -- None si ninguna."""
    for tabla, columna in _TABLAS_CASCADE_SIN_RELATIONSHIP:
        existe = db.execute(
            text(f"SELECT 1 FROM {tabla} WHERE {columna} = :pid LIMIT 1"), {"pid": proyecto_id}
        ).first()
        if existe:
            return tabla
    return None


@router.delete("/{id}", status_code=204)
def delete_proyecto(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(Proyecto).filter(Proyecto.id == id).first()
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")

    # Verificar si hay registros de negocio que impiden la eliminación
    business_records = (
        p.fallas or p.mantenimientos or p.liquidaciones
        or p.asic_solicitudes or p.rec_procesos or p.promotor_seguimientos
        or p.contratos_servicio or p.ppa_contratos
        or p.fronteras or p.generaciones
    )
    tabla_bloqueante = None if business_records else _tabla_cascade_bloqueante(db, id)
    if business_records or tabla_bloqueante:
        raise HTTPException(
            409,
            "No se puede eliminar el proyecto porque tiene registros operativos asociados "
            "(fallas, mantenimientos, liquidaciones, contratos, etc.). "
            "Elimine primero esos registros."
        )

    # Eliminar sub-recursos directos del proyecto
    db.query(ProyectoInversionista).filter_by(proyecto_id=id).delete()
    db.query(ProyectoInversor).filter_by(proyecto_id=id).delete()
    db.query(ProyectoAreaContacto).filter_by(proyecto_id=id).delete()
    db.query(ProyectoInfoTecnica).filter_by(proyecto_id=id).delete()

    db.delete(p)
    db.commit()


# ── Fusión de proyectos duplicados ────────────────────────────────────────────
# Mueve TODOS los registros hijos del proyecto "perdedor" al "ganador" y borra el
# perdedor, sin violar constraints únicos. Las tres categorías reflejan el esquema:
#   _MERGE_SIMPLE       : 1-a-muchos sin constraint -> repunte directo de proyecto_id.
#   _MERGE_COMPOSITE    : 1-a-muchos / M-M con UNIQUE compuesto -> si el ganador ya
#                         tiene esa clave, se descarta la fila del perdedor (política
#                         "conservar la del ganador"); el resto se repunta.
#   _MERGE_ONE_TO_ONE   : 1-a-1 (proyecto_id UNIQUE) -> si el ganador ya tiene fila,
#                         se descarta la del perdedor; si no, se mueve.
# Campos escalares únicos del propio proyecto (sub_project=API ID Unergy,
# project_id_solenium) se copian del perdedor al ganador solo si el ganador los tiene
# vacíos (liberándolos primero del perdedor para no chocar con el UNIQUE).
_MERGE_SIMPLE = [
    "proyecto_inversores",
    "proyecto_inversionistas", "fronteras", "fallas", "mantenimientos",
    "contratos_servicio", "asic_solicitudes",
    "rec_procesos", "costos_variables",
    "gestion_registros", "cumplimiento_mensual",
]
_MERGE_COMPOSITE = [
    ("generacion_diaria", ["fecha"]),
    ("liquidaciones", ["periodo"]),
    ("promotor_seguimientos", ["requisito_id"]),
    ("panel_contable", ["periodo", "tipo"]),
    ("clasificacion_liquidacion", ["periodo"]),
    ("mapeo_celda_concepto", ["concepto"]),
    ("alias_fuente_ingreso", ["columna_origen"]),
    ("ppa_contrato_proyectos", ["contrato_id"]),
    # proyecto_area_contacto tiene UNIQUE (proyecto_id, tipo) -- si el ganador
    # ya tiene un puntero para ese tipo, se descarta el del perdedor.
    ("proyecto_area_contacto", ["tipo"]),
]
_MERGE_ONE_TO_ONE = [
    "proyecto_info_tecnica", "proyecto_informe_om",
]
_MERGE_SCALAR_UNIQUE = ["sub_project", "project_id_solenium", "sunfactory_project_id"]
# Campos no-unicos que, si el ganador los tiene vacios, se rellenan con el
# valor del perdedor (a diferencia de _MERGE_SCALAR_UNIQUE, no hace falta
# liberarlos en el perdedor antes de copiar: no hay constraint que choque).
_MERGE_SCALAR_FILL_IF_EMPTY = ["municipio", "departamento", "latitud", "longitud", "codigo_tsf"]


def _scalar(db, sql, params):
    return db.execute(text(sql), params).scalar()


@router.post("/{ganador_id}/merge/{perdedor_id}")
def merge_proyectos(
    ganador_id: int,
    perdedor_id: int,
    dry_run: bool = Query(True, description="true (default): solo reporta, no modifica nada."),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Fusiona el proyecto `perdedor_id` dentro de `ganador_id`.

    Con `dry_run=true` (por defecto) solo devuelve un reporte de lo que pasaría.
    Con `dry_run=false` ejecuta la fusión completa en una sola transacción y borra
    el perdedor. Política de colisión: se conserva la fila del ganador.
    """
    if ganador_id == perdedor_id:
        raise HTTPException(400, "El ganador y el perdedor no pueden ser el mismo proyecto.")
    ganador = db.query(Proyecto).filter(Proyecto.id == ganador_id).first()
    perdedor = db.query(Proyecto).filter(Proyecto.id == perdedor_id).first()
    if not ganador:
        raise HTTPException(404, f"Proyecto ganador {ganador_id} no encontrado.")
    if not perdedor:
        raise HTTPException(404, f"Proyecto perdedor {perdedor_id} no encontrado.")

    p = {"keeper": ganador_id, "loser": perdedor_id}
    movimientos = []  # filas por tabla: {tabla, a_mover, descartadas_por_colision}

    # ── Conteo (dry-run y reporte) ──
    for t in _MERGE_SIMPLE:
        n = _scalar(db, f"SELECT count(*) FROM {t} WHERE proyecto_id=:loser", p)
        if n:
            movimientos.append({"tabla": t, "a_mover": n, "descartadas_por_colision": 0})

    for t, keys in _MERGE_COMPOSITE:
        n = _scalar(db, f"SELECT count(*) FROM {t} WHERE proyecto_id=:loser", p)
        if not n:
            continue
        cond = " AND ".join(f"k.{c} = {t}.{c}" for c in keys)
        coli = _scalar(
            db,
            f"SELECT count(*) FROM {t} WHERE proyecto_id=:loser AND EXISTS "
            f"(SELECT 1 FROM {t} k WHERE k.proyecto_id=:keeper AND {cond})",
            p,
        )
        movimientos.append({"tabla": t, "a_mover": n - coli, "descartadas_por_colision": coli})

    for t in _MERGE_ONE_TO_ONE:
        n_loser = _scalar(db, f"SELECT count(*) FROM {t} WHERE proyecto_id=:loser", p)
        if not n_loser:
            continue
        n_keeper = _scalar(db, f"SELECT count(*) FROM {t} WHERE proyecto_id=:keeper", p)
        coli = n_loser if n_keeper else 0
        movimientos.append({"tabla": t, "a_mover": n_loser - coli, "descartadas_por_colision": coli})

    # asic_cambios_contratos (doble FK, sin unique)
    asic_orig = _scalar(db, "SELECT count(*) FROM asic_cambios_contratos WHERE proyecto_original_id=:loser", p)
    asic_nuevo = _scalar(db, "SELECT count(*) FROM asic_cambios_contratos WHERE proyecto_nuevo_id=:loser", p)
    if asic_orig or asic_nuevo:
        movimientos.append({"tabla": "asic_cambios_contratos", "a_mover": asic_orig + asic_nuevo, "descartadas_por_colision": 0})

    # Campos escalares vacíos en el ganador: qué se copiaría del perdedor
    campos_copiados = []
    for f in _MERGE_SCALAR_UNIQUE + _MERGE_SCALAR_FILL_IF_EMPTY:
        val_keeper = getattr(ganador, f, None)
        val_loser = getattr(perdedor, f, None)
        if (val_keeper in (None, "")) and (val_loser not in (None, "")):
            campos_copiados.append({"campo": f, "valor": val_loser})

    reporte = {
        "dry_run": dry_run,
        "ganador": {"id": ganador.id, "nombre": ganador.nombre_comercial},
        "perdedor": {"id": perdedor.id, "nombre": perdedor.nombre_comercial},
        "movimientos": movimientos,
        "campos_copiados_al_ganador": campos_copiados,
        "total_filas_a_mover": sum(m["a_mover"] for m in movimientos),
        "total_filas_descartadas": sum(m["descartadas_por_colision"] for m in movimientos),
    }

    if dry_run:
        return reporte

    # ── Ejecución real (transacción única) ──
    try:
        # 0) Retrato del perdedor ANTES de tocarlo. Va primero a propósito: el
        #    paso 5 le vacía los campos únicos, así que más abajo la foto ya
        #    saldría mutilada. Es el único rastro que queda de la planta.
        registrar_borrado(db, "proyectos", perdedor.id, tipo="hard", contexto={
            "operacion": "merge_proyectos",
            "ganador_id": ganador.id,
            "ganador_nombre": ganador.nombre_comercial,
            "movimientos": movimientos,
            "campos_copiados_al_ganador": campos_copiados,
            "total_filas_movidas": sum(m["a_mover"] for m in movimientos),
        })

        # 1) Doble FK ASIC
        db.execute(text("UPDATE asic_cambios_contratos SET proyecto_original_id=:keeper WHERE proyecto_original_id=:loser"), p)
        db.execute(text("UPDATE asic_cambios_contratos SET proyecto_nuevo_id=:keeper WHERE proyecto_nuevo_id=:loser"), p)

        # 2) Tablas con unique compuesto: descartar colisiones, repuntar el resto
        for t, keys in _MERGE_COMPOSITE:
            cond = " AND ".join(f"k.{c} = {t}.{c}" for c in keys)
            db.execute(text(
                f"DELETE FROM {t} WHERE proyecto_id=:loser AND EXISTS "
                f"(SELECT 1 FROM {t} k WHERE k.proyecto_id=:keeper AND {cond})"), p)
            db.execute(text(f"UPDATE {t} SET proyecto_id=:keeper WHERE proyecto_id=:loser"), p)

        # 3) Tablas 1-a-1: si el ganador ya tiene, descartar la del perdedor; mover el resto
        for t in _MERGE_ONE_TO_ONE:
            db.execute(text(
                f"DELETE FROM {t} WHERE proyecto_id=:loser AND EXISTS "
                f"(SELECT 1 FROM {t} k WHERE k.proyecto_id=:keeper)"), p)
            db.execute(text(f"UPDATE {t} SET proyecto_id=:keeper WHERE proyecto_id=:loser"), p)

        # 4) Tablas simples
        for t in _MERGE_SIMPLE:
            db.execute(text(f"UPDATE {t} SET proyecto_id=:keeper WHERE proyecto_id=:loser"), p)

        # 5) Campos escalares únicos: liberar del perdedor y copiar al ganador si está vacío
        for f in _MERGE_SCALAR_UNIQUE:
            db.execute(text(f"UPDATE proyectos SET {f}=NULL WHERE id=:loser"), p)
        for c in campos_copiados:
            db.execute(
                text(f"UPDATE proyectos SET {c['campo']}=:val WHERE id=:keeper"),
                {**p, "val": c["valor"]},
            )

        # 6) Borrar el perdedor
        db.execute(text("DELETE FROM proyectos WHERE id=:loser"), p)

        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"La fusión falló y se revirtió por completo: {type(e).__name__}: {e}")

    reporte["ejecutado"] = True
    return reporte


@router.patch("/{id}/servicios", response_model=ProyectoOut)
def toggle_servicios(id: int, data: dict, db: Session = Depends(get_db), _=Depends(get_current_user)):
    p = db.query(Proyecto).filter(Proyecto.id == id).first()
    if not p:
        raise HTTPException(404, "Proyecto no encontrado")
    allowed = {"srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa", "srv_promotor", "srv_rec"}
    for k, v in data.items():
        if k in allowed:
            setattr(p, k, v)

    db.commit()
    return _get_proyecto_or_404(id, db)


# ── Info Técnica ──────────────────────────────────────────────────────────────

@router.get("/{id}/info-tecnica", response_model=ProyectoInfoTecnicaOut)
def get_info_tecnica(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    it = db.query(ProyectoInfoTecnica).filter_by(proyecto_id=id).first()
    if not it:
        raise HTTPException(404, "Info técnica no encontrada")
    return it


@router.put("/{id}/info-tecnica", response_model=ProyectoInfoTecnicaOut)
def upsert_info_tecnica(id: int, data: ProyectoInfoTecnicaCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    proyecto = _get_proyecto_or_404(id, db)
    it = db.query(ProyectoInfoTecnica).filter_by(proyecto_id=id).first()
    if it:
        for k, v in data.model_dump(exclude_unset=True).items():
            setattr(it, k, v)
    else:
        it = ProyectoInfoTecnica(proyecto_id=id, **data.model_dump())
        db.add(it)
    # Fix 2026-08-19: pese al nombre, proyectos.potencia_instalada_kwp guarda
    # históricamente la potencia AC (coincide con potencia_ac_kw en 56 de 66
    # proyectos verificados), NO la capacidad DC -- confirmado con el usuario.
    # Antes este bloque espejaba capacidad_instalada_kwp (DC) por error, lo
    # que corrompió el campo en los proyectos editados desde que existía ese
    # bug (ver Cedillanos, IML Empaques, Chiriguana N1, Agustín 2/3, Elektra,
    # Astrea 1/2/3). El espejo correcto es desde potencia_ac_kw.
    if it.potencia_ac_kw is not None:
        proyecto.potencia_instalada_kwp = it.potencia_ac_kw
    db.commit()
    db.refresh(it)
    return it


# ── Inversores ────────────────────────────────────────────────────────────────

def _validar_suma_inversores(id: int, db: Session, nuevo_kw, excluir_id: int | None = None) -> None:
    """La suma de potencias nominales de los inversores ACTIVOS no puede
    superar la potencia AC nominal del proyecto
    (proyecto_info_tecnica.potencia_ac_kw). Si no hay potencia AC
    configurada, no se valida (no se puede comparar).

    Un inversor retirado (activo=False) ya no aporta capacidad real -- antes
    contaba igual, así que reemplazar un inversor retirado por uno nuevo
    podía rechazarse por una suma que ya no existía físicamente (auditoría
    de Proyectos 2026-08-27)."""
    if nuevo_kw is None:
        return
    # SQL crudo: robusto aunque el resto de columnas de info_tecnica difieran por entorno.
    row = db.execute(
        text("SELECT potencia_ac_kw FROM proyecto_info_tecnica WHERE proyecto_id = :p"),
        {"p": id},
    ).fetchone()
    ac = float(row[0]) if row and row[0] is not None else None
    if ac is None or ac <= 0:
        return
    q = db.query(ProyectoInversor).filter_by(proyecto_id=id, activo=True)
    if excluir_id is not None:
        q = q.filter(ProyectoInversor.id != excluir_id)
    suma = sum(float(i.potencia_nominal_kw or 0) for i in q.all()) + float(nuevo_kw)
    # tolerancia pequeña por redondeos
    if suma > ac + 0.001:
        raise HTTPException(
            400,
            f"La suma de potencias de inversores ({suma:.1f} kW) supera la potencia AC "
            f"nominal del proyecto ({ac:.1f} kW).",
        )


@router.get("/{id}/inversores", response_model=list[ProyectoInversorOut])
def list_inversores(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Único consumidor: el selector de "qué inversor falló" al reportar una
    falla (FallaForm.vue / FallaCreateSheet.vue) -- por eso solo trae los
    activos. Un inversor retirado no debería poder recibir una falla nueva
    (antes sí aparecía, sin ningún filtro; auditoría de Proyectos 2026-08-27)."""
    _get_proyecto_or_404(id, db)
    return (
        db.query(ProyectoInversor)
        .filter_by(proyecto_id=id, activo=True)
        .order_by(ProyectoInversor.orden, ProyectoInversor.id)
        .all()
    )


@router.post("/{id}/inversores", response_model=ProyectoInversorOut, status_code=201)
def add_inversor(id: int, data: ProyectoInversorCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    _validar_suma_inversores(id, db, data.potencia_nominal_kw)
    inv = ProyectoInversor(proyecto_id=id, **data.model_dump(exclude_none=True))
    db.add(inv)
    try:
        db.commit()
    except IntegrityError:
        # Doble clic o dos requests casi simultáneas (p. ej. el prefill de
        # inversores típicos en FallaForm.vue/FallaCreateSheet.vue): sin esto,
        # sube como 500 crudo de Postgres en vez de un mensaje accionable.
        db.rollback()
        raise HTTPException(409, f"Ya existe un inversor llamado \"{inv.nombre}\" en este proyecto.")
    db.refresh(inv)
    return inv


@router.patch("/{id}/inversores/{inv_id}", response_model=ProyectoInversorOut)
def update_inversor(id: int, inv_id: int, data: ProyectoInversorUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    inv = db.query(ProyectoInversor).filter_by(id=inv_id, proyecto_id=id).first()
    if not inv:
        raise HTTPException(404, "Inversor no encontrado")
    cambios = data.model_dump(exclude_unset=True)
    if "potencia_nominal_kw" in cambios:
        _validar_suma_inversores(id, db, cambios["potencia_nominal_kw"], excluir_id=inv_id)
    for k, v in cambios.items():
        setattr(inv, k, v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Ya existe un inversor llamado \"{inv.nombre}\" en este proyecto.")
    db.refresh(inv)
    return inv


@router.delete("/{id}/inversores/{inv_id}", status_code=204)
def delete_inversor(id: int, inv_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    inv = db.query(ProyectoInversor).filter_by(id=inv_id, proyecto_id=id).first()
    if not inv:
        raise HTTPException(404, "Inversor no encontrado")
    db.delete(inv)
    db.commit()


# ── Puntero de contactos por área ──────────────────────────────────────────────
# Para cada tipo (operacional/cgm/liquidacion), este proyecto
# puede apuntar a un Cliente específico -- ver app/services/contactos.py.
# Sin fila para un tipo = usa los contactos de los inversionistas vigentes.

@router.get("/{id}/area-contactos", response_model=list[ProyectoAreaContactoOut])
def list_area_contactos(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    rows = (
        db.query(ProyectoAreaContacto, Cliente.razon_social_nombre)
        .join(Cliente, Cliente.id == ProyectoAreaContacto.cliente_id)
        .filter(ProyectoAreaContacto.proyecto_id == id)
        .all()
    )
    out = []
    for area, nombre in rows:
        area.cliente_nombre = nombre
        out.append(area)
    return out


@router.put("/{id}/area-contactos/{tipo}", response_model=ProyectoAreaContactoOut)
def set_area_contacto(id: int, tipo: str, data: ProyectoAreaContactoSet, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    if not db.query(Cliente).filter(Cliente.id == data.cliente_id).first():
        raise HTTPException(404, "Cliente no encontrado")
    area = db.query(ProyectoAreaContacto).filter_by(proyecto_id=id, tipo=tipo).first()
    if area:
        area.cliente_id = data.cliente_id
    else:
        area = ProyectoAreaContacto(proyecto_id=id, tipo=tipo, cliente_id=data.cliente_id)
        db.add(area)
    db.commit()
    db.refresh(area)
    area.cliente_nombre = db.query(Cliente.razon_social_nombre).filter(Cliente.id == data.cliente_id).scalar()
    return area


@router.delete("/{id}/area-contactos/{tipo}", status_code=204)
def clear_area_contacto(id: int, tipo: str, db: Session = Depends(get_db), _=Depends(get_current_user)):
    area = db.query(ProyectoAreaContacto).filter_by(proyecto_id=id, tipo=tipo).first()
    if not area:
        raise HTTPException(404, "Este proyecto no tiene un puntero para ese tipo")
    db.delete(area)
    db.commit()


# ── Inversionistas ────────────────────────────────────────────────────────────

@router.get("/{id}/inversionistas", response_model=list[ProyectoInversionistaOut])
def list_inversionistas(id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    return (
        db.query(ProyectoInversionista)
        .options(selectinload(ProyectoInversionista.cliente))
        .filter(ProyectoInversionista.proyecto_id == id)
        .all()
    )


@router.post("/{id}/inversionistas", response_model=ProyectoInversionistaOut, status_code=201)
def add_inversionista(id: int, data: ProyectoInversionistaCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    _get_proyecto_or_404(id, db)
    duplicate = db.query(ProyectoInversionista).filter_by(
        proyecto_id=id, cliente_id=data.cliente_id
    ).first()
    if duplicate:
        raise HTTPException(409, "Este cliente ya es inversionista de este proyecto")
    inv = ProyectoInversionista(proyecto_id=id, **data.model_dump())
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return db.query(ProyectoInversionista).options(
        selectinload(ProyectoInversionista.cliente)
    ).filter(ProyectoInversionista.id == inv.id).first()


@router.patch("/{id}/inversionistas/{inv_id}", response_model=ProyectoInversionistaOut)
def update_inversionista(id: int, inv_id: int, data: ProyectoInversionistaUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    inv = db.query(ProyectoInversionista).filter_by(id=inv_id, proyecto_id=id).first()
    if not inv:
        raise HTTPException(404, "Inversionista no encontrado")
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(inv, k, v)
    db.commit()
    return db.query(ProyectoInversionista).options(
        selectinload(ProyectoInversionista.cliente)
    ).filter(ProyectoInversionista.id == inv_id).first()


@router.delete("/{id}/inversionistas/{inv_id}", status_code=204)
def remove_inversionista(id: int, inv_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    inv = db.query(ProyectoInversionista).filter_by(id=inv_id, proyecto_id=id).first()
    if not inv:
        raise HTTPException(404, "Inversionista no encontrado")
    db.delete(inv)
    db.commit()
