"""
Puente de generación en vivo (_legacy) + resumen de flota para el módulo de
Fallas del frontend. El portal externo de clientes (login por correo +
reporte de fallas vanilla-JS) que vivía aquí se retiro -- ver
static/monitoreo/ (eliminado) y admin/sync-proyectos (utilidad aparte, sin
relacion con el portal).
"""
import calendar
import json
import logging
from datetime import datetime, date, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from sqlalchemy.orm import Session, selectinload
from app.core.config import settings
from app.core.database import get_db
from app.api.v1.auth import get_current_user
from app.models import ContratoServicio
from app.models.usuarios import Usuario
from app.models.proyectos import Proyecto

logger = logging.getLogger("monitoreo")
router = APIRouter(prefix="/monitoreo", tags=["Monitoreo"])



# ── Legacy bridge — replaces Google Apps Script ───────────────────────────────

_COL_TZ = timezone(timedelta(hours=-5))

# ── Unergy API helpers ────────────────────────────────────────────────────────

_token_cache: dict = {"token": "", "expires_at": 0.0}

async def _unergy_token() -> str:
    import time as _time
    now = _time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    auth_url = f"{settings.UNERGY_API_URL}/api/accounts/{settings.UNERGY_ACCOUNT_ID}/"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(auth_url, json={"login": settings.UNERGY_LOGIN, "password": settings.UNERGY_PASSWORD})
        r.raise_for_status()
        data = r.json()
        tok = data.get("token") or data.get("access") or data.get("key") or ""
        if tok:
            _token_cache["token"] = tok
            _token_cache["expires_at"] = now + 300  # reuse for 5 minutes
        return tok


