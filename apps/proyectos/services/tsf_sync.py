"""Sincronización del pipeline de Sun Factory (TSF) con `proyectos`.

Puerto de `app/services/tsf_sync.py`. Fuente principal: Sun Factory —la "BD de
Solenium/TSF"—, accesible por internet; no depende de `originabotdb`, que solo se
alcanza desde la red interna.

De las 696 líneas originales solo cuatro funciones tocaban la base; el resto
(normalización de nombres, matching, HTTP contra Sun Factory) vino verbatim.

**`sunfactory_project_id` manda sobre todo lo demás.** Es el único identificador
que Sun Factory garantiza estable aunque rebautice el proyecto entre dos
sincronizaciones: fue justo un `base_name` cambiado lo que duplicó Monterrubio
(ids 210 y 252, jul-2026) cuando el match era solo por texto.

**Nada se crea automáticamente acá.** Un proyecto de Sun Factory sin match queda
en `sin_match` o como sugerencia de vínculo, para que una persona lo confirme.
Crear al vuelo es como se producen los duplicados en silencio.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone as utc_tz

import httpx
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.comun.config import settings
from apps.comun.nombre_matching import mejor_candidato
from apps.proyectos.models import Proyecto

logger = logging.getLogger("operaciones.tsf")

_ENERG_MILESTONE_RE = re.compile(r"retie|legaliz|energiz|puesta\s+en\s+marcha|\bpem\b|\bpdm\b", re.I)


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None



# Siglas que .title() rompería (p. ej. "AGGE" -> "Agge"); se restauran a
# mayúscula tras el title-case.
_SIGLAS_MAYUSCULA = {"AGGE", "AGPE"}


def _derive_commercial_name(code: str) -> str:
    """Nombre legible derivado del código de proyecto de origina.

    Los códigos tienen la forma `<PREFIJO>_<SITIO>`, p. ej.
    `COLSUCT3P1_MORROA_SUR` → "Morroa Sur", y el sitio puede traer guiones
    (`LA-JAGUA-DEL-PILAR` → "La Jagua Del Pilar"). Es el respaldo cuando el
    proyecto aún no está en la tabla `proyectos`."""
    if not code:
        return ""
    parts = code.split("_", 1)
    prefix = parts[0]
    is_code_prefix = bool(re.match(r"^COL[A-Z0-9]*$", prefix)) or any(c.isdigit() for c in prefix)
    readable = (parts[1] if len(parts) > 1 and is_code_prefix else code)
    readable = re.sub(r"[_-]+", " ", readable).strip()
    if not readable:
        return code
    titled = readable.title()
    return " ".join(
        w.upper() if w.upper() in _SIGLAS_MAYUSCULA else w
        for w in titled.split(" ")
    )


def _parece_codigo(nombre: str | None) -> bool:
    """True si `nombre` en realidad es el código interno (p. ej.
    `COLBOYT123P1_FIRAVITOBA_OCCIDENTE`) en vez de un nombre comercial real --
    Sun Factory a veces guarda el mismo código en su campo `name`. El código
    de todos modos siempre queda a salvo en `origina_code`/`codigo_tsf`
    (vienen del campo `base_name`, aparte), esto solo evita que ensucie el
    nombre visible."""
    if not nombre or "_" not in nombre:
        return not nombre
    prefix = nombre.split("_", 1)[0]
    return bool(re.match(r"^COL[A-Z0-9]*$", prefix)) or any(c.isdigit() for c in prefix)


def _tsf_code_from_base_name(base_name: str | None) -> str | None:
    """Código de frontera CREG/TSF derivado del `base_name` de Sun Factory.

    `COLCEST55P2_VALLEDUPAR_NORTE` → `COLCEST55P2`. Es el "Código TSF" que el
    equipo registra manualmente en `proyectos.codigo_tsf`, así que sirve para
    cruzar y evitar duplicados. Devuelve None si el prefijo no tiene pinta de
    código CREG (p. ej. `SMGS_0006_FEN5_...`)."""
    if not base_name:
        return None
    prefix = base_name.split("_", 1)[0]
    return prefix if re.match(r"^COL[A-Z0-9]+$", prefix) else None


# ── Cronogramas EPC de Sun Factory (Solenium) ───────────────────────────────────

def _sunfactory_token() -> str | None:
    user = settings.SUNFACTORY_USERNAME or settings.SOLENIUM_USER
    password = settings.SUNFACTORY_PASSWORD or settings.SOLENIUM_PASS
    if not (user and password and settings.SUNFACTORY_AUTH_URL):
        return None
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            settings.SUNFACTORY_AUTH_URL,
            json={"username": user, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access"]


def _pick_energization_milestone(milestones: list[dict]) -> dict | None:
    """Función PURA: elige el hito de energización de una lista de milestones."""
    if not milestones:
        return None
    dated = [m for m in milestones if m.get("date") or m.get("planned_date")]
    if not dated:
        return None
    matches = [m for m in dated if _ENERG_MILESTONE_RE.search(m.get("name", "") or "")]
    pool = matches or dated
    chosen = max(pool, key=lambda m: (m.get("planned_date") or m.get("date") or ""))

    ed = _parse_iso_date(chosen.get("date") or chosen.get("planned_date"))
    if not ed:
        return None
    progress = chosen.get("progress") or {}
    avance = progress.get("calculated_percentage")
    if avance is None:
        avance = progress.get("activity_percentage")
    return {"energization_date": ed, "avance_pct": avance, "milestone": chosen.get("name")}


def _sunfactory_milestones_raw(token: str, project_id: int) -> list[dict]:
    """Milestones crudos de un proyecto vía Sun Factory (con paginación). Separado
    de `_sunfactory_energization` para poder inspeccionarlos sin filtrar (ver
    endpoint de diagnóstico `/proximos-energizar/{id}/debug-sunfactory`)."""
    base = settings.SUNFACTORY_API_URL.rstrip("/")
    milestones: list[dict] = []
    url: str | None = f"{base}/project/{project_id}/milestones/?limit=200"
    with httpx.Client(timeout=40, headers={"Authorization": f"Bearer {token}"}) as client:
        pages = 0
        while url and pages < 20:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            milestones += data.get("results", []) if isinstance(data, dict) else (data or [])
            url = data.get("next") if isinstance(data, dict) else None
            pages += 1
    return milestones


def _sunfactory_energization(token: str, project_id: int) -> dict | None:
    """Hito de energización de un proyecto vía Sun Factory (con paginación)."""
    try:
        milestones = _sunfactory_milestones_raw(token, project_id)
    except Exception as exc:
        logger.debug("Sun Factory milestones failed for project %s: %s", project_id, exc)
        return None
    return _pick_energization_milestone(milestones)


# ── Sun Factory como FUENTE PRINCIPAL (la "BD de Solenium/TSF") ─────────────────
# El endpoint /project/ de Sun Factory ya trae nombre, base_name, ubicación
# (lat/lon/city/department) y estado — accesible por internet, sin depender de
# originabotdb (que solo es alcanzable desde la red interna de Unergy y hace
# timeout desde Railway/fuera). Esta es la vía que pidió el usuario: copiar
# directo desde Solenium/TSF.

# state (int) de Sun Factory → etiqueta de fase. Solo estos estados se importan
# como "próximos a energizarse"; se excluyen 2 (Operación y Mantenimiento, ya
# energizado) y 5 (Debida diligencia, demasiado temprano). Se usa el int y no la
# descripción para evitar problemas de acentos/encoding.
_SF_IMPORT_STATES = {
    1: "En construcción",      # Construcción
    3: "Próximo a energizar",  # Despliegue (PEM/pruebas, lo más cercano)
    4: "En construcción",      # BT y Contrato
}


def _sunfactory_all_projects(token: str) -> list[dict]:
    """Lista completa de proyectos de Sun Factory (paginando /project/)."""
    base = settings.SUNFACTORY_API_URL.rstrip("/")
    url: str | None = f"{base}/project/?limit=200"
    out: list[dict] = []
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"}) as client:
        pages = 0
        while url and pages < 30:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            out += data.get("results", []) if isinstance(data, dict) else (data or [])
            url = data.get("next") if isinstance(data, dict) else None
            pages += 1
    return out


def _next_milestone_date(project: dict) -> date | None:
    """Fecha del próximo hito pendiente (respaldo cuando no se enriquece con
    el hito de energización RETIE/legalización)."""
    nm = project.get("next_milestone") or {}
    return _parse_iso_date(nm.get("planned_end_date") or nm.get("end_date")
                           or nm.get("planned_date") or nm.get("date"))


def fetch_sunfactory_projects(enrich_dates: bool = True) -> tuple[list[dict], list[str]]:
    """Proyectos del pipeline DIRECTO desde Sun Factory (Solenium/TSF).

    Devuelve `(proyectos, warnings)`. Cada proyecto:
    `{ origina_code, solenium_id, commercial_name, status, municipio,
       departamento, latitud, longitud, energization_date, avance_pct,
       monthly_mwh }`. `origina_code` = base_name (o `SF-<id>` si no tiene),
    usado como llave estable de upsert."""
    warnings: list[str] = []
    try:
        token = _sunfactory_token()
    except Exception as exc:
        logger.warning("Sun Factory auth falló: %s", exc)
        return [], [f"No se pudo autenticar contra Sun Factory: {exc}"]
    if not token:
        return [], ["Credenciales de Sun Factory no configuradas (SUNFACTORY_/SOLENIUM_)."]

    try:
        raw = _sunfactory_all_projects(token)
    except Exception as exc:
        logger.warning("Sun Factory lista de proyectos falló: %s", exc)
        return [], [f"No se pudo leer la lista de proyectos de Sun Factory: {exc}"]

    wanted = [p for p in raw if p.get("state") in _SF_IMPORT_STATES]

    # Enriquecer con el hito de energización (RETIE/legalización) por proyecto,
    # concurrente y best-effort. Si falla, se usa el next_milestone como respaldo.
    energ_map: dict[int, dict] = {}
    if enrich_dates and wanted:
        ids = [p["id"] for p in wanted if p.get("id") is not None]
        try:
            with ThreadPoolExecutor(max_workers=min(len(ids), 12)) as pool:
                for pid, energ in pool.map(lambda i: (i, _sunfactory_energization(token, i)), ids):
                    if energ:
                        energ_map[pid] = energ
        except Exception as exc:
            logger.warning("Sun Factory enriquecimiento de hitos falló: %s", exc)
            warnings.append("No se pudieron leer los hitos de energización; se usan fechas tentativas.")

    projects: list[dict] = []
    for p in wanted:
        pid = p.get("id")
        base_name = p.get("base_name")
        code = base_name or (f"SF-{pid}" if pid is not None else None)
        if not code:
            continue
        energ_info = energ_map.get(pid) or {}
        energ = energ_info.get("energization_date") or _next_milestone_date(p)
        lat = p.get("lat")
        lon = p.get("lon")
        projects.append({
            "origina_code": code,
            "base_name": base_name,
            "tsf_code": _tsf_code_from_base_name(base_name),
            "solenium_id": pid,
            "commercial_name": (
                p.get("name") if p.get("name") and not _parece_codigo(p.get("name"))
                else _derive_commercial_name(base_name or "")
            ),
            "status": _SF_IMPORT_STATES.get(p.get("state"), "En construcción"),
            "municipio": p.get("city"),
            "departamento": p.get("department"),
            "latitud": float(lat) if lat not in (None, "") else None,
            "longitud": float(lon) if lon not in (None, "") else None,
            "energization_date": energ,
            "avance_pct": energ_info.get("avance_pct"),
            "monthly_mwh": None,  # Sun Factory no expone potencia en el listado
        })
    return projects, warnings


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9\s]", " ", s.lower()).strip()


_PREFIJO_CATALOGO_RE = re.compile(r"^(?:(?:mgs|mgr|minigranja|granja|solar|gd)\b\s*)+")
_NUMERO_CATALOGO_RE = re.compile(r"^0*\d+\s*[-–]?\s*")

# Solenium abrevia la dirección pegada al número de fase (p. ej. "Chiriguana N2"
# = "Chiriguana Norte 2", "Valencia Or_1" = "Valencia Oriente 1") -- se expande
# ANTES de comparar para que coincida con la convención completa que usamos
# internamente. No tocar: '\s*' cubre tanto "n2" (sin espacio) como "or 1"
# (con espacio, tras _norm() convertir el "_" en espacio).
_ABREV_DIRECCION = {"n": "norte", "s": "sur", "or": "oriente", "oc": "occidente", "occ": "occidente"}
_ABREV_DIRECCION_RE = re.compile(
    r"\b(" + "|".join(_ABREV_DIRECCION) + r")\s*(\d+)\b"
)


def _expandir_abreviaturas_direccion(n: str) -> str:
    return _ABREV_DIRECCION_RE.sub(lambda m: f"{_ABREV_DIRECCION[m.group(1)]} {m.group(2)}", n)


def _core(s: str | None) -> str:
    """Nombre normalizado sin prefijo de catálogo (MGS/Minigranja/GD/... + el
    número que le sigue inmediatamente) -- para comparar el "nombre de lugar"
    real detrás de convenciones de nomenclatura distintas (p. ej.
    "MGS 0032 - El Paso Norte" vs "Minigranja 0032 - El Paso Norte").

    A propósito NO toca números en cualquier otra posición del nombre: esos
    suelen ser el número de FASE real ("Norte 2" vs "Norte 4"), y borrarlos
    colapsaba fases distintas en el mismo core -- bug encontrado 2026-07-08
    comparando contra Quoia/Solenium en producción (10 proyectos con fase
    activa que ya reportaban generación real quedaban indistinguibles de
    fases hermanas). También expande las abreviaturas de dirección que usa
    Solenium (N2/Or_1) antes de comparar, si no, "sin match" falso."""
    n = _norm(s)
    n = _expandir_abreviaturas_direccion(n)
    n = _PREFIJO_CATALOGO_RE.sub("", n)
    n = _NUMERO_CATALOGO_RE.sub("", n)
    return re.sub(r"\s+", " ", n).strip()


def _buscar_candidato_similar(nombre: str | None, municipio: str | None):
    """Busca un proyecto YA existente (sin sunfactory_project_id todavía) que
    podría ser el mismo que este proyecto de Sun Factory, cuando no hubo match
    exacto por id/código.

    Deliberadamente MÁS permisivo que el chequeo de create_proyecto: compara por
    "nombre de lugar" (sin prefijos MGS/Minigranja/GD ni números) en vez de
    nombre completo exacto, porque el caso que hay que atrapar aquí es justo ese
    (mismo lugar, prefijo o número escritos distinto -- Mapalé/El Paso Norte).
    Aquí nadie se crea automáticamente con este match: solo se guarda como
    sugerencia para que un humano la confirme o descarte con
    `POST /proyectos/{id}/vincular-sunfactory/{sunfactory_project_id}`. Un falso
    positivo aquí es barato (se descarta y, si de verdad es un proyecto distinto,
    se crea a mano); uno en create_proyecto no lo es (bloquearía una creación
    legítima al vuelo). El costo de ser permisivo: desarrollos con varias fases
    en el mismo lugar (p. ej. "Chinú Sur" y "Chinú Sur 2", que son plantas reales
    distintas) también se sugerirán como posible match -- se descartan con un
    clic si no aplican, no se pierde nada.

    NO se exige que `municipio` coincida (solo se usa para mostrarlo en la
    sugerencia): un `municipio` mal cargado en el proyecto existente (p. ej. el
    departamento en vez del municipio real -- pasó con "El Paso Norte", que
    tenía guardado "Cesar") descartaba en silencio un match de nombre correcto
    y terminaba creando el duplicado que se quería evitar. Mejor sugerir de más
    que duplicar en silencio.

    Tampoco se excluyen los proyectos que YA tienen un `sunfactory_project_id`
    distinto (antes sí se excluían). Caso real "Monterrubio": Sun Factory
    reporta el mismo proyecto bajo dos ids propios (106 y 111) -- uno se vincula
    primero, y si el otro quedara excluido de la búsqueda, la próxima
    sincronización lo vuelve a crear como duplicado en silencio. Al no excluirlo,
    se sigue sugiriendo cada vez (puede repetirse hasta que alguien lo confirme o
    Sun Factory deje de mandarlo) -- más repetitivo, pero nunca duplica solo.
    """
    if not nombre:
        return None
    objetivo = _core(nombre)
    if len(objetivo) < 4:
        return None
    rows = Proyecto.objects.filter(deleted_at__isnull=True).only(
        "id", "nombre_comercial", "municipio", "sunfactory_project_id",
    )
    for r in rows:
        n = _core(r.nombre_comercial)
        if len(n) < 4:
            continue
        if objetivo in n or n in objetivo:
            return r
    return None


# ── Upsert en `proyectos` ───────────────────────────────────────────────────────

# Fase de construcción persistida (slug) ↔ etiqueta del pipeline.
_STATUS_TO_FASE = {
    "En construcción": "en_construccion",
    "Pruebas": "pruebas",
    "Próximo a energizar": "proximo_energizar",
    "Energizado": "energizado",
}
# Inverso: slug → etiqueta que consume el frontend.
_FASE_TO_LABEL = {v: k for k, v in _STATUS_TO_FASE.items()}


def sync_tsf_projects(enrich_dates: bool = True) -> dict:
    """Upsert del pipeline TSF en `proyectos`. Devuelve estadísticas.

    `enrich_dates=False`: NO consulta los hitos de cada proyecto (que son ~99
    llamadas HTTP y pueden hacer timeout en un request síncrono); usa la fecha del
    `next_milestone` del listado. Úsalo para el botón on-demand. El job de 6h corre
    con `enrich_dates=True` para traer la fecha de energización precisa (RETIE).

    Fuente principal: Sun Factory (la "BD de Solenium/TSF"), accesible por internet.
    No depende de originabotdb (que solo es alcanzable desde la red interna)."""
    projects, warnings = fetch_sunfactory_projects(enrich_dates=enrich_dates)
    stats = {"creados": 0, "actualizados": 0, "sin_cambios": 0, "errores": 0, "sin_match": 0,
             "total_pipeline": len(projects), "warnings": warnings, "fuente": "sunfactory",
             "sugerencias_vinculo": [], "ambiguos": []}

    for p in projects:
        code = p["origina_code"]
        if not code:
            continue
        try:
            # El equipo registra el "Código TSF" (prefijo CREG, p. ej. COLCEST55P2)
            # al crear un proyecto a mano. Cruzamos por codigo_tsf (prefijo o
            # base_name completo) ADEMÁS de origina_code para no duplicar lo que
            # ya existe en la BD.
            #
            # `sunfactory_project_id` tiene prioridad sobre todo lo demás: es el
            # único identificador que Sun Factory garantiza que no cambia, aunque
            # le rebautice el proyecto (base_name/origina_code) entre una
            # sincronización y otra -- eso fue justo lo que causó el duplicado de
            # Monterrubio (id 210 vs 252, jul-2026): el `base_name` cambió y el
            # match por texto dejó de reconocer el proyecto ya existente.
            tsf_code = p.get("tsf_code")
            base_name = p.get("base_name")
            solenium_pipeline_id = p.get("solenium_id")
            # Sin LIMIT 1: si origina_code/codigo_tsf no son UNIQUE en el modelo,
            # más de un proyecto en BD puede compartir el mismo código (caso real:
            # "Astrea 1 (Calipso)" duplicado, ids 274/275, mismo codigo_tsf). Antes
            # esto elegía uno en silencio con LIMIT 1 y el otro quedaba huérfano de
            # las actualizaciones de Sun Factory sin que nadie se enterara.
            criterio = Q(origina_code=code)
            if solenium_pipeline_id is not None:
                criterio |= Q(sunfactory_project_id=solenium_pipeline_id)
            if tsf_code:
                criterio |= Q(codigo_tsf=tsf_code)
            if base_name:
                criterio |= Q(codigo_tsf=base_name)
            matches = list(
                Proyecto.objects
                .filter(deleted_at__isnull=True)
                .filter(criterio)
                # El match por `sunfactory_project_id` gana: es el único id que
                # Sun Factory garantiza estable aunque rebautice el proyecto.
                .annotate(por_sf_id=Case(
                    When(sunfactory_project_id=solenium_pipeline_id, then=Value(0)),
                    default=Value(1), output_field=IntegerField(),
                ))
                .order_by("por_sf_id", "id")
                .only("id", "estado")
            )
            if len(matches) > 1:
                stats["ambiguos"].append({
                    "origina_code": code,
                    "codigo_tsf": tsf_code,
                    "sunfactory_project_id": solenium_pipeline_id,
                    "candidatos_ids": [m.id for m in matches],
                    "motivo": (
                        "más de un proyecto en BD comparte este código -- se actualizó "
                        "solo el primero (menor id); revisar si son duplicados "
                        "(POST /proyectos/{ganador}/merge/{perdedor})"
                    ),
                })
            existing = matches[0] if matches else None

            fase = _STATUS_TO_FASE.get(p["status"], "en_construccion")
            energ = p["energization_date"]

            if existing is None:
                # Ningun match exacto por id/codigo. Si hay un proyecto ya existente
                # con nombre parecido (tipicamente creado a mano, sin ningun codigo
                # de Sun Factory nunca registrado), se sugiere el vinculo -- ver
                # POST /proyectos/{id}/vincular-sunfactory.
                candidato = _buscar_candidato_similar(
                    p.get("commercial_name") or code, p.get("municipio"),
                )
                if candidato is not None:
                    stats["sugerencias_vinculo"].append({
                        "sunfactory_project_id": solenium_pipeline_id,
                        "sunfactory_nombre": p.get("commercial_name") or code,
                        "sunfactory_municipio": p.get("municipio"),
                        "candidato_id": candidato.id,
                        "candidato_nombre": candidato.nombre_comercial,
                        "candidato_municipio": candidato.municipio,
                        # Si no es None, el candidato ya esta vinculado a OTRO id de
                        # Sun Factory -- confirmar la sugerencia reemplazaria ese
                        # vinculo (posible caso "mismo proyecto, dos ids en Sun
                        # Factory", como Monterrubio 106/111).
                        "candidato_sunfactory_id_previo": candidato.sunfactory_project_id,
                    })
                    continue

                # Sin match de ningun tipo: ya NO se crea aqui. Queda para que
                # /proyectos/pendientes lo detecte y un humano lo confirme.
                stats["sin_match"] += 1
            else:
                # Si el proyecto ya quedó confirmado como en operación (ej. vía
                # Proyectos pendientes con evidencia real de Quoia/Solenium), no
                # dejar que Sun Factory lo regrese a una fase de obra anterior
                # solo porque su propio tracker todavía no se actualizó -- el
                # estado real (`estado`) manda sobre el pipeline de construcción,
                # salvo que Sun Factory ya reporte "energizado", que siempre gana.
                set_fase = fase == "energizado" or existing.estado != "en_operacion"
                # COALESCE(existente, nuevo): enlaza y rellena sin pisar lo que el
                # operador ya tenga. `sunfactory_project_id` se respalda la PRIMERA
                # vez que hay match (por texto o por id) — de ahí en adelante el
                # match ya no depende de que el texto siga siendo el mismo.
                cambios = {
                    # `avance_obra_pct` es la EXCEPCION deliberada: es una
                    # medicion viva del avance de obra, nadie la carga a mano y
                    # congelarla en su primer valor dejaria "Proximos a
                    # energizar" mostrando un porcentaje viejo para siempre. Por
                    # eso ahi el valor nuevo gana.
                    "avance_obra_pct": Coalesce(Value(p.get("avance_pct")), F("avance_obra_pct")),
                    # La potencia NO: es un atributo estable de la planta que
                    # carga el operador, y hasta el 2026-09-10 este era el unico
                    # campo del diccionario --junto al avance-- donde el valor de
                    # Sun Factory PISABA lo cargado, contra lo que dice el
                    # comentario de arriba y contra los otros ocho campos. Ahora
                    # rellena solo si esta vacia.
                    "potencia_instalada_kwp": Coalesce(
                        F("potencia_instalada_kwp"), Value(p.get("installed_power_kwp"))),
                    "origina_code": Coalesce(F("origina_code"), Value(code)),
                    "codigo_tsf": Coalesce(F("codigo_tsf"), Value(tsf_code)),
                    "sunfactory_project_id": Coalesce(
                        F("sunfactory_project_id"), Value(solenium_pipeline_id)),
                    "municipio": Coalesce(F("municipio"), Value(p["municipio"])),
                    "departamento": Coalesce(F("departamento"), Value(p["departamento"])),
                    "latitud": Coalesce(F("latitud"), Value(p["latitud"])),
                    "longitud": Coalesce(F("longitud"), Value(p["longitud"])),
                    "updated_at": timezone.now(),
                }
                if set_fase:
                    cambios["fase_construccion"] = fase
                if energ is not None:
                    cambios["fecha_estimada_energizacion"] = energ
                with transaction.atomic():
                    Proyecto.objects.filter(pk=existing.id).update(**cambios)
                stats["actualizados"] += 1
        except Exception as exc:
            logger.warning("upsert TSF falló para %s: %s", code, exc)
            stats["errores"] += 1

    return stats


# ── Backfill de departamento/municipio/codigo_tsf por nombre ────────────────────
# `sync_tsf_projects()` ya rellena estos tres campos continuamente (job de 6h +
# botón "Actualizar"), pero SOLO para proyectos ya vinculados por ID
# (sunfactory_project_id/origina_code/codigo_tsf/base_name). Un proyecto sin
# ningún vínculo por ID (creado a mano, o cuyo vínculo con Sun Factory nunca se
# confirmó) nunca los recibe por esa vía -- esto cubre ese hueco emparejando
# por nombre, con el mismo umbral estricto que el backfill de Unergy (0.95):
# mejor dejar el campo vacío que asignar la ubicación de otro proyecto.
UMBRAL_UBICACION_TSF = 0.95


def _match_sunfactory_por_id(proyecto: Proyecto, sf_projects: list[dict]) -> dict | None:
    """Mismo orden de prioridad que sync_tsf_projects(): sunfactory_project_id
    > origina_code > codigo_tsf/base_name."""
    if proyecto.sunfactory_project_id is not None:
        for sp in sf_projects:
            if sp.get("solenium_id") == proyecto.sunfactory_project_id:
                return sp
    if proyecto.origina_code:
        for sp in sf_projects:
            if (sp.get("origina_code") or "").upper() == proyecto.origina_code.upper():
                return sp
    if proyecto.codigo_tsf:
        for sp in sf_projects:
            if (sp.get("tsf_code") or "").upper() == proyecto.codigo_tsf.upper() \
               or (sp.get("base_name") or "").upper() == proyecto.codigo_tsf.upper():
                return sp
    return None


def _match_sunfactory_seguro(proyecto: Proyecto, sf_projects: list[dict]) -> dict | None:
    """Vínculo por ID si existe (siempre confiable); si no, por nombre, solo
    si el score supera UMBRAL_UBICACION_TSF."""
    item = _match_sunfactory_por_id(proyecto, sf_projects)
    if item:
        return item
    candidatos = [(sp, [sp.get("commercial_name")]) for sp in sf_projects if sp.get("commercial_name")]
    item, score = mejor_candidato(proyecto.nombre_comercial, candidatos)
    return item if item and score >= UMBRAL_UBICACION_TSF else None


def _cambios_ubicacion_codigo_tsf(proyecto: Proyecto, item: dict) -> dict:
    """Solo los campos vacíos -- nunca pisa un valor ya cargado."""
    cambios = {}
    if not proyecto.departamento and item.get("departamento"):
        cambios["departamento"] = item["departamento"]
    if not proyecto.municipio and item.get("municipio"):
        cambios["municipio"] = item["municipio"]
    if not proyecto.codigo_tsf and item.get("tsf_code"):
        cambios["codigo_tsf"] = item["tsf_code"]
    return cambios


def backfill_ubicacion_codigo_tsf(apply: bool = False) -> dict:
    """Corrida masiva sobre proyectos existentes a los que les falte
    departamento, municipio o codigo_tsf. Ver
    scripts/backfill_ubicacion_tsf.py para el CLI (dry-run por defecto)."""
    candidatos_proyecto = list(
        Proyecto.objects
        .filter(deleted_at__isnull=True)
        .filter(
            Q(departamento__isnull=True) | Q(municipio__isnull=True)
            | Q(codigo_tsf__isnull=True)
        )
        .order_by("nombre_comercial")
    )
    if not candidatos_proyecto:
        return {"ok": True, "revisados": 0, "asignados": [], "sin_match_seguro": []}

    sf_projects, warnings = fetch_sunfactory_projects(enrich_dates=False)
    if not sf_projects:
        return {"ok": False, "error": "Sun Factory no devolvió proyectos" + (f" ({warnings[0]})" if warnings else "")}

    asignados: list[dict] = []
    sin_match_seguro: list[dict] = []

    for p in candidatos_proyecto:
        item = _match_sunfactory_seguro(p, sf_projects)
        if not item:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": "sin match seguro en Sun Factory",
            })
            continue
        cambios = _cambios_ubicacion_codigo_tsf(p, item)
        if not cambios:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": "matcheó con Sun Factory, pero ese registro tampoco tiene el dato que falta",
            })
            continue
        asignados.append({"proyecto_id": p.id, "nombre": p.nombre_comercial, "cambios": cambios})
        if apply:
            for campo, valor in cambios.items():
                setattr(p, campo, valor)
            p.save(update_fields=list(cambios))

    return {
        "ok": True,
        "revisados": len(candidatos_proyecto),
        "asignados": asignados,
        "sin_match_seguro": sin_match_seguro,
    }


def sincronizar_ubicacion_tsf_si_aplica(proyecto: Proyecto) -> dict | None:
    """Best-effort para UN proyecto, en el momento de crearlo/confirmarlo (ver
    app/api/v1/proyectos.py) -- llena departamento/municipio/codigo_tsf desde
    Sun Factory si falta alguno, para que los proyectos nuevos no dependan de
    que alguien corra el backfill manual más adelante.

    Nunca sobreescribe un valor ya cargado, y nunca lanza: si Sun Factory
    falla, está lento, o no hay match seguro, el proyecto simplemente queda
    como estaba."""
    if proyecto.departamento and proyecto.municipio and proyecto.codigo_tsf:
        return None
    try:
        sf_projects, _warnings = fetch_sunfactory_projects(enrich_dates=False)
        if not sf_projects:
            return None
        item = _match_sunfactory_seguro(proyecto, sf_projects)
        if not item:
            return None
        cambios = _cambios_ubicacion_codigo_tsf(proyecto, item)
        if cambios:
            for campo, valor in cambios.items():
                setattr(proyecto, campo, valor)
            proyecto.save(update_fields=list(cambios))
        return cambios or None
    except Exception:
        logger.warning(
            "No se pudo sincronizar ubicación/código TSF desde Sun Factory para proyecto %s (%s)",
            proyecto.id, proyecto.nombre_comercial, exc_info=True,
        )
        return None
