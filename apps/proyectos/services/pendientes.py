"""Candidatos de Sun Factory y Quoia que aún no están en `proyectos`.

Puerto de `app/services/proyectos_pendientes.py`. De sus 546 líneas, solo
`resolver_pendientes` tocaba la base: el resto es cruce contra dos APIs externas
y normalización de nombres, y vino verbatim.

**Nunca escribe.** Es una cola de sugerencias que una persona confirma con
`POST /proyectos/pendientes/{clave}/confirmar`. Crear al vuelo lo que dicen esas
dos APIs es como se producen los duplicados en silencio — pasó con Monterrubio,
que Sun Factory reporta bajo dos ids propios.

**Un proyecto ya "energizado" nunca se sugiere de vuelta a una fase de obra
anterior.** Sun Factory puede seguir trayendo un status desactualizado para un
proyecto ya confirmado operando (caso real 2026-07-09: Chima Oriente, Chiriguana
N1 y Valencia Oriente 1).

**Desde el 2026-09-10 esta cola es el unico camino por el que cambia la fase de
un proyecto que ya existe.** `sync_tsf_projects` la escribia sola cada 6 horas,
asi que estas sugerencias casi nunca alcanzaban a verse: la cola existia y
estaba vacia por construccion. Ahora la tarea solo rellena huecos y actualiza
las dos mediciones vivas de la obra (`avance_obra_pct` y la fecha estimada de
energizacion).

**Ignorar es por PLANTA y es para siempre.** La clave es `core:<nombre
normalizado>`, no el campo sugerido, asi que una planta ignorada no vuelve a
proponerse -- tampoco cuando cambie su fase mas adelante. Es lo que se pidio
explicitamente; el contrapeso es que para volver a verla hay que borrar su fila
de `proyectos_pendientes_ignorados`.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta

from apps.fronteras.models import Frontera

# Sun Factory usa este listado para el propio edificio de Solenium y para
# proyectos ya dados de baja -- nunca son candidatos reales. Vienen de
# app/services/proyectos_pendientes.py (lineas 44-45); el port conservo el
# filtro que las usa pero no las constantes, asi que listar pendientes
# moria con NameError.
_EXCLUIR_NOMBRES = ("solenium piso",)
_EXCLUIR_PREFIJOS = ("deprecated",)
from apps.proyectos.models import Proyecto, ProyectoPendienteIgnorado
from apps.proyectos.services.tsf_sync import (
    _SF_IMPORT_STATES, _STATUS_TO_FASE, _core, _derive_commercial_name,
    _parece_codigo, _sunfactory_all_projects, _sunfactory_token,
)

# `ponytail: el cliente de Quoia sigue en app/services/mgs/gaia_client.py`.
# Es HTTP puro, sin sesión de base: se mueve cuando se retire FastAPI.
from app.services.mgs.gaia_client import GaiaClient, _get_dynamic_maps

logger = logging.getLogger("operaciones.proyectos.pendientes")


def _excluir_por_nombre(nombre: str) -> bool:
    n = (nombre or "").strip().lower()
    return any(n.startswith(p) for p in _EXCLUIR_PREFIJOS) or any(x in n for x in _EXCLUIR_NOMBRES)


def _coord_valida(lat, lon) -> bool:
    """Filtra coordenadas placeholder de las fuentes (ej. -1,-1 o 0,0 como
    "sin dato", visto en Solenium) -- Colombia continental cae aprox. en
    lat [-5, 16], lon [-82, -65]."""
    if lat is None or lon is None:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat == lon:
        return False
    return -5 <= lat <= 16 and -82 <= lon <= -65


@dataclass
class _Candidato:
    fuentes: set[str] = field(default_factory=set)
    nombre_raw: str = ""
    core: str = ""
    municipio: str | None = None
    departamento: str | None = None
    latitud: float | None = None
    longitud: float | None = None
    tipo_proyecto: str | None = None
    fase_construccion: str | None = None
    estado_sugerido: str | None = None
    potencia_ac_kw: float | None = None
    capacidad_instalada_kwp: float | None = None
    sub_project: str | None = None
    project_id_solenium: str | None = None
    origina_code: str | None = None
    codigo_tsf: str | None = None
    sunfactory_project_id: int | None = None
    proyecto_id: int | None = None  # si ya se resolvió contra uno existente
    # Solo lo llena _candidatos_quoia -- generación real sostenida varios días
    # (no solo el último reportado). Se exige cuando el candidato NO tiene
    # corroboración de Sun Factory/Solenium (ver resolver_pendientes).
    generacion_multidia: bool = False


def _tsf_code_from_base_name(base_name: str | None) -> str | None:
    if not base_name:
        return None
    prefix = base_name.split("_", 1)[0]
    return prefix if re.match(r"^COL[A-Z0-9]+$", prefix) else None


def _candidatos_sunfactory() -> list[_Candidato]:
    """Todos los estados (no solo el pipeline de construcción) -- para
    /proyectos/pendientes nos interesa tanto lo que sigue en obra como lo
    que Sun Factory ya marcó como operando, no solo lo primero."""
    try:
        token = _sunfactory_token()
    except Exception:
        token = None
    if not token:
        return []
    try:
        raw = _sunfactory_all_projects(token)
    except Exception:
        return []

    out = []
    for p in raw:
        nombre = (p.get("name") or "").strip()
        if not nombre or _excluir_por_nombre(nombre):
            continue
        state = p.get("state")
        if state == 5:  # Debida diligencia -- demasiado temprano, ni prospecto confirmado
            continue
        base_name = p.get("base_name")
        lat, lon = p.get("lat"), p.get("lon")
        c = _Candidato(
            fuentes={"sunfactory"},
            nombre_raw=nombre if not _parece_codigo(nombre) else _derive_commercial_name(base_name or nombre),
            municipio=p.get("city"),
            departamento=p.get("department"),
            latitud=lat if _coord_valida(lat, lon) else None,
            longitud=lon if _coord_valida(lat, lon) else None,
            tipo_proyecto="minigranja" if p.get("is_minifarm") else "autoconsumo",
            origina_code=base_name,
            codigo_tsf=_tsf_code_from_base_name(base_name),
            sunfactory_project_id=p.get("id"),
        )
        if state == 2:
            c.estado_sugerido = "en_operacion"
            c.fase_construccion = "energizado"
        elif state in _SF_IMPORT_STATES:
            c.fase_construccion = _STATUS_TO_FASE.get(_SF_IMPORT_STATES[state])
        c.core = _core(c.nombre_raw)
        out.append(c)
    return out


def _nodo_tiene_generacion(gaia: GaiaClient, node_id: int | None, node_id_resp: int | None, fecha: str) -> bool:
    """True si el medidor (principal o respaldo, por node_id) reportó energía
    real (eae > 0) ese día.

    Por nodo, NO por frt_code: `/border/{frt}/measurements/` da 400 para
    algunos borders -- caso real "El Paso Norte" (Quoia lo tiene bajo otro
    `company`) -- mientras que por nodo funciona siempre y coincide con lo
    que muestra el dashboard de Quoia."""
    for nid in (node_id, node_id_resp):
        if nid is None:
            continue
        try:
            rows = gaia.get_node_measurements(nid, fecha, "eae")
        except Exception:
            continue
        total = 0.0
        for r in rows:
            for f in ("eaepd1", "eaepd2", "eaepd3"):
                v = r.get(f)
                if v is not None:
                    try:
                        total += float(v)
                    except (TypeError, ValueError):
                        pass
        if total > 0:
            return True
    return False


def _cgm_tiene_generacion(gaia: GaiaClient, border_id: int | None, fecha: str) -> bool:
    """True si el reporte CGM (Quoia/ASIC) de esa frontera muestra energía
    real ese día -- fallback para cuando el medidor de nodo no tiene NINGUNA
    medición aunque el borde diga estado OK con reporte reciente (ver AGGE
    Extractora Monterrey 2026-08-10: medidor 0 filas, pero CGM 3.204 kWh
    reales ese mismo día -- mismo patrón ya documentado en memoria como
    "Estado válido sin CGM real", que hasta ahora también bloqueaba la
    sugerencia de pendientes, no solo el reporte de energía)."""
    if border_id is None:
        return False
    try:
        reporte = gaia.get_border_report_status(border_id, fecha)
    except Exception:
        return False
    curva = reporte.get("reported_data_main") if reporte else None
    if not curva:
        return False
    return sum(float(v) for v in curva if v is not None) > 0


def _generacion_real(
    gaia: GaiaClient, node_id: int | None, node_id_resp: int | None, border_id: int | None, fecha: str,
) -> bool:
    """Medidor de nodo primero (más preciso); CGM como respaldo si el nodo
    no tiene nada -- ver _cgm_tiene_generacion."""
    return _nodo_tiene_generacion(gaia, node_id, node_id_resp, fecha) or _cgm_tiene_generacion(gaia, border_id, fecha)


def _frt_a_border_id(borders: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for b in borders:
        frt = b.get("frt_generation") or {}
        code = (frt.get("frt_code") or "").strip().lower()
        if code and frt.get("id") is not None:
            out[code] = frt["id"]
    return out


# Cache de "¿generación real?" por frt_code -- evita repetir ~66 llamadas de
# medición en paralelo en cada GET /proyectos/pendientes (se llama también
# desde confirmar/ignorar). Mismo TTL que _get_dynamic_maps en gaia_client.
_generacion_real_cache: dict[str, bool] | None = None
_generacion_real_cache_ts: float = 0.0
_GENERACION_REAL_CACHE_TTL = 3600  # segundos


def _generacion_real_por_frt(gaia: GaiaClient, borders: list[dict]) -> dict[str, bool]:
    global _generacion_real_cache, _generacion_real_cache_ts
    now = time.monotonic()
    if _generacion_real_cache is not None and (now - _generacion_real_cache_ts) < _GENERACION_REAL_CACHE_TTL:
        return _generacion_real_cache

    dynamic = _get_dynamic_maps(gaia) or {}
    frt_a_nodos = dynamic.get("frt") or {}

    con_reporte = [
        ((b.get("frt_generation") or {}).get("frt_code", "").strip().lower(),
         (b.get("frt_generation") or {}).get("last_report_date"),
         (b.get("frt_generation") or {}).get("id"))
        for b in borders
        if (b.get("frt_generation") or {}).get("last_report_date")
    ]
    resultado: dict[str, bool] = {}
    if con_reporte:
        with ThreadPoolExecutor(max_workers=min(len(con_reporte), 12)) as pool:
            def _check(item):
                code, fecha, border_id = item
                node_p, node_r = frt_a_nodos.get(code, (None, None))
                return code, _generacion_real(gaia, node_p, node_r, border_id, fecha)
            for code, tiene in pool.map(_check, con_reporte):
                resultado[code] = tiene

    _generacion_real_cache = resultado
    _generacion_real_cache_ts = now
    return resultado


# Cache de "¿generación sostenida varios días?" -- más caro que el de 1 día
# (repite la medición por N días), así que solo se calcula para los frt_code
# que YA pasaron el chequeo de 1 día (subconjunto chico). Mismo TTL.
_generacion_multidia_cache: dict[str, bool] | None = None
_generacion_multidia_cache_ts: float = 0.0
_DIAS_GENERACION_SOSTENIDA = 3


def _generacion_real_multidia_por_frt(
    gaia: GaiaClient, frt_codes: list[str], frt_a_border: dict[str, int] | None = None,
) -> dict[str, bool]:
    """Como `_generacion_real_por_frt`, pero exige generación real en los
    últimos `_DIAS_GENERACION_SOSTENIDA` días completos (no el día de hoy,
    que puede estar parcial) -- una sola lectura aislada (prueba/calibración
    de un proyecto recién comisionado) no basta para considerar que ya opera
    de verdad. Caso real 2026-07-10: Garza/La Perdiz/Taurus VIII-X pasaban el
    chequeo de 1 día, pero ese mismo día, revisado después, mostraba
    generación real en cero -- solo se sostuvo un día aislado."""
    global _generacion_multidia_cache, _generacion_multidia_cache_ts
    now = time.monotonic()
    if _generacion_multidia_cache is not None and (now - _generacion_multidia_cache_ts) < _GENERACION_REAL_CACHE_TTL:
        return _generacion_multidia_cache

    dynamic = _get_dynamic_maps(gaia) or {}
    frt_a_nodos = dynamic.get("frt") or {}
    frt_a_border = frt_a_border or {}
    hoy = date.today()
    fechas = [(hoy - timedelta(days=i)).isoformat() for i in range(1, _DIAS_GENERACION_SOSTENIDA + 1)]

    resultado: dict[str, bool] = {}
    codigos = [c for c in frt_codes if c]
    if codigos:
        with ThreadPoolExecutor(max_workers=min(len(codigos), 12)) as pool:
            def _check(code):
                node_p, node_r = frt_a_nodos.get(code, (None, None))
                border_id = frt_a_border.get(code)
                sostenida = all(_generacion_real(gaia, node_p, node_r, border_id, f) for f in fechas)
                return code, sostenida
            for code, tiene in pool.map(_check, codigos):
                resultado[code] = tiene

    _generacion_multidia_cache = resultado
    _generacion_multidia_cache_ts = now
    return resultado


def _candidatos_quoia(fronteras_vinculadas: dict[str, int]) -> list[_Candidato]:
    gaia = GaiaClient()
    if not gaia.enabled:
        return []
    try:
        borders = gaia.get_all_borders()
    except Exception:
        return []
    generacion_real = _generacion_real_por_frt(gaia, borders)
    # Multi-día solo para los que ya pasaron el de 1 día -- subconjunto chico,
    # evita multiplicar por 3 las llamadas de medición para todo el pipeline.
    codigos_1dia = [code for code, tiene in generacion_real.items() if tiene]
    frt_a_border = _frt_a_border_id(borders)
    generacion_multidia = _generacion_real_multidia_por_frt(gaia, codigos_1dia, frt_a_border)

    out = []
    for b in borders:
        nombre = (b.get("name") or "").strip()
        if not nombre or _excluir_por_nombre(nombre):
            continue
        gen = b.get("frt_generation") or {}
        cons = b.get("frt_consumption") or {}
        frt_gen_code = (gen.get("frt_code") or "").strip().lower()
        frt_cons_code = (cons.get("frt_code") or "").strip().lower()

        # Ya vinculado a un proyecto vía fronteras.codigo_frontera -- match
        # directo, no hace falta adivinar por nombre.
        proyecto_id = fronteras_vinculadas.get(frt_gen_code) or fronteras_vinculadas.get(frt_cons_code)

        cap_mw = gen.get("installed_capacity")
        c = _Candidato(
            fuentes={"quoia"},
            nombre_raw=nombre,
            tipo_proyecto="minigranja" if re.match(r"^(mgs|minigranja)\b", nombre, re.IGNORECASE) else (
                "gd" if nombre.upper().startswith("GD ") else None
            ),
            potencia_ac_kw=(float(cap_mw) * 1000) if cap_mw else None,
            proyecto_id=proyecto_id,
        )
        # Exige generación real (eae > 0), no solo que el medidor esté
        # registrado y reportando -- ver _tiene_generacion_real.
        if gen.get("last_report_date") and generacion_real.get(frt_gen_code):
            c.estado_sugerido = "en_operacion"
            c.fase_construccion = "energizado"
            c.generacion_multidia = generacion_multidia.get(frt_gen_code, False)
        c.core = _core(c.nombre_raw)
        out.append(c)
    return out


def _fusionar_por_core(candidatos: list[_Candidato]) -> list[_Candidato]:
    """Combina candidatos de distintas fuentes que refieren al mismo
    proyecto real (mismo `core`), sin pisar campos ya llenados.

    Excepción: `fase_construccion`/`estado_sugerido` -- "energizado"/
    "en_operacion" (evidencia real de Quoia: generación medida, no solo el
    medidor registrado) SIEMPRE gana, sin importar el orden de llegada.
    Bug real encontrado 2026-07-09: Sun Factory siempre trae algún
    fase_construccion (aunque esté desactualizado, ej. "en_construccion"),
    y como sus candidatos suelen llegar primero en la lista, el "no pisar
    si ya tiene valor" dejaba la fase vieja de Sun Factory ganando sobre la
    señal real de Quoia -- proyectos que ya generan de verdad (El Paso
    Norte, Chiriguaná Norte 2, etc.) nunca se sugerían para actualizar."""
    por_core: dict[str, _Candidato] = {}
    for c in candidatos:
        if len(c.core) < 3:
            continue
        existente = por_core.get(c.core)
        if existente is None:
            por_core[c.core] = c
            continue
        existente.fuentes |= c.fuentes
        # "energizado"/"en_operacion" siempre gana; si no, se rellena el
        # hueco como cualquier otro campo (primero que llegue, sin pisar).
        if c.fase_construccion == "energizado":
            existente.fase_construccion = "energizado"
        elif existente.fase_construccion is None and c.fase_construccion is not None:
            existente.fase_construccion = c.fase_construccion
        if c.estado_sugerido == "en_operacion":
            existente.estado_sugerido = "en_operacion"
        elif existente.estado_sugerido is None and c.estado_sugerido is not None:
            existente.estado_sugerido = c.estado_sugerido
        existente.generacion_multidia = existente.generacion_multidia or c.generacion_multidia
        for campo in (
            "municipio", "departamento", "latitud", "longitud", "tipo_proyecto",
            "potencia_ac_kw", "capacidad_instalada_kwp", "sub_project",
            "project_id_solenium", "origina_code", "codigo_tsf",
            "sunfactory_project_id", "proyecto_id",
        ):
            if getattr(existente, campo) is None and getattr(c, campo) is not None:
                setattr(existente, campo, getattr(c, campo))
    return list(por_core.values())


def _reforzar_solo_quoia(candidatos: list[_Candidato]) -> None:
    """Sin corroboración de Sun Factory (el candidato viene solo de
    Quoia), exige generación sostenida varios días, no solo el último
    reportado -- caso real 2026-07-10: Garza/La Perdiz/Taurus VIII-X se
    confirmaron como "en operación" con evidencia de un solo día que resultó
    ser aislada (prueba/calibración), sin que ninguna otra fuente respaldara
    la sugerencia. Muta `candidatos` in-place."""
    for c in candidatos:
        if c.fuentes == {"quoia"} and c.estado_sugerido == "en_operacion" and not c.generacion_multidia:
            c.estado_sugerido = None
            c.fase_construccion = None


def resolver_pendientes() -> list[dict]:
    """Candidatos de Sun Factory y Quoia que no están reflejados en `proyectos`,
    o que sí lo están pero con el estado o la fase desincronizados.

    **Nunca se escribe nada acá**: esto solo propone. Confirmar es una acción de
    una persona (`POST /proyectos/pendientes/{clave}/confirmar`).
    """
    proyectos = list(
        Proyecto.objects.filter(deleted_at__isnull=True).only(
            "id", "nombre_comercial", "estado", "fase_construccion", "origina_code",
            "codigo_tsf", "sunfactory_project_id", "sub_project", "project_id_solenium",
        )
    )

    por_sunfactory_id = {
        p.sunfactory_project_id: p for p in proyectos if p.sunfactory_project_id is not None
    }
    por_origina_code = {(p.origina_code or "").upper(): p for p in proyectos if p.origina_code}
    por_codigo_tsf = {(p.codigo_tsf or "").upper(): p for p in proyectos if p.codigo_tsf}
    por_solenium_id = {p.project_id_solenium: p for p in proyectos if p.project_id_solenium}
    por_core = {}
    for p in proyectos:
        core = _core(p.nombre_comercial)
        if len(core) >= 3:
            por_core.setdefault(core, p)

    fronteras_vinculadas = {
        (codigo or "").lower(): proyecto_id
        for codigo, proyecto_id in Frontera.objects
        .filter(
            proyecto_id__isnull=False,
            codigo_frontera__isnull=False,
            deleted_at__isnull=True,
        )
        .values_list("codigo_frontera", "proyecto_id")
    }
    ignorados = set(
        ProyectoPendienteIgnorado.objects.values_list("clave", flat=True)
    )

    crudos = (
        _candidatos_sunfactory()
        + _candidatos_quoia(fronteras_vinculadas)
    )
    candidatos = _fusionar_por_core(crudos)
    _reforzar_solo_quoia(candidatos)

    pendientes: list[dict] = []
    vistos_proyecto_id: set[int] = set()

    for c in candidatos:
        match = None
        # 1. Match exacto por ID/código.
        if c.sunfactory_project_id is not None:
            match = por_sunfactory_id.get(c.sunfactory_project_id)
        if match is None and c.origina_code:
            match = por_origina_code.get(c.origina_code.upper())
        if match is None and c.codigo_tsf:
            match = por_codigo_tsf.get(c.codigo_tsf.upper())
        if match is None and c.project_id_solenium:
            match = por_solenium_id.get(c.project_id_solenium)
        if match is None and c.proyecto_id:
            match = next((p for p in proyectos if p.id == c.proyecto_id), None)

        confianza = "id"
        # 2. Sin match por ID -- probar nombre normalizado.
        if match is None:
            match = por_core.get(c.core)
            confianza = "nombre" if match else "id"

        clave = f"core:{c.core}"
        if clave in ignorados:
            continue

        if match is not None:
            if match.id in vistos_proyecto_id:
                continue
            necesita_actualizar = (
                (c.estado_sugerido == "en_operacion" and match.estado != "en_operacion")
                or (
                    c.fase_construccion
                    and match.fase_construccion != c.fase_construccion
                    # Nunca sugerir que un proyecto YA "energizado" regrese a
                    # una fase de obra anterior -- mismo bug que el de
                    # sync_tsf_projects: Sun Factory puede seguir trayendo un
                    # status de obra desactualizado para un proyecto que ya
                    # se confirmó operando (caso real 2026-07-09: "Chima
                    # Oriente"/"Chiriguana N1"/"Valencia Oriente 1" ya estaban
                    # en energizado y esto los sugería de vuelta a
                    # en_construccion).
                    and match.fase_construccion != "energizado"
                )
                or (confianza == "nombre")  # vínculo sin confirmar todavía
            )
            if not necesita_actualizar:
                continue
            vistos_proyecto_id.add(match.id)
            pendientes.append({
                "clave": clave,
                "tipo_sugerencia": "actualizar",
                "confianza": confianza,
                "fuentes": sorted(c.fuentes),
                "proyecto_id": match.id,
                "proyecto_nombre_actual": match.nombre_comercial,
                "nombre_sugerido": c.nombre_raw,
                "estado_actual": match.estado,
                "estado_sugerido": c.estado_sugerido,
                "fase_construccion_actual": match.fase_construccion,
                "fase_construccion_sugerida": c.fase_construccion,
                "tipo_proyecto_sugerido": c.tipo_proyecto,
                "municipio": c.municipio,
                "departamento": c.departamento,
                "latitud": c.latitud,
                "longitud": c.longitud,
                "potencia_ac_kw": c.potencia_ac_kw,
                "capacidad_instalada_kwp": c.capacidad_instalada_kwp,
                "sub_project": c.sub_project,
                "project_id_solenium": c.project_id_solenium,
                "origina_code": c.origina_code,
                "codigo_tsf": c.codigo_tsf,
                "sunfactory_project_id": c.sunfactory_project_id,
            })
        else:
            pendientes.append({
                "clave": clave,
                "tipo_sugerencia": "crear",
                "confianza": "sin_match",
                "fuentes": sorted(c.fuentes),
                "proyecto_id": None,
                "proyecto_nombre_actual": None,
                "nombre_sugerido": c.nombre_raw,
                "estado_actual": None,
                "estado_sugerido": c.estado_sugerido or "en_desarrollo",
                "fase_construccion_actual": None,
                "fase_construccion_sugerida": c.fase_construccion,
                "tipo_proyecto_sugerido": c.tipo_proyecto,
                "municipio": c.municipio,
                "departamento": c.departamento,
                "latitud": c.latitud,
                "longitud": c.longitud,
                "potencia_ac_kw": c.potencia_ac_kw,
                "capacidad_instalada_kwp": c.capacidad_instalada_kwp,
                "sub_project": c.sub_project,
                "project_id_solenium": c.project_id_solenium,
                "origina_code": c.origina_code,
                "codigo_tsf": c.codigo_tsf,
                "sunfactory_project_id": c.sunfactory_project_id,
            })

    return pendientes