async def _fetch_unergy_raw(token: str, sub_project: str, from_iso: str, to_iso: str, verified_only: bool) -> list:
    params: dict = {
        "time_stamp__gte": from_iso,
        "time_stamp__lte": to_iso,
        "sub_project": sub_project,
        "limit": "10000",
    }
    if verified_only:
        params["verified_by_operator"] = "True"
    data_url = f"{settings.UNERGY_API_URL}/api/admin/operations/project_generation/"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(data_url, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            return []
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else body.get("results", [])


def _compute_deltas(readings: list, d_from_dt: datetime, d_to_dt: datetime) -> list:
    readings.sort(key=lambda x: x.get("time_stamp") or x.get("timestamp") or "")
    before, period = [], []
    for r in readings:
        ts_raw = r.get("time_stamp") or r.get("timestamp") or ""
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")) if "T" in ts_raw else datetime.strptime(ts_raw[:16], "%Y-%m-%d %H:%M").replace(tzinfo=_COL_TZ)
        except Exception:
            continue
        if ts < d_from_dt:
            before.append((ts, r))
        elif ts <= d_to_dt:
            period.append((ts, r))

    if not period:
        return []

    working = ([before[-1]] if before else []) + period
    result = []
    for i in range(1, len(working)):
        ts_prev, r_prev = working[i - 1]
        ts_curr, r_curr = working[i]
        gen_curr = float(r_curr.get("generacion") or r_curr.get("generation") or 0)
        gen_prev = float(r_prev.get("generacion") or r_prev.get("generation") or 0)
        delta = max(0.0, gen_curr - gen_prev)
        ts_local = ts_curr.astimezone(_COL_TZ)
        result.append({
            "time": ts_local.strftime("%Y-%m-%d %H:%M"),
            "date": ts_local.strftime("%Y-%m-%d"),
            "kwh": round(delta, 3),
        })
    return result


async def _action_get_generation(sub_project: str | None, date_from: str | None, date_to: str | None, db: Session) -> dict:
    if not sub_project:
        return {"ok": False, "error": "sub_project requerido"}

    try:
        d_from_date = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else date.today().replace(day=1)
        d_to_date = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else date.today()
    except Exception:
        return {"ok": False, "error": "Formato de fecha inválido (YYYY-MM-DD)"}

    d_from_dt = datetime(d_from_date.year, d_from_date.month, d_from_date.day, 0, 0, 0, tzinfo=_COL_TZ)
    d_to_dt = datetime(d_to_date.year, d_to_date.month, d_to_date.day, 23, 59, 59, tzinfo=_COL_TZ)
    fetch_from_dt = d_from_dt - timedelta(days=2)
    from_iso = fetch_from_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = d_to_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        token = await _unergy_token()
        readings = await _fetch_unergy_raw(token, sub_project, from_iso, to_iso, verified_only=True)
        if not readings:
            readings = await _fetch_unergy_raw(token, sub_project, from_iso, to_iso, verified_only=False)
    except Exception as e:
        return {"ok": False, "error": f"Error API Unergy: {e}"}

    filtered = _compute_deltas(readings, d_from_dt, d_to_dt)

    simulation = None
    from sqlalchemy import or_
    import json as _json

    def _parse_kwh_list(val):
        """Normaliza JSONB o string JSON a list[float|None]. Maneja datos históricos."""
        if val is None:
            return None
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                result = _json.loads(val)
                return result if isinstance(result, list) else None
            except Exception:
                return None
        return None

    proyecto = db.query(Proyecto).filter(Proyecto.sub_project == sub_project).first()
    if proyecto and (proyecto.p90_mensual_kwh or proyecto.p50_mensual_kwh):
        try:
            month = d_from_date.month
            p90_list = _parse_kwh_list(proyecto.p90_mensual_kwh) or [None] * 12
            p50_list = _parse_kwh_list(proyecto.p50_mensual_kwh) or [None] * 12
            p99_list = _parse_kwh_list(getattr(proyecto, "p99_mensual_kwh", None)) or [None] * 12
            p90m = p90_list[month - 1] if len(p90_list) >= month else None
            p50m = p50_list[month - 1] if len(p50_list) >= month else None
            p99m = p99_list[month - 1] if len(p99_list) >= month else None
            days_in_month = calendar.monthrange(d_from_date.year, month)[1]
            simulation = {
                "p90_monthly": p90m,
                "p50_monthly": p50m,
                "p99_monthly": p99m,
                "p90_daily": round(p90m / days_in_month, 1) if p90m else None,
            }
        except Exception:
            # No silenciar: si el P50/P90 está corrupto, el dashboard muestra
            # generación real sin línea base y sin señal del fallo. Logear.
            logger.exception("Fallo al parsear simulación P90/P50 proyecto_id=%s", getattr(proyecto, "id", "?"))

    return {"ok": True, "data": filtered, "simulation": simulation}


def _action_get_projects(db: Session) -> dict:
    proyectos = (
        db.query(Proyecto)
        .filter(
            Proyecto.sub_project.isnot(None),
            Proyecto.estado == "en_operacion",
        )
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    return {
        "ok": True,
        "projects": [
            {
                "sub_project": p.sub_project,
                "nombre_comercial": p.nombre_comercial,
                "municipio": p.municipio or "—",
                "departamento": p.departamento or "—",
                "potencia_instalada_kwp": float(p.potencia_instalada_kwp) if p.potencia_instalada_kwp else None,
                "estado": p.estado,
                "project_id_solenium": p.project_id_solenium or "",
            }
            for p in proyectos
        ],
    }


def _action_get_portfolios(db: Session) -> dict:
    """Agrupamiento por PORTAFOLIO (capa de proyectos, vía proyectos.portafolio_id).
    Fuente de verdad gestionada en la vista de Gestión de portafolios. Se siembra una
    vez desde el agrupamiento por cliente/inversionista para no perder lo existente."""
    try:
        from app.api.v1.portafolios import get_portfolios_grouping
        return {"ok": True, "portfolios": get_portfolios_grouping(db)}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(exc), "portfolios": {}}


def _action_get_all_contratos(db: Session) -> dict:
    rows = (
        db.query(ContratoServicio)
        .filter(
            # "operacion" nunca se usa como servicio_aplica: el contrato O&M
            # real es "mantenimiento" (ver ServiciosUnificadoView.vue en el
            # frontend, mismo hallazgo). Con "operacion" este filtro devolvia
            # 0 filas siempre.
            ContratoServicio.servicio_aplica == "mantenimiento",
            ContratoServicio.estado.in_(["vigente", "en_renovacion"]),
        )
        .options(selectinload(ContratoServicio.proyecto))
        .all()
    )
    contratos = []
    for cs in rows:
        p = cs.proyecto
        slug = p.sub_project if p else None
        if not slug:
            continue
        contratos.append({
            "sub_project": slug,
            "nombre_comercial": p.nombre_comercial,
            "disponibilidad_garantizada_pct": "97",
            "contratista": cs.prestador_nombre or "Unergy S.A.S.",
            "valor_estimado_ano1_cop": str(round(float(cs.tarifa_base) * 12)) if cs.tarifa_base else "0",
            "garantias_equipos": "",
            "numero_contrato": cs.numero_contrato or "",
            "fecha_inicio": cs.fecha_inicio.isoformat() if cs.fecha_inicio else "",
            "fecha_fin": cs.fecha_fin.isoformat() if cs.fecha_fin else "",
            "project_id_solenium": p.project_id_solenium or "",
        })
    return {"ok": True, "contratos": contratos}


# ── Solenium token cache ──────────────────────────────────────────────────────
_sol_cache: dict = {"token": None, "expires_at": 0.0}


async def _solenium_token() -> str | None:
    import time
    if _sol_cache["token"] and time.time() < _sol_cache["expires_at"]:
        return _sol_cache["token"]
    if not settings.SOLENIUM_USER or not settings.SOLENIUM_PASS:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                settings.SOLENIUM_AUTH_URL,
                json={"username": settings.SOLENIUM_USER, "password": settings.SOLENIUM_PASS},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            token = data.get("access") or data.get("token") or data.get("key") or ""
            if token:
                import time as _t
                _sol_cache["token"] = token
                _sol_cache["expires_at"] = _t.time() + 20 * 3600
            return token or None
    except Exception:
        return None


async def _solenium_inverters(proyecto: Proyecto) -> tuple[list, str | None]:
    token = await _solenium_token()
    if not token:
        return [], "Solenium no configurado o sin credenciales"

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"{settings.SOLENIUM_DATA_URL}/project/",
                params={"menu": "1"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 401:
                _sol_cache["token"] = None
                return [], "Solenium: sesión expirada"
            projs = r.json() if r.status_code == 200 else []
            projs = projs if isinstance(projs, list) else projs.get("results", [])

            sol_id = proyecto.project_id_solenium or ""
            if not sol_id:
                candidates = [
                    (proyecto.nombre_comercial or "").lower(),
                    (proyecto.sub_project or "").lower(),
                ]
                for sp in projs:
                    sp_name = (sp.get("name") or sp.get("nombre") or "").lower()
                    for cand in candidates:
                        if not cand:
                            continue
                        cand_words = [w for w in cand.split() if len(w) > 2]
                        if cand_words and sum(1 for w in cand_words if w in sp_name) / len(cand_words) >= 0.5:
                            sol_id = str(sp.get("id") or "")
                            break
                    if sol_id:
                        break

            if not sol_id:
                return [], "No se encontró el proyecto en Solenium (configura project_id_solenium en el proyecto)"

            r2 = await c.get(
                f"{settings.SOLENIUM_DATA_URL}/project/{sol_id}/inverter/",
                headers={"Authorization": f"Bearer {token}"},
            )
            if r2.status_code == 401:
                # Limpiar token también aquí: si solo se limpiaba en la 1ª llamada,
                # un 401 en /inverter/ dejaba el token vencido cacheado hasta 20h y
                # todos los sondeos seguían fallando.
                _sol_cache["token"] = None
                return [], "Solenium: sesión expirada"
            if r2.status_code != 200:
                return [], f"Solenium inversores HTTP {r2.status_code}"
            body = r2.json()
            inverters = body if isinstance(body, list) else body.get("results", body.get("inverters", []))
            return inverters, None
    except Exception as e:
        return [], str(e)


async def _action_get_fmo_data(sub_project: str | None, date_from: str | None, date_to: str | None, db: Session) -> dict:
    if not sub_project:
        return {"ok": False, "error": "sub_project requerido"}

    proyecto = db.query(Proyecto).filter(Proyecto.sub_project == sub_project).first()

    contrato = None
    if proyecto:
        cs = (
            db.query(ContratoServicio)
            .filter(
                ContratoServicio.proyecto_id == proyecto.id,
                ContratoServicio.servicio_aplica == "mantenimiento",
                ContratoServicio.estado.in_(["vigente", "en_renovacion"]),
            )
            .first()
        )
        if cs:
            contrato = {
                "sub_project": sub_project,
                "nombre_comercial": proyecto.nombre_comercial,
                "disponibilidad_garantizada_pct": "97",
                "contratista": cs.prestador_nombre or "Unergy S.A.S.",
                "valor_estimado_ano1_cop": str(round(float(cs.tarifa_base) * 12)) if cs.tarifa_base else "0",
                "garantias_equipos": "",
                "numero_contrato": cs.numero_contrato or "",
            }

    inverters: list = []
    inverters_error: str | None = None
    if proyecto:
        inverters, inverters_error = await _solenium_inverters(proyecto)

    return {
        "ok": True,
        "contrato": contrato,
        "inverters": inverters,
        "inverters_error": inverters_error,
    }


# ── GET /monitoreo/resumen-generacion ─────────────────────────────────────────
@router.get("/resumen-generacion")
async def resumen_generacion_fleet(
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to:   str = Query(..., description="YYYY-MM-DD"),
    db: Session = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    """
    Generación real (API Unergy) de todos los proyectos activos con sub_project,
    agregada por fecha y también por proyecto, para el rango indicado.
    Usado por los gráficos de Monitoreo de Fallas.
    """
    import asyncio

    proyectos_db = db.query(Proyecto).filter(
        Proyecto.sub_project.isnot(None),
        Proyecto.estado == "en_operacion",
    ).all()

    if not proyectos_db:
        return {"projects_count": 0, "dates": [], "by_project": []}

    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    except Exception:
        return {"projects_count": 0, "dates": [], "by_project": []}

    d_from_dt    = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=_COL_TZ)
    d_to_dt      = datetime(d_to.year,   d_to.month,   d_to.day,  23, 59, 59, tzinfo=_COL_TZ)
    fetch_from   = (d_from_dt - timedelta(days=2)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetch_to     = d_to_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        token = await _unergy_token()
    except Exception:
        return {"projects_count": len(proyectos_db), "dates": [], "by_project": [], "error": "token_error"}

    by_date: dict[str, float] = {}
    by_project: list[dict] = []

    async def fetch_one(p: Proyecto):
        sub = p.sub_project
        try:
            readings = await _fetch_unergy_raw(token, sub, fetch_from, fetch_to, verified_only=True)
            if not readings:
                readings = await _fetch_unergy_raw(token, sub, fetch_from, fetch_to, verified_only=False)
            return p, _compute_deltas(readings, d_from_dt, d_to_dt)
        except Exception:
            return p, []

    results = await asyncio.gather(*[fetch_one(p) for p in proyectos_db], return_exceptions=True)

    for item in results:
        if not isinstance(item, tuple):
            continue
        p, entries = item
        total_kwh = 0.0
        for e in entries:
            fecha = e.get("date", "")
            kwh   = float(e.get("kwh") or 0)
            if fecha:
                by_date[fecha] = by_date.get(fecha, 0.0) + kwh
                total_kwh += kwh
        by_project.append({
            "proyecto_id":  p.id,
            "nombre":       p.nombre_comercial,
            "sub_project":  p.sub_project,
            "kwh_real":     round(total_kwh, 1),
        })

    return {
        "projects_count": len(proyectos_db),
        "dates": [
            {"fecha": f, "kwh_real": round(v, 1)}
            for f, v in sorted(by_date.items())
        ],
        "by_project": sorted(by_project, key=lambda x: x["kwh_real"], reverse=True),
    }


# ── GET /monitoreo/_legacy ────────────────────────────────────────────────────
@router.get("/_legacy")
async def legacy_bridge(
    action: str = Query(...),
    sub_project: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    db: Session = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    if action == "getGeneration":
        return await _action_get_generation(sub_project, date_from, date_to, db)
    if action == "getProjects":
        return _action_get_projects(db)
    if action == "getPortfolios":
        return _action_get_portfolios(db)
    if action == "getAllContratos":
        return _action_get_all_contratos(db)
    if action == "getFMOData":
        return await _action_get_fmo_data(sub_project, date_from, date_to, db)
    if action == "sendCode":
        return {"ok": True}
    raise HTTPException(400, f"Acción no reconocida: {action}")


# ── POST /monitoreo/_legacy ───────────────────────────────────────────────────
@router.post("/_legacy")
async def legacy_bridge_post(
    payload: dict,
    current_user: Usuario = Depends(get_current_user),
):
    action = payload.get("action", "")

    if action == "savePhoto":
        import base64
        import re
        from pathlib import Path as _Path

        fault_id = re.sub(r"[^\w\-]", "_", str(payload.get("faultId") or "unknown"))
        photo_name = re.sub(r"[^\w\-\.]", "_", str(payload.get("photoName") or "foto.jpg"))
        b64 = payload.get("b64") or ""
        mime = payload.get("mimeType") or "image/jpeg"

        ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
        ext = ext_map.get(mime, ".jpg")
        if not photo_name.lower().endswith(ext):
            photo_name = photo_name.rsplit(".", 1)[0] + ext

        try:
            img_bytes = base64.b64decode(b64)
        except Exception:
            return {"ok": False, "error": "Base64 inválido"}

        from pathlib import Path as _Path
        folder = _Path("uploads/fotos") / fault_id
        folder.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{photo_name}"
        (folder / filename).write_bytes(img_bytes)

        url = f"/static/uploads/fotos/{fault_id}/{filename}"
        return {"ok": True, "folderUrl": url, "photoUrl": url}

    if action == "sendCode":
        return {"ok": True}

    raise HTTPException(400, f"Acción POST no reconocida: {action}")


# ── POST /monitoreo/admin/sync-proyectos (temporal) ───────────────────────────
@router.post("/admin/sync-proyectos")
def sync_proyectos(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """Llena campos faltantes de proyectos desde proyectos_solares_completo.json.
    Solo admin.

    Ya no aplica el mapeo hardcodeado de Operadores de Red (OR_MAP): desde
    2026-07-02, proyectos.operador_red se llena de forma confiable desde
    fronteras.operador_red (dato oficial de GESCON) a través del vínculo
    fronteras.proyecto_id -- ese mapeo hardcodeado quedaba obsoleto en cuanto
    se agregaba un proyecto nuevo, y podía pisar en silencio el dato bueno con
    un valor viejo si este endpoint se volvía a correr."""
    if current_user.rol.value not in ("admin", "operaciones"):
        raise HTTPException(403, "Sin permisos")

    import json as _json
    import re as _re
    from pathlib import Path as _Path
    from app.models.proyectos import ProyectoInfoTecnica

    NOMBRE_MAP = {
        "MGS 0004 Valle de Gandalf": "Gandalf",
        "MGS 0005 Cañahuate": "Cañahuate",
        "MGS 0006 Perijá": "Perija",
        "MGS 0007 La Paz Vallenata": "La Paz Vallenata",
        "MGS 0008 La Paz Verso": "La Paz Verso",
        "MGS 0009 El Molino": "Molino",
        "MGS 0010 - Villanueva": "Villanueva",
        "MGS 0011 El Roble": "El Roble",
        "MGS 0013 La Mesa": "La Mesa",
        "MGS 0014 - El Olimpo": "El Olimpo",
        "MGS 0016 - Puya": "La Puya",
        "MGS 0017- Esmeralda": "Esmeralda",
        "MGS 0018 La Paz Leyenda": "La Paz Leyenda",
        "MGS 0019 El Merengue": "merengue",
        "Complejo Industrial Cedillanos": "Cedillanos",
        "GRANJA SOLAR SAN AGUSTIN": "San Agustin",
    }

    def _find(kw):
        r = db.query(Proyecto).filter(Proyecto.nombre_comercial == kw).first()
        if r: return r
        return db.query(Proyecto).filter(Proyecto.nombre_comercial.ilike(f"%{kw}%")).first()

    def _clean_dpto(s):
        return _re.sub(r"\s+[Dd]epartment$", "", (s or "").strip()).strip()

    def _upsert_it(pid, n):
        it = db.query(ProyectoInfoTecnica).filter(ProyectoInfoTecnica.proyecto_id == pid).first()
        if it:
            if not it.cantidad_total_paneles:
                it.cantidad_total_paneles = n
        else:
            db.add(ProyectoInfoTecnica(proyecto_id=pid, cantidad_total_paneles=n))

    json_path = _Path(__file__).parent.parent.parent.parent / "data" / "proyectos_solares_completo.json"
    data = _json.loads(json_path.read_text(encoding="utf-8"))

    updated, skipped = [], []

    for row in data:
        nombre = row.get("nombre_topico", "").strip()
        kw = NOMBRE_MAP.get(nombre) or _re.sub(r"^MGS\s*\d+\s*[-\s]*", "", nombre).strip() or nombre
        proj = _find(kw)
        if not proj:
            skipped.append(nombre)
            continue
        changed = False
        dpto = _clean_dpto(row.get("departamento") or "")
        if dpto and not proj.departamento:
            proj.departamento = dpto; changed = True
        ciudad = (row.get("ciudad") or "").strip()
        if ciudad and not proj.municipio:
            proj.municipio = ciudad; changed = True
        kwp = row.get("potencia_instalada_dc_kwp")
        if kwp is not None and not proj.potencia_instalada_kwp:
            proj.potencia_instalada_kwp = kwp; changed = True
        paneles = row.get("numero_de_paneles")
        if paneles is not None:
            it = db.query(ProyectoInfoTecnica).filter(ProyectoInfoTecnica.proyecto_id == proj.id).first()
            if not (it and it.cantidad_total_paneles):
                _upsert_it(proj.id, paneles); changed = True
        if changed:
            updated.append(proj.nombre_comercial)

    db.commit()
    return {
        "ok": True,
        "json_actualizados": updated,
        "json_saltados": skipped,
    }
