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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta

from apps.fronteras.models import Frontera

# Sun Factory usa este listado para el propio edificio de Solenium y para
# proyectos ya dados de baja -- nunca son candidatos reales. Vienen de
# app/services/proyectos_pendientes.py (lineas 44-45); el port conservo el
# filtro que las usa pero no las constantes, asi que listar pendientes
# moria con NameError.
_EXCLUIR_NOMBRES = ("solenium piso",)
_EXCLUIR_PREFIJOS = ("deprecated",)
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import (
    GeneracionDiaria, Proyecto, ProyectoPendienteIgnorado,
)
from apps.proyectos.services.generacion_unergy import DIAS_VENTANA
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


# ── Cuánto hay que preguntarle a Quoia ──────────────────────────────────────
# Antes se medían TODOS los borders con reporte reciente --~145, y cada uno son
# hasta DOS llamadas (medidor de nodo, y el CGM como respaldo)-- y recién
# después se filtraba por nombre y por si la frontera ya estaba vinculada. Se
# medía para botar.
#
# Ahora se filtra primero, y lo que queda se parte en dos:
#
#   · La frontera YA está vinculada a un proyecto → la respuesta está en
#     `generacion_diaria`, que se indexa justo por `proyecto_id`. Cero llamadas.
#   · La frontera no la tenemos → es una candidata de verdad y Quoia es la
#     única fuente. Solo por esas se pregunta.
#
# Medido contra PRODUCCIÓN el 2026-09-15: de las 153 fronteras vivas con código,
# **las 153 están vinculadas a un proyecto**, ninguna suelta. Hoy, entonces, el
# segundo grupo son solo las que Quoia conoce y nosotros todavía no. El reparto
# no depende de esa proporción, pero dice de dónde sale el ahorro.
#
# Esto depende de que `generacion_diaria` cubra a los proyectos que AÚN NO
# están marcados en operación -- que es lo que se arregló el mismo día en
# `generacion_unergy.sincronizar`. Sin eso, este camino responde "no genera"
# para todos y calla la sugerencia en vez de acelerarla.

_DIAS_GENERACION_SOSTENIDA = 3


def _frt_codes(border: dict) -> tuple[str, str]:
    """`(código de generación, código de consumo)` de un border, normalizados."""
    gen = border.get("frt_generation") or {}
    cons = border.get("frt_consumption") or {}
    return ((gen.get("frt_code") or "").strip().lower(),
            (cons.get("frt_code") or "").strip().lower())


def _repartir_borders(
    borders: list[dict], fronteras_vinculadas: dict[str, int],
) -> tuple[list[tuple[str, int]], list[tuple[str, str, int | None]]]:
    """`(propias, ajenas)` entre los borders por los que vale la pena preguntar.

    `propias` son `(frt_code, proyecto_id)`: se responden desde nuestra base.
    `ajenas` son `(frt_code, última fecha reportada, border_id)`: van a Quoia.

    Quedan afuera los que no tienen nombre, los excluidos por nombre
    (`deprecated`, el edificio de Solenium) y los que nunca reportaron.
    """
    propias: list[tuple[str, int]] = []
    ajenas: list[tuple[str, str, int | None]] = []
    for b in borders:
        nombre = (b.get("name") or "").strip()
        if not nombre or _excluir_por_nombre(nombre):
            continue
        gen = b.get("frt_generation") or {}
        fecha = gen.get("last_report_date")
        if not fecha:
            continue
        code_gen, code_cons = _frt_codes(b)
        if not code_gen:
            continue
        proyecto_id = fronteras_vinculadas.get(code_gen) or fronteras_vinculadas.get(code_cons)
        if proyecto_id is not None:
            propias.append((code_gen, proyecto_id))
        else:
            ajenas.append((code_gen, fecha, gen.get("id")))
    return propias, ajenas


def _generacion_desde_la_base(
    propias: list[tuple[str, int]],
) -> tuple[dict[str, bool], dict[str, bool]]:
    """`(generó, generó sostenido)` por frt_code, leyendo `generacion_diaria`.

    Una consulta para todas. Sostenido = los `_DIAS_GENERACION_SOSTENIDA` días
    completos anteriores a hoy, el mismo criterio que se le exigía a Quoia; hoy
    no cuenta porque puede estar parcial.

    El chequeo de un día mira la ventana que el sync llena, no la fecha exacta
    que Quoia reporta como último dato: nuestra tabla es diaria y no tiene por
    qué coincidir al día con el catálogo de un tercero.
    """
    if not propias:
        return {}, {}

    ids = {pid for _, pid in propias}
    hoy = hoy_col()
    completos = [hoy - timedelta(days=i) for i in range(1, _DIAS_GENERACION_SOSTENIDA + 1)]
    desde = min(completos + [hoy - timedelta(days=DIAS_VENTANA)])

    por_proyecto: dict[int, set] = defaultdict(set)
    for pid, fecha in (
        GeneracionDiaria.objects
        .filter(proyecto_id__in=ids, fecha__gte=desde, kwh_real__gt=0)
        .values_list("proyecto_id", "fecha")
    ):
        por_proyecto[pid].add(fecha)

    un_dia: dict[str, bool] = {}
    sostenida: dict[str, bool] = {}
    for code, pid in propias:
        fechas = por_proyecto.get(pid, set())
        un_dia[code] = bool(fechas)
        sostenida[code] = all(d in fechas for d in completos)
    return un_dia, sostenida


# Cache de lo que SÍ hay que preguntarle a Quoia -- ya solo las fronteras que
# no tenemos. Vive en una variable de módulo, así que es por worker de gunicorn
# y se pierde en cada despliegue; con el reparto de arriba eso dejó de importar,
# porque lo que puede llegar a recalcular es un puñado y no el catálogo entero.
#
# **Es POR CÓDIGO, no un diccionario entero con una fecha.** Antes se guardaba
# el resultado completo de la última corrida: si otra la llamaba con menos
# fronteras --el endpoint de diagnóstico pregunta por una sola-- el diccionario
# chico quedaba pisando al grande, y todo lo que no estuviera ahí se leía como
# "no genera". Un falso negativo callado.
_generacion_real_cache: dict[str, tuple[bool, float]] = {}
_GENERACION_REAL_CACHE_TTL = 3600  # segundos


def _vigentes(cache: dict, codigos) -> tuple[dict[str, bool], list]:
    """`(lo que ya sabemos, lo que hay que volver a medir)`."""
    now = time.monotonic()
    sabido: dict[str, bool] = {}
    faltan = []
    for item in codigos:
        code = item[0] if isinstance(item, tuple) else item
        guardado = cache.get(code)
        if guardado is not None and (now - guardado[1]) < _GENERACION_REAL_CACHE_TTL:
            sabido[code] = guardado[0]
        else:
            faltan.append(item)
    return sabido, faltan


def _guardar(cache: dict, resultado: dict[str, bool]) -> None:
    now = time.monotonic()
    for code, valor in resultado.items():
        cache[code] = (valor, now)


def _generacion_real_por_frt(
    gaia: GaiaClient, ajenas: list[tuple[str, str, int | None]],
) -> dict[str, bool]:
    """¿Generó de verdad el día que Quoia reporta como último? Por frt_code."""
    sabido, faltan = _vigentes(_generacion_real_cache, ajenas)
    if not faltan:
        return sabido

    frt_a_nodos = (_get_dynamic_maps(gaia) or {}).get("frt") or {}

    resultado: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(len(faltan), 12)) as pool:
        def _check(item):
            code, fecha, border_id = item
            node_p, node_r = frt_a_nodos.get(code, (None, None))
            return code, _generacion_real(gaia, node_p, node_r, border_id, fecha)
        for code, tiene in pool.map(_check, faltan):
            resultado[code] = tiene

    _guardar(_generacion_real_cache, resultado)
    return {**sabido, **resultado}


# Cache de "¿generación sostenida varios días?" -- más caro que el de 1 día
# (repite la medición por N días), así que solo se calcula para los frt_code
# que YA pasaron el chequeo de 1 día (subconjunto chico). Mismo TTL, y también
# por código, por la misma razón que el de arriba.
_generacion_multidia_cache: dict[str, tuple[bool, float]] = {}


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
    sabido, faltan = _vigentes(_generacion_multidia_cache, [c for c in frt_codes if c])
    if not faltan:
        return sabido

    frt_a_nodos = (_get_dynamic_maps(gaia) or {}).get("frt") or {}
    frt_a_border = frt_a_border or {}
    hoy = hoy_col()
    fechas = [(hoy - timedelta(days=i)).isoformat() for i in range(1, _DIAS_GENERACION_SOSTENIDA + 1)]

    resultado: dict[str, bool] = {}
    with ThreadPoolExecutor(max_workers=min(len(faltan), 12)) as pool:
        def _check(code):
            node_p, node_r = frt_a_nodos.get(code, (None, None))
            border_id = frt_a_border.get(code)
            return code, all(_generacion_real(gaia, node_p, node_r, border_id, f) for f in fechas)
        for code, tiene in pool.map(_check, faltan):
            resultado[code] = tiene

    _guardar(_generacion_multidia_cache, resultado)
    return {**sabido, **resultado}


def _candidatos_quoia(fronteras_vinculadas: dict[str, int]) -> list[_Candidato]:
    gaia = GaiaClient()
    if not gaia.enabled:
        return []
    try:
        borders = gaia.get_all_borders()
    except Exception:
        return []

    propias, ajenas = _repartir_borders(borders, fronteras_vinculadas)

    # Las que ya son nuestras: una consulta a la base, ninguna llamada externa.
    generacion_real, generacion_multidia = _generacion_desde_la_base(propias)

    # Las que no tenemos: acá sí hay que preguntar. Multi-día solo para las que
    # ya pasaron el de 1 día -- subconjunto chico, evita multiplicar por 3.
    if ajenas:
        de_quoia = _generacion_real_por_frt(gaia, ajenas)
        generacion_real.update(de_quoia)
        codigos_1dia = [code for code, tiene in de_quoia.items() if tiene]
        generacion_multidia.update(
            _generacion_real_multidia_por_frt(gaia, codigos_1dia, _frt_a_border_id(borders))
        )

    out = []
    for b in borders:
        nombre = (b.get("name") or "").strip()
        if not nombre or _excluir_por_nombre(nombre):
            continue
        gen = b.get("frt_generation") or {}
        frt_gen_code, frt_cons_code = _frt_codes(b)

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


def _candidatos_unergy() -> list[_Candidato]:
    """Las plantas registradas en la plataforma Unergy original.

    Tercera fuente, despues de Sun Factory y Quoia. Hacia falta porque hay
    plantas que **solo** existen aca: las de terceros a las que les prestamos
    algun servicio. Caso que lo motivo (2026-09-14): "Caracoli" aparecia como
    frontera pendiente en Quoia y nunca como proyecto por sugerir, porque
    ninguna de las dos fuentes la conocia.

    `nombre_topico` es el identificador, y es el mismo valor que guarda
    `Proyecto.sub_project` -- por eso `sub_project` tuvo que entrar antes en la
    cascada de emparejamiento. Sin ese ancla, cada planta de aca habria caido a
    la coincidencia por nombre, que es como se crean los duplicados.

    Un fallo devuelve lista vacia, no levanta: las otras dos fuentes siguen
    sirviendo y la vista no se queda sin pendientes por una API caida.
    """
    from apps.energia.services.comercializacion import (
        fetch_unergy_projects_cacheado,
        unergy_token,
    )

    try:
        token = unergy_token()
    except Exception:
        return []
    if not token:
        return []
    try:
        crudos = fetch_unergy_projects_cacheado(token)
    except Exception:
        return []

    out = []
    for p in crudos:
        topico = (p.get("nombre_topico") or "").strip()
        nombre = (p.get("nombre_proyecto") or p.get("nombre_corto") or "").strip()
        if not topico or not nombre or _excluir_por_nombre(nombre):
            continue
        c = _Candidato(
            fuentes={"unergy"},
            nombre_raw=nombre,
            sub_project=topico,
        )
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
            "origina_code", "codigo_tsf",
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
        # `== {"quoia"}` y no `"quoia" in c.fuentes`: la exigencia es para el
        # candidato que NADIE MAS corrobora. Desde que la API de Unergy es la
        # tercera fuente (2026-09-14), aparecer tambien ahi cuenta como
        # corroboracion y el candidato sale de esta regla.
        if c.fuentes == {"quoia"} and c.estado_sugerido == "en_operacion" and not c.generacion_multidia:
            c.estado_sugerido = None
            c.fase_construccion = None


# Los identificadores que `_actualizar_desde_pendiente` rellena cuando estan
# vacios. Solo estos: la ubicacion tambien se rellena ahi, pero confirmar un
# vinculo es sobre el vinculo -- y ademas esos campos no vienen cargados en el
# queryset, asi que mirarlos costaria una consulta por proyecto.
_IDENTIFICADORES_DEL_VINCULO = (
    "origina_code", "codigo_tsf", "sunfactory_project_id", "sub_project",
)


def _hay_vinculo_por_escribir(candidato, proyecto) -> bool:
    """Si confirmar este candidato dejaria algun identificador nuevo.

    `_actualizar_desde_pendiente` NO pisa lo que ya tiene valor, asi que cuando
    el proyecto los tiene todos, confirmar no cambia nada.
    """
    return any(
        getattr(proyecto, campo, None) is None
        and getattr(candidato, campo, None) is not None
        for campo in _IDENTIFICADORES_DEL_VINCULO
    )


def resolver_pendientes() -> list[dict]:
    """Candidatos de Sun Factory y Quoia que no están reflejados en `proyectos`,
    o que sí lo están pero con el estado o la fase desincronizados.

    **Nunca se escribe nada acá**: esto solo propone. Confirmar es una acción de
    una persona (`POST /proyectos/pendientes/{clave}/confirmar`).
    """
    proyectos = list(
        Proyecto.objects.filter(deleted_at__isnull=True).only(
            "id", "nombre_comercial", "estado", "fase_construccion", "origina_code",
            "codigo_tsf", "sunfactory_project_id", "sub_project",
        )
    )

    por_sunfactory_id = {
        p.sunfactory_project_id: p for p in proyectos if p.sunfactory_project_id is not None
    }
    por_origina_code = {(p.origina_code or "").upper(): p for p in proyectos if p.origina_code}
    por_codigo_tsf = {(p.codigo_tsf or "").upper(): p for p in proyectos if p.codigo_tsf}
    # `sub_project` es como identifica cada planta la API de Unergy (su
    # `nombre_topico`). Faltaba, y sin el un candidato que solo traiga ese
    # identificador cae al ultimo recurso --coincidencia por nombre-- que es
    # exactamente como se crean los duplicados.
    por_sub_project = {
        (p.sub_project or "").lower(): p for p in proyectos if p.sub_project
    }
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
        + _candidatos_unergy()
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
        if match is None and c.sub_project:
            match = por_sub_project.get(c.sub_project.lower())
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
                # Un match por NOMBRE se sugiere para confirmar el vinculo --
                # pero solo si al confirmarlo se va a escribir algo. Si el
                # proyecto ya tiene todos sus identificadores, confirmar no hace
                # nada (`_actualizar_desde_pendiente` solo rellena campos
                # vacios) y la sugerencia vuelve a aparecer en la siguiente
                # consulta: un bucle en el que el boton "Actualizar" no
                # progresa nunca.
                #
                # Caso real (2026-09-15): la API de Unergy trae DOS entradas
                # para la misma planta --"chima" y "chima_oriente"-- y nuestro
                # proyecto ya estaba vinculado a la primera. La segunda
                # emparejaba por nombre, no tenia nada que aportar, y volvia
                # siempre.
                or (confianza == "nombre" and _hay_vinculo_por_escribir(c, match))
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
                # Se conserva en la respuesta para no romper al frontend, pero
                # ninguna fuente lo llena desde que Solenium dejo de serlo.
                "project_id_solenium": None,
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
                # Se conserva en la respuesta para no romper al frontend, pero
                # ninguna fuente lo llena desde que Solenium dejo de serlo.
                "project_id_solenium": None,
                "origina_code": c.origina_code,
                "codigo_tsf": c.codigo_tsf,
                "sunfactory_project_id": c.sunfactory_project_id,
            })

    return pendientes
