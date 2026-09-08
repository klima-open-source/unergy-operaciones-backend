"""
Módulo de cumplimiento contractual de energía.

Endpoint principal: GET /cumplimiento/ppa/{contrato_id}?year=&month=
Consulta la generación real desde la API de Unergy para las plantas
asignadas al contrato vía GESCON (asic_solicitudes) y la cruza con
los compromisos de energía (min/max MWh) del contrato PPA.
"""

import calendar
import logging
import time as _time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, text
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.v1.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db, SessionLocal
from app.models.asic import AsicSolicitud, TipoSolicitudAsicEnum, EstadoSolicitudAsicEnum
from app.utils.gescon_vigencia import resolver_vigencias
from app.services.comercializacion import identificador_monitoreo as _mon_id
from app.models.contratos import PPAContrato, PPACompromisoEnergia, PPATarifa, PPAResponsable
from app.models.cumplimiento import CumplimientoMensual, EstadoCumplimientoEnum
from app.schemas.cumplimiento import (
    CumplimientoMensualOut, CerrarPeriodoRequest, CerrarPeriodoResponse,
    FacturarRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cumplimiento", tags=["Cumplimiento"])


def _get_bolsa_avg(db: Session, year: int, month: int) -> dict:
    """Get average bolsa price for a month from precios_bolsa_diario."""
    row = db.execute(text("""
        SELECT
            AVG(precio_promedio) as precio_promedio,
            MIN(precio_min) as precio_min,
            MAX(precio_max) as precio_max,
            AVG(precio_escasez) as precio_escasez,
            COUNT(*) as dias_con_datos
        FROM precios_bolsa_diario
        WHERE EXTRACT(YEAR FROM fecha) = :year
          AND EXTRACT(MONTH FROM fecha) = :month
          AND precio_promedio IS NOT NULL
    """), {"year": year, "month": month}).fetchone()
    if not row or not row.precio_promedio:
        return {"precio_promedio": None, "precio_min": None, "precio_max": None,
                "precio_escasez": None, "dias_con_datos": 0}
    return {
        "precio_promedio": round(float(row.precio_promedio), 2),
        "precio_min": round(float(row.precio_min), 2) if row.precio_min else None,
        "precio_max": round(float(row.precio_max), 2) if row.precio_max else None,
        "precio_escasez": round(float(row.precio_escasez), 2) if row.precio_escasez else None,
        "dias_con_datos": int(row.dias_con_datos),
    }


# ── Unergy API ────────────────────────────────────────────────────────────────

def _unergy_token() -> str:
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{settings.UNERGY_API_URL}/api/accounts/{settings.UNERGY_ACCOUNT_ID}/",
            json={"login": settings.UNERGY_LOGIN, "password": settings.UNERGY_PASSWORD},
            headers={"User-Agent": "PostmanRuntime/7.50.0"},
        )
        resp.raise_for_status()
        return resp.json()["access"]


_COL_TZ = timezone(timedelta(hours=-5))


def _monthly_mwh_from_records(records: list) -> dict:
    """Calcula los MWh del mes a partir de registros de un contador acumulado.

    Reglas (función pura, testeable):
    - Ignora lecturas con ``generacion`` None: una lectura faltante NO es 0; antes
      ``or 0`` la forzaba a 0 y podía hacer que el mes reportara 0 MWh en vez de
      "sin dato" cuando la lectura de borde venía nula.
    - Suma los deltas positivos entre lecturas consecutivas. Esto es robusto ante
      reinicios de contador (un paso negativo aporta 0 en vez de corromper el
      total) y es EXACTAMENTE igual a (último − primero) cuando el contador es
      monótono creciente, que es el caso normal. Así el cálculo no cambia para los
      meses sanos y solo se corrige el caso anómalo (reinicio / lectura nula).

    Devuelve ``mwh`` (float redondeado a 3) o None si no hay lecturas válidas, y
    el datetime tz-aware (Colombia) de la última lectura válida en ``last_dt``.
    """
    rows = []
    for r in sorted(records, key=lambda r: r.get("time_stamp", "")):
        g = r.get("generacion")
        if g is None:
            continue
        rows.append((r.get("time_stamp", ""), float(g)))

    if not rows:
        return {"mwh": None, "n_used": 0, "last_dt": None}

    total_kwh = 0.0
    for (_, prev), (_, cur) in zip(rows, rows[1:]):
        if cur > prev:
            total_kwh += cur - prev

    last_dt = None
    try:
        last_aware = datetime.fromisoformat(rows[-1][0].replace("Z", "+00:00"))
        # Normalizar a hora Colombia antes de leer el día: si la API entrega el
        # timestamp en UTC ("...Z"), el .day crudo podía rodar al mes siguiente
        # en lecturas cercanas a medianoche (fin de mes).
        last_dt = last_aware.astimezone(_COL_TZ)
    except Exception:
        last_dt = None

    return {"mwh": round(total_kwh / 1000, 3), "n_used": len(rows), "last_dt": last_dt}


def _fetch_month(token: str, sub_project: str, year: int, month: int) -> dict:
    """
    Consulta la generación acumulada de un mes para un sub_project.
    Devuelve MWh del mes = (último acumulado – primero) / 1000.
    Colombia = UTC-5: agrega 5h para obtener el timestamp UTC correcto.
    """
    tz_offset = timedelta(hours=5)
    last_day = calendar.monthrange(year, month)[1]
    start_utc = datetime(year, month, 1, 0, 0, 0) + tz_offset
    end_utc = datetime(year, month, last_day, 23, 59, 59) + tz_offset

    try:
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            resp = client.get(
                f"{settings.UNERGY_API_URL}/api/admin/operations/project_generation/",
                params={
                    "time_stamp__gte": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "time_stamp__lte": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sub_project": sub_project,
                    "limit": "10000",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PostmanRuntime/7.50.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            records = data if isinstance(data, list) else data.get("results", [])
    except Exception as exc:
        logger.warning("API error sub_project=%s %d-%02d: %s", sub_project, year, month, exc)
        return {"mwh": None, "n_records": 0, "ultimo_dia": None}

    if not records:
        return {"mwh": None, "n_records": 0, "ultimo_dia": None}

    calc = _monthly_mwh_from_records(records)
    ultimo_dia = calc["last_dt"].day if calc["last_dt"] is not None else None

    return {
        "mwh": calc["mwh"],
        "n_records": len(records),
        "ultimo_dia": ultimo_dia,
    }


def _fetch_recent_avg(token: str, sub_project: str, n_days: int = 15) -> dict:
    """
    Promedio diario de generación en los últimos n_days días con datos reales.
    Consulta los 60 días previos a hoy para encontrar días con producción > 0.
    Usa para proyectar meses futuros donde no hay datos reales.
    """
    now_col = datetime.now(timezone.utc) - timedelta(hours=5)
    start_col = now_col - timedelta(days=60)
    tz_offset = timedelta(hours=5)
    start_utc = start_col + tz_offset
    end_utc = now_col + tz_offset

    try:
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            resp = client.get(
                f"{settings.UNERGY_API_URL}/api/admin/operations/project_generation/",
                params={
                    "time_stamp__gte": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "time_stamp__lte": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sub_project": sub_project,
                    "limit": "10000",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PostmanRuntime/7.50.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            records = data if isinstance(data, list) else data.get("results", [])
    except Exception as exc:
        logger.warning("API error recent_avg sub_project=%s: %s", sub_project, exc)
        return {"avg_daily_mwh": None, "n_days_used": 0, "last_data_date": None}

    if not records:
        return {"avg_daily_mwh": None, "n_days_used": 0, "last_data_date": None}

    by_day: dict = defaultdict(list)
    for r in records:
        ts_str = r.get("time_stamp", "")
        gen_val = r.get("generacion")
        if not ts_str or gen_val is None:
            continue
        try:
            dt_aware = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            dt_col = dt_aware.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
            by_day[dt_col.date()].append(float(gen_val))
        except Exception:
            continue

    daily_gen = []
    for day, vals in by_day.items():
        delta_mwh = (max(vals) - min(vals)) / 1000
        if delta_mwh > 0:
            daily_gen.append((day, delta_mwh))

    if not daily_gen:
        return {"avg_daily_mwh": None, "n_days_used": 0, "last_data_date": None}

    daily_gen.sort(key=lambda x: x[0])
    recent = daily_gen[-n_days:]
    avg = round(sum(v for _, v in recent) / len(recent), 3)
    return {
        "avg_daily_mwh": avg,
        "n_days_used": len(recent),
        "last_data_date": recent[-1][0].isoformat(),
    }


def _sumar_deltas_en_rango(records: list, d_from_utc: datetime, d_to_utc: datetime) -> Optional[float]:
    """Suma la generación REAL entre lecturas consecutivas cuyo timestamp cae en
    [d_from_utc, d_to_utc] (ambos naive-UTC, igual que `time_stamp` de la API).

    Usa la última lectura ANTERIOR al rango como base del primer delta, para no
    perder la generación de las primeras horas del día de inicio (si no hubiera
    "antes", ese primer intervalo no tendría con qué compararse). Ignora deltas
    negativos (reinicio de contador), mismo criterio que `_monthly_mwh_from_records`.

    Es el mismo método que usa la vista "Generación solar" al filtrar por rango
    (`_compute_deltas` en monitoreo.py) — sumar días reales, no repartir el
    total del mes por fracción de días.
    """
    rows = []
    for r in records:
        ts_raw = r.get("time_stamp", "")
        gen = r.get("generacion")
        if not ts_raw or gen is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        rows.append((ts, float(gen)))
    rows.sort(key=lambda x: x[0])

    before = [x for x in rows if x[0] < d_from_utc]
    period = [x for x in rows if d_from_utc <= x[0] <= d_to_utc]
    if not period:
        return None

    working = ([before[-1]] if before else []) + period
    total_kwh = 0.0
    for (_, prev), (_, cur) in zip(working, working[1:]):
        if cur > prev:
            total_kwh += cur - prev
    return round(total_kwh / 1000, 3)


def _fetch_range(token: str, sub_project: str, start: date, end: date) -> dict:
    """
    Generación REAL de un sub_project entre `start` y `end` (ambos inclusive),
    sumando los deltas de cada lectura — NO reparte el total del mes por
    fracción de días. Se usa cuando un registro GESCON solo estuvo vigente
    PARTE del mes (relevo, arranque o terminación a mitad de mes): el total
    mensual prorrateado por días no coincide con la generación real de esos
    días específicos porque la generación diaria no es pareja (clima, etc.).

    Colchón de 2 días antes de `start` para poder calcular el delta del primer
    punto dentro del rango (sin una lectura "antes" no hay con qué comparar el
    primer dato del día de inicio).
    """
    tz_offset = timedelta(hours=5)  # Colombia = UTC-5
    d_from = datetime(start.year, start.month, start.day, 0, 0, 0)
    d_to = datetime(end.year, end.month, end.day, 23, 59, 59)
    fetch_from_utc = (d_from - timedelta(days=2)) + tz_offset
    d_from_utc = d_from + tz_offset
    d_to_utc = d_to + tz_offset

    try:
        with httpx.Client(timeout=90, follow_redirects=True) as client:
            resp = client.get(
                f"{settings.UNERGY_API_URL}/api/admin/operations/project_generation/",
                params={
                    "time_stamp__gte": fetch_from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "time_stamp__lte": d_to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sub_project": sub_project,
                    "limit": "10000",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PostmanRuntime/7.50.0",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            records = data if isinstance(data, list) else data.get("results", [])
    except Exception as exc:
        logger.warning("API error rango sub_project=%s %s..%s: %s", sub_project, start, end, exc)
        return {"mwh": None, "n_records": 0}

    if not records:
        return {"mwh": None, "n_records": 0}

    mwh = _sumar_deltas_en_rango(records, d_from_utc, d_to_utc)
    return {"mwh": mwh, "n_records": len(records)}


# ── Energía de un registro por su vigencia (fuente única) ─────────────────────

def _vigencia_window(fecha_inicio, fecha_fin, first_day: date, last_day: date):
    """Ventana efectiva [eff_start, eff_end] (ambos inclusive) de un registro
    dentro del período [first_day, last_day], recortada a esos límites."""
    eff_start = max(first_day, fecha_inicio) if fecha_inicio else first_day
    eff_end = min(last_day, fecha_fin) if fecha_fin else last_day
    return eff_start, eff_end


def _gen_vigencia_mwh(
    eff_start: date,
    eff_end: date,
    dias_periodo: int,
    gen_periodo_completo: Optional[float],
    gen_rango: Optional[float],
) -> Optional[float]:
    """Generación REAL atribuible a un registro vigente [eff_start, eff_end] dentro
    de un período de `dias_periodo` días (típicamente el mes, o el mes hasta el
    corte para el mes en curso).

    - Vigencia que cubre TODO el período → `gen_periodo_completo` (ya es la
      generación real de esos días; no hace falta pedir el rango aparte).
    - Vigencia PARCIAL → `gen_rango` (suma real de esos días exactos, vía
      `_fetch_range`/`_sumar_deltas_en_rango`) — NUNCA el total del período
      prorrateado por fracción de días: la generación diaria no es pareja
      (clima, etc.), así que `total × dias/dias_mes` (regla de tres) ≠ suma real.

    Devuelve None si no hay días activos o falta el dato correspondiente.

    Fuente ÚNICA de verdad de energía para las tres vistas — Estrategia
    (/simulador), Matriz (/anual-matriz) y Energía transada (/energia-transada):
    todas deben dar el mismo número para la misma planta/mes.
    """
    dias_activos = max(0, (eff_end - eff_start).days + 1)
    if dias_activos <= 0:
        return None
    if dias_activos >= dias_periodo:
        return gen_periodo_completo
    return gen_rango


# ── Contratos vigentes ────────────────────────────────────────────────────────

INCLUIR_TODOS_DESC = (
    "Incluye también los contratos cuya empresa responsable está marcada como no "
    "relevante (incluir_en_cumplimiento=false). Por defecto se omiten en todas las "
    "vistas de /mem/cumplimiento."
)


def _responsable_payload(contrato) -> dict:
    """Empresa responsable del PPA, aplanada para las filas de las vistas."""
    r = contrato.responsable
    return {
        "responsable_id": contrato.responsable_id,
        "responsable": r.nombre if r else None,
        "responsable_relevante": r.incluir_en_cumplimiento if r else True,
    }


def _ids_responsables_ocultos(db: Session) -> set:
    """Responsables marcados como NO relevantes: sus contratos los gestiona un
    tercero y no deben aparecer en las vistas de /mem/cumplimiento."""
    return {
        r.id for r in db.query(PPAResponsable)
        .filter(PPAResponsable.incluir_en_cumplimiento.is_(False)).all()
    }


def _filtro_responsable_relevante(db: Session):
    """Cláusula SQL reusable: deja pasar los contratos sin responsable y los de un
    responsable relevante. Devuelve None si no hay ninguno oculto (no filtra)."""
    ocultos = _ids_responsables_ocultos(db)
    if not ocultos:
        return None
    return or_(
        PPAContrato.responsable_id.is_(None),
        PPAContrato.responsable_id.notin_(ocultos),
    )


def _contratos_vigentes(db: Session, year: int, month: int | None = None,
                        solo_relevantes: bool = True) -> list:
    """
    PPA contracts active during the given period, excluding soft-deleted.
    month=None → any month in the year.

    `solo_relevantes` (default True) además descarta los contratos cuya empresa
    responsable está marcada con incluir_en_cumplimiento=False. Es el default
    porque TODAS las vistas de /mem/cumplimiento los ocultan; los endpoints
    exponen `incluir_todos` para verlos. Contrato SIN responsable = se incluye:
    nada se esconde por omisión, solo por marca explícita.

    Se pasa False a propósito en /descubrimientos y /cerrar-periodo: no son vistas
    de esa página y cerrar-periodo además PERSISTE registros mensuales — dejar
    contratos fuera del cierre cambiaría datos históricos, no solo lo que se ve.
    """
    if month:
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
    else:
        first_day = date(year, 1, 1)
        last_day = date(year, 12, 31)
    q = (
        db.query(PPAContrato)
        # el responsable se lee en las filas de la matriz: precargarlo evita N+1
        .options(selectinload(PPAContrato.responsable))
        .filter(
            PPAContrato.deleted_at.is_(None),
            or_(PPAContrato.fecha_inicio.is_(None), PPAContrato.fecha_inicio <= last_day),
            or_(PPAContrato.fecha_fin.is_(None), PPAContrato.fecha_fin >= first_day),
        )
    )
    if solo_relevantes:
        clausula = _filtro_responsable_relevante(db)
        if clausula is not None:
            q = q.filter(clausula)
    return q.order_by(PPAContrato.nombre_interno.nullslast(), PPAContrato.id).all()


def _contrato_vigente_en_mes(contrato, year: int, month: int) -> bool:
    """True si el contrato está vigente en (year, month) según fecha_inicio/fecha_fin.

    Un compromiso del mes M solo cuenta si:
      (fecha_inicio IS NULL OR fecha_inicio <= último día de M) AND
      (fecha_fin    IS NULL OR fecha_fin    >= primer día de M).
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return (
        (contrato.fecha_inicio is None or contrato.fecha_inicio <= last_day)
        and (contrato.fecha_fin is None or contrato.fecha_fin >= first_day)
    )


# ── GESCON ────────────────────────────────────────────────────────────────────

class _AsicVigenciaRecortada:
    """Envoltura de solo lectura sobre un AsicSolicitud que acota su `fecha_fin`
    efectiva sin tocar el registro real.

    Necesaria porque el objeto ORM está en el identity map de la sesión: la
    misma instancia se reutiliza en las 12 llamadas por mes de
    `_anual_meses_para_contrato` (una por mes), así que mutar `.fecha_fin`
    directamente contaminaría el cálculo de los demás meses. Reenvía cualquier
    otro atributo (proyecto, porcentaje_despacho, fecha_inicio, etc.) al
    original vía __getattr__.
    """
    __slots__ = ("_r", "fecha_fin")

    def __init__(self, r: AsicSolicitud, fecha_fin: date):
        self._r = r
        self.fecha_fin = fecha_fin

    def __getattr__(self, name):
        return getattr(self._r, name)


def _resolve_gescon(db: Session, contrato_interno: str, year: int, month: int) -> list:
    """
    Devuelve los registros ASIC vigentes para el contrato en el mes dado,
    reconstruyendo la vigencia HISTÓRICA (no la versión más reciente global).

    Procesa cronológicamente por `fecha_inicio` (cuándo tomó efecto cada
    registro, no cuándo se radicó) y, para el mes consultado, solo reproduce
    los eventos que ya habían tomado efecto (fecha_inicio <= último día del
    mes). Así un mes anterior a una modificación ve la versión que estaba
    vigente en ese momento, no la que la reemplazó después.

    Ej.: Vallenata en Terpel 1 tiene un registro al 100% vigente desde
    2024-07-04 y una modificación al 50% vigente desde 2026-02-12 (radicada
    2026-01-30). Para enero-2026 solo el registro del 100% ya había tomado
    efecto → se usa ese. Para febrero en adelante, ya tomó efecto también la
    modificación → se usa el 50%. Antes se ordenaba/filtraba por
    fecha_solicitud, así que la modificación (radicada en enero, antes de
    tomar efecto) desplazaba al registro viejo también en enero, y luego el
    filtro final de fechas la descartaba a ella igual → el mes quedaba sin
    ninguna versión.

    Por cada codigo_sic_contrato, entre los eventos ya vigentes:
    - Si un registro introduce una PLANTA DISTINTA (proyecto_id nuevo) y
      reemplaza_anterior=True, reemplaza a la(s) planta(s) previas en ese SIC.
      Si el relevo ocurre A MITAD DE MES, la planta saliente NO desaparece:
      se devuelve también, con una fecha_fin EFECTIVA recortada al día
      anterior al del relevo (aunque su fecha_fin real diga otra cosa, por no
      haber una terminación explícita que la estampe) — así el prorrateo por
      días de `_anual_meses_para_contrato` cuenta a cada una solo sus días
      reales, sin solaparse ni sobrecontar. Caso real: SIC 89115 (Terpel 2,
      feb-2026): Baraya (registro, desde 5-feb) es reemplazada por Yurbaqua
      (desde 26-feb) → Baraya debe contar del 5 al 25 (21 días), Yurbaqua del
      26 al 28 (3 días); antes Baraya desaparecía por completo.
    - Si reemplaza_anterior=False, la nueva planta coexiste con las existentes.
    - Terminaciones eliminan la planta indicada (en la práctica siempre con
      proyecto_id=NULL; su fecha_fin real ya viene estampada en el registro/
      modificación del mismo SIC por `_auto_terminate`, así que el filtro
      final de fechas ya la excluye — no requiere recorte aquí).
    - Modificaciones a una MISMA planta ya activa (mismo proyecto_id) solo
      actualizan sus datos (no se considera relevo, no se recorta nada).
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    # Traemos TODAS las versiones del contrato (pasadas y futuras): la vigencia
    # de cada mes se resuelve abajo en memoria, no en este filtro.
    records = (
        db.query(AsicSolicitud)
        .options(joinedload(AsicSolicitud.proyecto))
        .filter(
            AsicSolicitud.contrato_interno == contrato_interno,
            AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
            AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.desistimiento,
        )
        .order_by(
            AsicSolicitud.fecha_inicio.asc().nullsfirst(),
            AsicSolicitud.fecha_solicitud.asc().nullsfirst(),
            AsicSolicitud.created_at.asc(),
        )
        .all()
    )

    # Núcleo compartido con GET /asic y /alertas (app/utils/gescon_vigencia.py):
    # el walk por SIC vive allá; aquí solo se interpreta el resultado para la
    # vista mensual. hasta=last_day reproduce la vista histórica: eventos que
    # aún no tomaban efecto no desplazan la versión vigente del mes.
    vigencias = resolver_vigencias(records, hasta=last_day)

    result = []
    for r in records:
        v = vigencias[r.id]
        if not v.procesado:
            continue
        if r.tipo_solicitud == TipoSolicitudAsicEnum.terminacion:
            continue
        if v.vigente:
            result.append(r)
        elif v.saliente_por_relevo:
            # Planta relevada a mitad de mes: se conserva con su fecha_fin
            # EFECTIVA recortada para el prorrateo por días (ver docstring).
            result.append(_AsicVigenciaRecortada(r, v.fecha_fin_efectiva))
        # Superseded en sitio (misma planta): la versión nueva la representa.

    return [
        r for r in result
        if (r.fecha_fin is None or r.fecha_fin >= first_day)
        and (r.fecha_inicio is None or r.fecha_inicio <= last_day)
    ]


# Código SIC de Unergy actuando como comercializador. Confirmado contra datos
# de producción (catálogo de codigo_sic_comprador): el literal es "UNGC".
UNGC_COMERCIALIZADOR = "UNGC"


def _clasificar_remanente_bolsa(db: Session, proyecto_id: int, first_day: date, last_day: date):
    """Clasifica una planta del remanente (sin contrato PPA) en su piscina de bolsa.

    Paso POSTERIOR a la lógica de contratos: solo se aplica a plantas que ya
    quedaron SIN contrato PPA asignado vía GESCON. Mira asic_solicitudes el
    registro vigente en el período (publicado, fecha_inicio <= last_day AND
    (fecha_fin IS NULL OR fecha_fin >= first_day), excluye terminación):
      - Tiene código SIC vigente con codigo_sic_comprador == 'UNGC'
        → 'comercializador' (bolsa con comercializador Unergy).
      - No tiene código SIC vigente con comprador UNGC en esas fechas
        → 'libre' (libre en bolsa, generador).

    Retorna (piscina, asic_vigente|None). El asic se devuelve para diagnóstico/validación.
    """
    asic = (
        db.query(AsicSolicitud)
        .filter(
            AsicSolicitud.proyecto_id == proyecto_id,
            AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
            AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.desistimiento,
            AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.terminacion,
            AsicSolicitud.codigo_sic_contrato.isnot(None),
            AsicSolicitud.codigo_sic_comprador == UNGC_COMERCIALIZADOR,
            or_(AsicSolicitud.fecha_inicio.is_(None), AsicSolicitud.fecha_inicio <= last_day),
            or_(AsicSolicitud.fecha_fin.is_(None), AsicSolicitud.fecha_fin >= first_day),
        )
        .order_by(AsicSolicitud.fecha_inicio.desc().nullslast())
        .first()
    )
    if asic is not None:
        return "comercializador", asic
    return "libre", None


def _fin_efectivo_asic(db: Session, asic: AsicSolicitud, last_day: date) -> date | None:
    """Fin EFECTIVO de la ventana de `asic`, recortado por supersesiones/relevos
    en su SIC (vista histórica al mes consultado, mismo criterio que la piscina
    b — caso La Reserva). Fallback: la fecha_fin cruda del registro."""
    universo = (
        db.query(AsicSolicitud)
        .filter(
            AsicSolicitud.codigo_sic_contrato == asic.codigo_sic_contrato,
            AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
            AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.desistimiento,
        )
        .order_by(
            AsicSolicitud.fecha_inicio.asc().nullsfirst(),
            AsicSolicitud.fecha_solicitud.asc().nullsfirst(),
            AsicSolicitud.created_at.asc(),
        )
        .all()
    )
    v = resolver_vigencias(universo, hasta=last_day).get(asic.id)
    return v.fecha_fin_efectiva if v else asic.fecha_fin


# ── Historial intra-mes ───────────────────────────────────────────────────────
# El tab Proyectos muestra el HISTORIAL del mes, no una foto: una planta que
# cambió de modalidad a mitad de mes aparece en todas las piscinas por las que
# pasó, cada una con su ventana de días. Estos helpers son aritmética de fechas
# pura (sin BD) para poder probarlos sueltos.

def _fecha_corte(year: int, month: int, hoy: date | None = None) -> date:
    """Fecha contra la que se decide si una ventana sigue vigente.

    Mes en curso → HOY: una planta cuyo contrato termina el 30 sigue vigente el
    26. Mes pasado → último día del mes (foto de cierre). Mes futuro → último
    día del mes (proyección).
    """
    hoy = hoy or date.today()
    if (year, month) == (hoy.year, hoy.month):
        return hoy
    return date(year, month, calendar.monthrange(year, month)[1])


def _recortar(ini: date | None, fin: date | None, lo: date, hi: date):
    """Intersección de [ini, fin] con [lo, hi]. Bordes nulos = abiertos.
    Devuelve None si no se tocan."""
    a = max(ini, lo) if ini else lo
    b = min(fin, hi) if fin else hi
    return (a, b) if a <= b else None


def _restar_intervalos(base, ocupados: list) -> list:
    """Días de `base` (par (ini, fin)) que NO cubre ninguno de `ocupados`.

    Es lo que convierte la bolsa de "plantas sin contrato" a "días sin
    contrato": el residuo de una planta liberada el 23-jul es [24-jul, 31-jul].
    """
    if base is None:
        return []
    libres = [base]
    for oi, of in sorted(ocupados):
        nuevos = []
        for li, lf in libres:
            if of < li or oi > lf:  # sin solape: el tramo sobrevive entero
                nuevos.append((li, lf))
                continue
            if li < oi:
                nuevos.append((li, oi - timedelta(days=1)))
            if lf > of:
                nuevos.append((of + timedelta(days=1), lf))
        libres = nuevos
    return libres


def _estado_segmento(ini: date, fin: date, corte: date) -> str:
    """'terminado' (se acabó antes del corte) | 'futuro' (aún no empieza) |
    'vigente'. Los contadores de la vista solo cuentan 'vigente'."""
    if fin < corte:
        return "terminado"
    if ini > corte:
        return "futuro"
    return "vigente"


def _con_segmento(entry: dict, ini: date | None, fin: date | None,
                  first_day: date, last_day: date, corte: date) -> dict:
    """Añade a una fila de planta su ventana recortada al mes y su estado."""
    seg = _recortar(ini, fin, first_day, last_day) or (first_day, last_day)
    entry["segmento_inicio"] = seg[0].isoformat()
    entry["segmento_fin"] = seg[1].isoformat()
    entry["estado"] = _estado_segmento(seg[0], seg[1], corte)
    return entry


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/ppa")
def list_ppa(
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Lista todos los contratos PPA para el selector."""
    q = (
        db.query(PPAContrato)
        .options(selectinload(PPAContrato.responsable))
        .filter(PPAContrato.deleted_at.is_(None))
    )
    if not incluir_todos:
        clausula = _filtro_responsable_relevante(db)
        if clausula is not None:
            q = q.filter(clausula)
    rows = q.order_by(PPAContrato.nombre_interno.nullslast(), PPAContrato.id).all()
    return [
        {
            "id": r.id,
            "nombre_interno": r.nombre_interno,
            "numero_codigo_contrato": r.numero_codigo_contrato,
            "comprador_nombre": r.comprador_nombre,
            "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
            "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
            **_responsable_payload(r),
        }
        for r in rows
    ]


@router.get("/ppa/resumen")
def get_resumen(
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Resumen de cumplimiento de todos los contratos PPA para un período.
    Deduplica sub_projects y hace una sola llamada a la API por planta.
    """
    today = date.today()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 1. Contratos y compromisos ────────────────────────────────────────────
    contratos = _contratos_vigentes(db, year, month, solo_relevantes=not incluir_todos)
    compromisos_map = {
        c.contrato_id: c
        for c in db.query(PPACompromisoEnergia).filter(
            PPACompromisoEnergia.año == year,
            PPACompromisoEnergia.mes == month,
        ).all()
    }

    # ── 2. GESCON por contrato ────────────────────────────────────────────────
    contrato_assignments: dict[int, list] = {}
    for c in contratos:
        if c.numero_codigo_contrato:
            contrato_assignments[c.id] = _resolve_gescon(db, c.numero_codigo_contrato, year, month)
        else:
            contrato_assignments[c.id] = []

    # ── 3. Sub-projects únicos ────────────────────────────────────────────────
    sp_set: set[str] = set()
    for assignments in contrato_assignments.values():
        for asic in assignments:
            if asic.proyecto and asic.proyecto.sub_project:
                sp_set.add(asic.proyecto.sub_project)

    # ── 4. Generación en paralelo (un solo fetch por planta) ──────────────────
    gen_cache: dict[str, dict] = {}
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in resumen: %s", exc)
            token = None

        if token:
            sp_list = list(sp_set)

            def _fetch_sp(sp: str) -> tuple:
                if es_mes_futuro:
                    recent = _fetch_recent_avg(token, sp)
                    avg = recent["avg_daily_mwh"]
                    mwh = round(avg * total_dias, 3) if avg is not None else None
                    return sp, {"mwh": mwh, "n_records": recent["n_days_used"], "ultimo_dia": None}
                return sp, _fetch_month(token, sp, year, month)

            max_workers = min(len(sp_list), 10)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for sp, result in pool.map(_fetch_sp, sp_list):
                    gen_cache[sp] = result

    # ── 5. Cálculo por contrato ────────────────────────────────────────────────
    contratos_result = []
    total_min = 0.0
    total_max = 0.0
    total_gen = 0.0
    total_proy = 0.0
    has_any_compromisos = False

    for c in contratos:
        assignments = contrato_assignments[c.id]
        compromiso = compromisos_map.get(c.id)

        gen_total_c = 0.0
        bolsa_dup_c = 0.0
        ur_c = 0.0
        plantas_sin_datos: list[str] = []
        dias_datos: list[int] = []
        n_duplicados = 0
        n_uso_recurso = 0

        for asic in assignments:
            proyecto = asic.proyecto
            nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
            pct = float(asic.porcentaje_despacho or 0)
            is_dup = bool(asic.es_duplicado)
            is_ur = bool(getattr(asic, "uso_del_recurso", False))
            if proyecto and proyecto.sub_project:
                gd = gen_cache.get(proyecto.sub_project, {"mwh": None, "ultimo_dia": None})
                gp = gd["mwh"]
                if gp is not None:
                    mwh_contrato = gp * pct
                    # El suministro al contrato cuenta para el cumplimiento sin importar
                    # el origen (real o compra en bolsa). El duplicado además se registra
                    # en bolsa_dup_c como sub-cifra informativa (cuánto proviene de bolsa).
                    gen_total_c += mwh_contrato
                    if is_dup:
                        bolsa_dup_c += mwh_contrato
                        n_duplicados += 1
                    if is_ur:
                        # Uso del recurso: cuenta como suministro normal del contrato;
                        # la sub-cifra estima lo que se le pagará al cliente a bolsa.
                        ur_c += mwh_contrato
                        n_uso_recurso += 1
                    if gd.get("ultimo_dia") is not None:
                        dias_datos.append(gd["ultimo_dia"])
                else:
                    plantas_sin_datos.append(nombre)
            else:
                plantas_sin_datos.append(nombre)

        gen_total_c = round(gen_total_c, 3)
        bolsa_dup_c = round(bolsa_dup_c, 3)
        ur_c = round(ur_c, 3)
        gen_proy_c = (
            round(gen_total_c * total_dias / dia_actual, 3)
            if es_mes_actual and dia_actual > 0 and gen_total_c > 0
            else gen_total_c
        )

        min_mwh: Optional[float] = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None
        max_mwh: Optional[float] = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else None

        val_b = gen_proy_c if (es_mes_actual or es_mes_futuro) else gen_total_c

        if min_mwh is not None or max_mwh is not None:
            has_any_compromisos = True
            effective_max = max_mwh if max_mwh is not None else float('inf')
            effective_min = min_mwh if min_mwh is not None else 0.0
            if val_b < effective_min:
                estado_c = "deficit"
            elif val_b > effective_max:
                estado_c = "excedente"
            else:
                estado_c = "ok"
            compras_c = round(max(0.0, effective_min - val_b), 3)
            excedentes_c = round(max(0.0, val_b - effective_max), 3) if max_mwh is not None else 0.0
            total_min += effective_min
            total_max += max_mwh if max_mwh is not None else 0.0
        else:
            estado_c = "sin_compromisos"
            compras_c = None
            excedentes_c = None

        total_gen += gen_total_c
        total_proy += gen_proy_c

        contratos_result.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "energia_minima_mwh": min_mwh,
            "energia_maxima_mwh": max_mwh,
            "gen_total_mwh": gen_total_c,
            "gen_proyectada_mwh": gen_proy_c,
            "estado": estado_c,
            "compras_bolsa_mwh": compras_c,
            "excedentes_bolsa_mwh": excedentes_c,
            "exposicion_bolsa_duplicados_mwh": bolsa_dup_c if bolsa_dup_c > 0 else None,
            "uso_recurso_mwh": ur_c if ur_c > 0 else None,
            "n_plantas_activas": len(assignments),
            "n_duplicados": n_duplicados,
            "n_uso_recurso": n_uso_recurso,
            "plantas_sin_datos": plantas_sin_datos,
            "dia_min_datos": min(dias_datos) if dias_datos else None,
            # Indicador de cumplimiento de plantas: registradas (numerador) vs esperadas (denominador).
            "plantas_registradas": len(assignments),
            "plantas_esperadas": int(compromiso.cantidad_proyectos) if compromiso and compromiso.cantidad_proyectos is not None else None,
        })

    # ── 6. Totales agregados ──────────────────────────────────────────────────
    total_gen = round(total_gen, 3)
    total_proy = round(total_proy, 3)
    val_total = total_proy if (es_mes_actual or es_mes_futuro) else total_gen

    if has_any_compromisos and (total_min > 0 or total_max > 0):
        if total_min > 0 and val_total < total_min:
            total_estado = "deficit"
        elif total_max > 0 and val_total > total_max:
            total_estado = "excedente"
        else:
            total_estado = "ok"
        total_compras = round(max(0.0, total_min - val_total), 3) if total_min > 0 else 0.0
        total_excedentes = round(max(0.0, val_total - total_max), 3) if total_max > 0 else 0.0
    else:
        total_estado = "sin_compromisos"
        total_compras = None
        total_excedentes = None

    dias_min_list = [c["dia_min_datos"] for c in contratos_result if c["dia_min_datos"] is not None]

    # ── Valoración COP con precios de bolsa ──────────────────
    bolsa = _get_bolsa_avg(db, year, month)
    precio_bolsa = bolsa["precio_promedio"]

    valoracion_total = None
    if precio_bolsa is not None and (total_compras or total_excedentes):
        valoracion_total = {
            "precio_bolsa_avg_cop_kwh": precio_bolsa,
            "dias_con_precios": bolsa["dias_con_datos"],
            "compras_bolsa_cop": round(total_compras * 1000 * precio_bolsa, 0) if total_compras else 0,
            "excedentes_bolsa_cop": round(total_excedentes * 1000 * precio_bolsa, 0) if total_excedentes else 0,
        }

    # Add COP to each contract row
    if precio_bolsa is not None:
        for c in contratos_result:
            if c["compras_bolsa_mwh"] is not None and c["compras_bolsa_mwh"] > 0:
                c["compras_bolsa_cop"] = round(c["compras_bolsa_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["compras_bolsa_cop"] = None
            if c["excedentes_bolsa_mwh"] is not None and c["excedentes_bolsa_mwh"] > 0:
                c["excedentes_bolsa_cop"] = round(c["excedentes_bolsa_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["excedentes_bolsa_cop"] = None
            if c.get("uso_recurso_mwh"):
                # Costo interno estimado: lo que Unergy le pagará al cliente a precio
                # bolsa (el pago real es manual en Liquidaciones).
                c["uso_recurso_cop"] = round(c["uso_recurso_mwh"] * 1000 * precio_bolsa, 0)
            else:
                c["uso_recurso_cop"] = None

    return {
        "periodo": {
            "year": year,
            "month": month,
            "dia_actual": dia_actual,
            "dias_mes": total_dias,
            "es_mes_actual": es_mes_actual,
            "es_mes_futuro": es_mes_futuro,
            "tipo_datos": "proyeccion_historica" if es_mes_futuro else ("proyeccion_lineal" if es_mes_actual else "real"),
            "dia_min_datos": min(dias_min_list) if dias_min_list else None,
            "dia_max_datos": max(dias_min_list) if dias_min_list else None,
        },
        "totales": {
            "energia_minima_mwh": round(total_min, 3) if has_any_compromisos else None,
            "energia_maxima_mwh": round(total_max, 3) if has_any_compromisos else None,
            "gen_total_mwh": total_gen,
            "gen_proyectada_mwh": total_proy,
            "estado": total_estado,
            "compras_bolsa_mwh": total_compras,
            "excedentes_bolsa_mwh": total_excedentes,
        },
        "valoracion_bolsa": valoracion_total,
        "contratos": contratos_result,
    }


@router.get("/ppa/resumen-anual")
def get_resumen_anual(
    year: int = Query(..., ge=2020, le=2050),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Annual commitment totals per contract (DB only, no Unergy API)."""
    contratos = _contratos_vigentes(db, year, solo_relevantes=not incluir_todos)
    compromisos = (
        db.query(PPACompromisoEnergia)
        .filter(PPACompromisoEnergia.año == year)
        .all()
    )
    comp_by_c: dict = defaultdict(list)
    for c in compromisos:
        comp_by_c[c.contrato_id].append(c)

    result = []
    for c in contratos:
        # Solo contar compromisos de meses en los que el contrato estuvo vigente:
        # excluye meses posteriores a fecha_fin (contrato terminado) y anteriores a
        # fecha_inicio. Antes sumaba los 12 meses sin filtrar (p.ej. Naos 2/3 mostraban
        # compromiso may-dic pese a terminar el 30-abr-2026).
        rows = [r for r in comp_by_c.get(c.id, []) if _contrato_vigente_en_mes(c, year, r.mes)]
        total_min = sum(float(r.energia_minima) for r in rows if r.energia_minima is not None)
        total_max = sum(float(r.energia_maxima) for r in rows if r.energia_maxima is not None)
        plantas_vals = [int(r.cantidad_proyectos) for r in rows if r.cantidad_proyectos is not None]
        result.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            "total_min_mwh": round(total_min, 1) if rows else None,
            "total_max_mwh": round(total_max, 1) if rows else None,
            "meses_con_compromisos": len(rows),
            # Plantas esperadas (denominador): valor máximo definido entre los meses del año.
            "plantas_esperadas": max(plantas_vals) if plantas_vals else None,
        })
    return result


@router.get("/simulador")
def get_simulador(
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Plants with avg generation + GESCON assignments for the simulator."""
    from app.models.proyectos import Proyecto, TipoProyectoEnum, EstadoProyectoEnum

    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)

    plantas_db = (
        db.query(Proyecto)
        .filter(
            Proyecto.tipo_proyecto != TipoProyectoEnum.autoconsumo,
            Proyecto.estado == EstadoProyectoEnum.en_operacion,
            # Entra a Cumplimiento si YA tiene fecha de inicio de comercialización
            # (primer día con generación real, autoderivada) O si tiene sub_project
            # (comportamiento previo). Aditivo: no saca ninguna planta que ya salía.
            or_(
                Proyecto.fecha_inicio_comercializacion.isnot(None),
                Proyecto.sub_project.isnot(None),
            ),
            # Solo plantas con servicio de representación activo (flag de Proyectos → Servicios).
            # Es la fuente correcta (la que edita el usuario), no contratos_servicio.
            Proyecto.srv_representacion.is_(True),
            # Misma semántica que /plantas-contratos y /energia-transada: no listar
            # plantas cuya representación terminó antes del mes consultado.
            or_(Proyecto.fecha_fin_representacion.is_(None), Proyecto.fecha_fin_representacion >= first_day),
        )
        .order_by(Proyecto.nombre_comercial)
        .all()
    )

    contratos_db = _contratos_vigentes(db, year, month, solo_relevantes=not incluir_todos)

    contratos_venta = _query_contratos_venta(db, year, month, solo_relevantes=not incluir_todos)
    contratos_compra = [c for c in contratos_db if (c.tipo_contrato or "venta") == "compra"]

    from sqlalchemy.orm import selectinload
    last_day = date(year, month, total_dias)
    compra_proyecto_ids: set[int] = set()
    compra_nombre_map: dict[int, str] = {}
    for cc in contratos_compra:
        if cc.fecha_fin and cc.fecha_fin < first_day:
            continue
        if cc.fecha_inicio and cc.fecha_inicio > last_day:
            continue
        cc_loaded = db.query(PPAContrato).options(selectinload(PPAContrato.proyectos)).filter(PPAContrato.id == cc.id).first()
        if cc_loaded:
            for proy in cc_loaded.proyectos:
                compra_proyecto_ids.add(proy.id)
                compra_nombre_map[proy.id] = cc.nombre_interno or cc.numero_codigo_contrato or f"Compra {cc.id}"

    proyecto_primary: dict[int, dict] = {}
    proyecto_dups: list[dict] = []
    assigned_ids: set[int] = set()
    for c in contratos_venta:
        if not c.numero_codigo_contrato:
            continue
        for asic in _resolve_gescon(db, c.numero_codigo_contrato, year, month):
            if not asic.proyecto_id:
                continue
            entry = {
                "contrato_id": c.id,
                "pct_despacho": float(asic.porcentaje_despacho or 0),
                "es_duplicado": bool(asic.es_duplicado),
                "uso_del_recurso": bool(getattr(asic, "uso_del_recurso", False)),
                "proyecto_id": asic.proyecto_id,
                "fecha_inicio": asic.fecha_inicio,
                "fecha_fin": asic.fecha_fin,
            }
            if asic.es_duplicado or asic.proyecto_id in proyecto_primary:
                # Duplicado, O una asignación primaria ADICIONAL de una planta que ya
                # tiene primaria en otro contrato (despacho partido, p.ej. 50% Terpel 1
                # + 50% Terpel 2). Antes el dict proyecto_primary sobrescribía y solo
                # sobrevivía el último contrato → el 50% del primero desaparecía de la
                # vista. Ahora cada asignación extra se emite como fila propia.
                proyecto_dups.append(entry)
            else:
                proyecto_primary[asic.proyecto_id] = entry
            assigned_ids.add(c.id)

    comp_map = {
        r.contrato_id: r
        for r in db.query(PPACompromisoEnergia).filter(
            PPACompromisoEnergia.año == year,
            PPACompromisoEnergia.mes == month,
        ).all()
    }

    today = date.today()
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    es_mes_actual = (year == today.year and month == today.month)
    dia_actual = today.day if es_mes_actual else total_dias
    dias_restantes = (total_dias - dia_actual) if es_mes_actual else 0

    sp_list = [_mon_id(p) for p in plantas_db if _mon_id(p)]
    gen_cache: dict[str, float | None] = {}
    avg_cache_sim: dict[str, float | None] = {}
    gen_warning: str | None = None
    if sp_list:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in simulador: %s", exc)
            gen_warning = "No se pudo autenticar con la API de generación. Datos de generación no disponibles."
            token = None

        if token:
            if es_mes_futuro:
                def _fa(sp: str):
                    res = _fetch_recent_avg(token, sp, n_days=30)
                    avg = res.get("avg_daily_mwh")
                    return sp, round(avg * total_dias, 3) if avg is not None else None

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, mwh in pool.map(_fa, sp_list):
                        gen_cache[sp] = mwh
            elif es_mes_actual:
                def _fm(sp: str):
                    return sp, _fetch_month(token, sp, year, month), _fetch_recent_avg(token, sp, n_days=30)

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, month_res, avg_res in pool.map(_fm, sp_list):
                        gen_cache[sp] = month_res.get("mwh")
                        avg_cache_sim[sp] = avg_res.get("avg_daily_mwh")
            else:
                def _fp(sp: str):
                    res = _fetch_month(token, sp, year, month)
                    return sp, res.get("mwh")

                with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                    for sp, mwh in pool.map(_fp, sp_list):
                        gen_cache[sp] = mwh

    plantas_by_id = {p.id: p for p in plantas_db}

    # Rangos reales para asignaciones con vigencia PARCIAL del período (relevo/
    # arranque/terminación intra-mes). Misma fuente que Matriz y Generación solar:
    # se suma la generación real de esos días, no se prorratea el total del mes.
    # (Solo meses pasado/actual; el futuro se proyecta con avg × días.)
    dias_periodo_real = dia_actual  # = total_dias para mes pasado; = hoy para mes actual
    period_end_real = date(year, month, dia_actual)

    range_cache_sim: dict = {}
    if not es_mes_futuro:
        _sp_by_pid = {pid: (_mon_id(plantas_by_id[pid]) if pid in plantas_by_id else None)
                      for pid in set(proyecto_primary) | {d["proyecto_id"] for d in proyecto_dups}}
        need_range_sim: set = set()
        for entry in list(proyecto_primary.values()) + proyecto_dups:
            sp_e = _sp_by_pid.get(entry["proyecto_id"])
            if not sp_e:
                continue
            eff_start, eff_end = _vigencia_window(entry["fecha_inicio"], entry["fecha_fin"], first_day, period_end_real)
            dias_act = max(0, (eff_end - eff_start).days + 1)
            if 0 < dias_act < dias_periodo_real:
                need_range_sim.add((sp_e, eff_start, eff_end))
        if need_range_sim:
            try:
                tok = token  # reutiliza el token ya obtenido arriba
            except NameError:
                tok = None
            if tok:
                def _fr(task: tuple) -> tuple:
                    sp, s, e = task
                    return task, _fetch_range(tok, sp, s, e)
                with ThreadPoolExecutor(max_workers=min(len(need_range_sim), 12)) as pool:
                    for task, res in pool.map(_fr, list(need_range_sim)):
                        range_cache_sim[task] = res

    def _scoped_gen(sp, fecha_inicio, fecha_fin):
        """(month_mwh, month_mwh_proyectado) de una asignación escalada a SU
        vigencia dentro del mes — energía real de la vigencia, no el total del
        mes. Misma lógica que la Matriz para que Estrategia coincida."""
        if not sp:
            return None, None
        full = gen_cache.get(sp)
        if es_mes_futuro:
            # Proyección plana: avg_daily × días vigentes (= total_proyectado × proración).
            eff_start, eff_end = _vigencia_window(fecha_inicio, fecha_fin, first_day, last_day)
            dias_act = max(0, (eff_end - eff_start).days + 1)
            if full is None or dias_act <= 0:
                return (None, None) if full is None else (0.0, 0.0)
            val = round(full * dias_act / total_dias, 3)
            return val, val
        # Mes pasado/actual: parte REAL de la vigencia hasta el corte.
        eff_start, eff_end = _vigencia_window(fecha_inicio, fecha_fin, first_day, period_end_real)
        range_gen = range_cache_sim.get((sp, eff_start, eff_end), {}).get("mwh")
        real = _gen_vigencia_mwh(eff_start, eff_end, dias_periodo_real, full, range_gen)
        if not es_mes_actual:
            return real, real
        # Mes actual: proyecta los días de vigencia que aún faltan (después de hoy).
        avg = avg_cache_sim.get(sp)
        eff_end_full = _vigencia_window(fecha_inicio, fecha_fin, first_day, last_day)[1]
        dias_proj = max(0, (eff_end_full - period_end_real).days)
        if real is None:
            return None, None
        proy = round(real + (avg or 0) * dias_proj, 3) if avg is not None else real
        return real, proy

    plantas_out = []
    for p in plantas_db:
        asn = proyecto_primary.get(p.id)
        if asn:
            # Energía escalada a la vigencia de ESTA asignación (clave del fix:
            # una planta que solo estuvo parte del mes en el contrato refleja la
            # generación real de esos días, no el total del mes).
            row_mwh, row_proy = _scoped_gen(_mon_id(p), asn["fecha_inicio"], asn["fecha_fin"])
        else:
            # Planta sin contrato PPA de venta (remanente/bolsa): generación del
            # mes completo, sin escalar.
            _mid = _mon_id(p)
            row_mwh = gen_cache.get(_mid)
            row_proy = (
                round((gen_cache.get(_mid) or 0) + (avg_cache_sim.get(_mid) or 0) * dias_restantes, 3)
                if es_mes_actual and avg_cache_sim.get(_mid) is not None
                else gen_cache.get(_mid)
            )
        plantas_out.append({
            "id": p.id,
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "tipo_proyecto": p.tipo_proyecto,
            "potencia_kwp": float(p.potencia_instalada_kwp) if p.potencia_instalada_kwp else None,
            "month_mwh": row_mwh,
            "month_mwh_proyectado": row_proy,
            "contrato_id": asn["contrato_id"] if asn else None,
            "pct_despacho": asn["pct_despacho"] if asn else 1.0,
            "es_duplicado": False,
            "uso_del_recurso": asn["uso_del_recurso"] if asn else False,
            "comprado_por_unergy": p.id in compra_proyecto_ids,
            "contrato_compra_nombre": compra_nombre_map.get(p.id),
            # Subdivisión del remanente (mismo criterio que /plantas-contratos): solo para
            # plantas sin contrato PPA de venta. "comercializador" (UNGC) | "libre" | None.
            "piscina_bolsa": (
                None if asn else _clasificar_remanente_bolsa(db, p.id, first_day, last_day)[0]
            ),
        })

    for dup in proyecto_dups:
        p = plantas_by_id.get(dup["proyecto_id"])
        if not p:
            continue
        # Escalada a la vigencia de esta asignación (igual que la fila primaria).
        dup_mwh, dup_proy = _scoped_gen(_mon_id(p), dup["fecha_inicio"], dup["fecha_fin"])
        plantas_out.append({
            "id": f"{p.id}_dup_{dup['contrato_id']}",
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "tipo_proyecto": p.tipo_proyecto,
            "potencia_kwp": float(p.potencia_instalada_kwp) if p.potencia_instalada_kwp else None,
            "month_mwh": dup_mwh,
            "month_mwh_proyectado": dup_proy,
            "contrato_id": dup["contrato_id"],
            "pct_despacho": dup["pct_despacho"],
            # es_duplicado real del registro: True para duplicado (compra en bolsa),
            # False para una segunda asignación primaria (despacho partido entre contratos).
            "es_duplicado": dup["es_duplicado"],
            "uso_del_recurso": dup["uso_del_recurso"],
            # Misma lógica que la fila primaria: si Unergy compra la planta vía un
            # contrato de compra, la etiqueta es "comprado por Unergy" aunque la fila
            # sea duplicado de un contrato de venta (ej. GD Astrolumen La Garita en
            # Terpel 4). Antes estaba hardcodeado a False/None y salía como compra en bolsa.
            "comprado_por_unergy": p.id in compra_proyecto_ids,
            "contrato_compra_nombre": compra_nombre_map.get(p.id),
            "piscina_bolsa": None,  # los duplicados pertenecen a un contrato, no al remanente
        })

    contratos_out = []
    for c in contratos_venta:
        comp = comp_map.get(c.id)
        if comp is None and c.id not in assigned_ids:
            continue
        contratos_out.append({
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "min_mwh": float(comp.energia_minima) if comp and comp.energia_minima is not None else None,
            "max_mwh": float(comp.energia_maxima) if comp and comp.energia_maxima is not None else None,
            # Plantas esperadas para el mes (denominador del indicador de cumplimiento de plantas).
            # El numerador (plantas registradas) lo calcula el frontend con las plantas asignadas.
            "plantas_esperadas": int(comp.cantidad_proyectos) if comp and comp.cantidad_proyectos is not None else None,
        })

    result = {
        "year": year, "month": month, "dias_mes": total_dias,
        "es_mes_futuro": es_mes_futuro, "es_mes_actual": es_mes_actual,
        "dia_actual": dia_actual, "dias_restantes": dias_restantes,
        "plantas": plantas_out, "contratos": contratos_out,
    }
    if gen_warning:
        result["warning"] = gen_warning
    return result


@router.get("/vista-contratos", summary="Foto de un día: qué planta está en qué contrato")
def get_vista_contratos(
    fecha: str = Query(..., description="Día de la foto, YYYY-MM-DD (p. ej. 2026-08-20)"),
    responsable: str | None = Query(
        "Unergy",
        description="Empresa responsable a mostrar. Vacío o 'todos' = sin filtro. "
                    "Ojo: filtra ESTRICTO — un contrato sin responsable asignado no pasa.",
    ),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Todo lo que hace falta para leer los contratos de venta de UN día, en una
    sola llamada: qué plantas tiene cada contrato ese día, cuánto se comprometió
    el contrato en el mes y cuánto genera cada planta en un mes típico.

    Pensado para consumirse desde afuera (script, Excel, Power BI) sin tener que
    entender GESCON. Ver `docs/API_VISTA_CONTRATOS.md`.

    Es solo lectura y no escribe nada.
    """
    from app.services import vista_contratos

    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        raise HTTPException(422, f"'{fecha}' no es una fecha válida. Usá el formato YYYY-MM-DD.")

    filtro = None if (responsable or "").strip().lower() in ("", "todos") else responsable
    return vista_contratos.construir(db, fecha=dia, responsable=filtro,
                                     incluir_todos=incluir_todos)


@router.get("/plantas-contratos")
def get_plantas_contratos(
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Overview of all plants grouped by contract: venta, compra, and bolsa."""
    from app.models.proyectos import Proyecto, TipoProyectoEnum, EstadoProyectoEnum
    from sqlalchemy.orm import selectinload

    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)

    plantas_db = (
        db.query(Proyecto)
        .filter(
            Proyecto.tipo_proyecto != TipoProyectoEnum.autoconsumo,
            Proyecto.estado == EstadoProyectoEnum.en_operacion,
            # Entra a Cumplimiento si YA tiene fecha de inicio de comercialización
            # (primer día con generación real, autoderivada) O si tiene sub_project
            # (comportamiento previo). Aditivo: no saca ninguna planta que ya salía.
            or_(
                Proyecto.fecha_inicio_comercializacion.isnot(None),
                Proyecto.sub_project.isnot(None),
            ),
            # Solo plantas con servicio de representación activo (flag de Proyectos → Servicios).
            # Es la fuente correcta (la que edita el usuario), no contratos_servicio.
            Proyecto.srv_representacion.is_(True),
            # Excluir plantas cuya representación ya terminó antes del mes consultado:
            # aparece si fecha_fin_representacion >= primer día del mes (o si es NULL).
            or_(Proyecto.fecha_fin_representacion.is_(None), Proyecto.fecha_fin_representacion >= first_day),
        )
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    plantas_map = {p.id: p for p in plantas_db}

    contratos_venta = _query_contratos_venta(db, year, month, solo_relevantes=not incluir_todos)
    corte = _fecha_corte(year, month)

    # --- VENTA: use GESCON to resolve plant assignments ---
    venta_out = []
    # Días del mes que cada planta pasó asignada a un contrato de venta. La
    # bolsa es el RESIDUO de esto (ver más abajo), no el complemento del set de
    # plantas: así una planta liberada a mitad de mes aparece en las dos.
    assigned_windows: dict[int, list] = defaultdict(list)
    for c in contratos_venta:
        plantas_list = []
        if c.numero_codigo_contrato:
            for asic in _resolve_gescon(db, c.numero_codigo_contrato, year, month):
                if asic.proyecto_id and asic.proyecto_id in plantas_map:
                    p = plantas_map[asic.proyecto_id]
                    seg = _recortar(asic.fecha_inicio, asic.fecha_fin, first_day, last_day)
                    if seg:
                        assigned_windows[p.id].append(seg)
                    plantas_list.append(_con_segmento({
                        "id": p.id,
                        "nombre": p.nombre_comercial,
                        "codigo_sic": asic.codigo_sic_contrato,
                        "fecha_inicio": asic.fecha_inicio.isoformat() if asic.fecha_inicio else None,
                        "fecha_fin": asic.fecha_fin.isoformat() if asic.fecha_fin else None,
                        "pct_despacho": float(asic.porcentaje_despacho or 0),
                        "es_duplicado": bool(asic.es_duplicado),
                        "uso_del_recurso": bool(getattr(asic, "uso_del_recurso", False)),
                        # 'plg' | 'plc': una planta repartida entre dos contratos,
                        # uno de cada modalidad, no está duplicada.
                        "modalidad_pago": getattr(asic, "modalidad_pago", None),
                    }, asic.fecha_inicio, asic.fecha_fin, first_day, last_day, corte))
        venta_out.append({
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            # Clave GESCON (contrato_interno en asic_solicitudes) para el detalle
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            "plantas": plantas_list,
        })

    # --- COMPRA (b. ppa_compra_ungc): GESCON PURO ---
    # Nada del módulo PPA: las compras de UNGC salen de asic_solicitudes —
    # registros publicados con codigo_sic_comprador == UNGC, agrupados por
    # contrato_interno. La vigencia se resuelve con el mismo núcleo
    # (relevos/modificaciones, gescon_vigencia) y la ventana efectiva debe
    # pisar el mes. Antes se listaban los PPAContrato tipo_contrato='compra'
    # (compras GD a terceros registradas a mano) — eso NO es UNGC en el MEM.
    compra_out = []
    sics_ungc = [
        s for (s,) in db.query(AsicSolicitud.codigo_sic_contrato).filter(
            AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
            AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.desistimiento,
            AsicSolicitud.codigo_sic_comprador == UNGC_COMERCIALIZADOR,
            AsicSolicitud.codigo_sic_contrato.isnot(None),
        ).distinct()
    ]
    if sics_ungc:
        # Se resuelve sobre TODO el historial de esos SICs (un relevo puede
        # venir de una fila con otro comprador) y luego se filtra a UNGC.
        registros_ungc = (
            db.query(AsicSolicitud)
            .options(joinedload(AsicSolicitud.proyecto))
            .filter(
                AsicSolicitud.codigo_sic_contrato.in_(sics_ungc),
                AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
                AsicSolicitud.tipo_solicitud != TipoSolicitudAsicEnum.desistimiento,
            )
            .order_by(
                AsicSolicitud.fecha_inicio.asc().nullsfirst(),
                AsicSolicitud.fecha_solicitud.asc().nullsfirst(),
                AsicSolicitud.created_at.asc(),
            )
            .all()
        )
        vig_ungc = resolver_vigencias(registros_ungc, hasta=last_day)
        por_contrato: dict[str, dict] = {}
        for r in registros_ungc:
            v = vig_ungc[r.id]
            if not v.procesado or r.tipo_solicitud == TipoSolicitudAsicEnum.terminacion:
                continue
            if not v.vigente and not v.saliente_por_relevo:
                continue  # superada en sitio: la representa su versión nueva
            if (r.codigo_sic_comprador or "") != UNGC_COMERCIALIZADOR:
                continue
            fin_ef = v.fecha_fin_efectiva
            if fin_ef is not None and fin_ef < first_day:
                continue
            if r.fecha_inicio and r.fecha_inicio > last_day:
                continue
            key = r.contrato_interno or f"SIC {r.codigo_sic_contrato}"
            card = por_contrato.setdefault(key, {
                "id": r.contrato_ppa_id or f"gescon-{r.codigo_sic_contrato}",
                "contrato_ppa_id": r.contrato_ppa_id,
                "contrato_interno": r.contrato_interno,
                "nombre": r.nombre_interno or key,
                "vendedor_nombre": r.codigo_sic_vendedor or "—",
                "fecha_inicio": None,
                "fecha_fin": None,
                "plantas": [],
            })
            if r.contrato_ppa_id and not card["contrato_ppa_id"]:
                card["contrato_ppa_id"] = r.contrato_ppa_id
                card["id"] = r.contrato_ppa_id
            if r.proyecto_id:
                fin_mostrado = fin_ef or r.fecha_fin
                card["plantas"].append(_con_segmento({
                    "id": r.proyecto_id,
                    "nombre": r.proyecto.nombre_comercial if r.proyecto else f"Proyecto {r.proyecto_id}",
                    "codigo_sic": r.codigo_sic_contrato,
                    "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
                    # fin EFECTIVO (recortado por relevos), no la fecha cruda
                    "fecha_fin": fin_mostrado.isoformat() if fin_mostrado else None,
                }, r.fecha_inicio, fin_mostrado, first_day, last_day, corte))
        compra_out = sorted(por_contrato.values(), key=lambda c: c["nombre"])

    # --- COMPRA EXTERNA (g. ppa_compra_externa): PPAs de compra FUERA de GESCON ---
    # Plantas de terceros a las que Unergy les compra energía directamente con un
    # PPA firmado a mano (módulo PPA, tipo_contrato='compra') sin registro en
    # GESCON/ASIC. Antes se listaban en (b); al volver (b) GESCON-puro quedaron
    # sin piscina. Un PPA de compra que sí llegó a GESCON ya está en (b) y se
    # excluye aquí para no duplicarlo.
    gescon_compra_ids = {c["contrato_ppa_id"] for c in compra_out if c.get("contrato_ppa_id")}
    compra_externa_out = []
    for c in _contratos_vigentes(db, year, month, solo_relevantes=not incluir_todos):
        if (c.tipo_contrato or "venta") != "compra" or c.id in gescon_compra_ids:
            continue
        compra_externa_out.append({
            "id": c.id,
            "nombre": c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}",
            "numero_codigo_contrato": c.numero_codigo_contrato,
            **_responsable_payload(c),
            "vendedor_nombre": c.vendedor_nombre,
            "vendedor_nit": c.vendedor_nit,
            "tarifa_base": float(c.tarifa_base) if c.tarifa_base is not None else None,
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            # Plantas externas vinculadas al PPA (no exigen representación ni
            # estado en_operacion: no son plantas del portafolio Unergy).
            "plantas": [
                _con_segmento({
                    "id": p.id,
                    "nombre": p.nombre_comercial,
                    "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
                    "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
                }, c.fecha_inicio, c.fecha_fin, first_day, last_day, corte)
                for p in c.proyectos
            ],
        })

    # --- BOLSA: DÍAS del mes sin contrato PPA, subdivididos en comercializador (UNGC) / libre ---
    # Paso POSTERIOR que NO altera la lógica de contratos: solo reparte el residuo.
    # Antes esto era "plantas no asignadas"; con eso una planta liberada el 23-jul
    # quedaba en (a) los 31 días y (e) salía vacía. Ahora se restan los días ya
    # cubiertos por contratos y CADA tramo libre genera su propia fila.
    bolsa_plantas = []
    bolsa_comercializador = []
    bolsa_libre = []
    for p in plantas_db:
        # Ventana operativa dentro del mes: no inventamos días en bolsa antes de
        # que la planta empiece a comercializar ni después de que termine su
        # representación. Ambos campos suelen ser NULL → mes completo.
        operativa = _recortar(
            p.fecha_inicio_comercializacion, p.fecha_fin_representacion, first_day, last_day
        )
        for seg_ini, seg_fin in _restar_intervalos(operativa, assigned_windows.get(p.id) or []):
            # La modalidad se evalúa SOBRE EL TRAMO, no sobre el mes: una planta
            # que salió de contrato el 23 se juzga por lo que tenga del 24 al 31.
            piscina, asic = _clasificar_remanente_bolsa(db, p.id, seg_ini, seg_fin)
            fin_efectivo = _fin_efectivo_asic(db, asic, seg_fin) if asic else None
            entry = {
                "id": p.id,
                "nombre": p.nombre_comercial,
                # "comercializador" (registro SIC vigente con comprador UNGC) | "libre" (sin SIC vigente)
                "piscina": piscina,
                "codigo_sic": asic.codigo_sic_contrato if asic else None,
                "codigo_sic_comprador": asic.codigo_sic_comprador if asic else None,
                # ventana de la modalidad: inicio del registro SIC y fin EFECTIVO
                # (recortado por relevos). Nulos para la piscina libre — ahí la
                # ventana que importa es el propio tramo (segmento_*).
                "fecha_inicio": asic.fecha_inicio.isoformat() if asic and asic.fecha_inicio else None,
                "fecha_fin": fin_efectivo.isoformat() if fin_efectivo else None,
                "segmento_inicio": seg_ini.isoformat(),
                "segmento_fin": seg_fin.isoformat(),
                "estado": _estado_segmento(seg_ini, seg_fin, corte),
            }
            bolsa_plantas.append(entry)
            (bolsa_comercializador if piscina == "comercializador" else bolsa_libre).append(entry)

    out = {
        "year": year,
        "month": month,
        # Fecha contra la que se evalúa "vigente" (hoy si el mes es el actual,
        # fin de mes si no). El front la usa para explicar los contadores.
        "fecha_corte": corte.isoformat(),
        "venta": venta_out,
        "compra": compra_out,
        # Compatibilidad con el frontend: "bolsa" sigue siendo la lista COMPLETA del remanente,
        # con el mismo shape de antes (id, nombre) + un campo nuevo "piscina". Se añaden además
        # dos sub-listas ("bolsa_comercializador" UNGC / "bolsa_libre") que apuntan a los mismos
        # objetos, para el front que quiera consumirlas directamente. Nada existente se rompe.
        "bolsa": bolsa_plantas,
        "bolsa_comercializador": bolsa_comercializador,
        "bolsa_libre": bolsa_libre,
        "compra_externa": compra_externa_out,
    }
    # Piscinas estandarizadas a-f (misma fuente que GET /clasificacion-energia):
    # aditivo — re-agrupa lo anterior sin alterar las claves existentes.
    from app.services.clasificacion_energia import derivar_pools
    out.update(derivar_pools(out))
    return out


@router.get("/balance-energia")
def get_balance_energia(
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    excluir_compra_externa: bool = Query(
        False,
        description="Saca del balance las plantas con PPA de compra externa (piscina g). "
                    "Su energía está comprada fuera de GESCON, así que contarlas en el "
                    "residuo de bolsa infla la venta en bolsa UNGG.",
    ),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Balance mensual de energía en bolsa (MWh), real + proyección al cierre.

    Responde "este mes, ¿cuánto compro o compraré en bolsa?". Netea DENTRO de
    cada agente: la venta en bolsa de UNGG se contrarresta con sus compras
    (duplicados + uso del recurso); la venta por UNGC va aparte porque es otro
    agente y solo acarrea cartera.

    Cruza los DOS ejes que hasta ahora se calculaban por separado: los días de
    cada tramo (historial intra-mes) y el % de despacho. Lo que no está
    contratado en un tramo se vende en bolsa por UNGG.
    """
    from app.services.balance_energia import calcular_balance

    return calcular_balance(
        db, year, month, excluir_compra_externa=excluir_compra_externa,
        incluir_todos=incluir_todos,
    )


@router.post("/backfill-comercializacion")
def post_backfill_comercializacion(
    dry_run: bool = Query(True, description="Si es true (default) no escribe, solo reporta lo que haría."),
    force: bool = Query(False, description="Recalcula también proyectos que ya tienen fecha (respeta los editados a mano)."),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Deriva y persiste la fecha de inicio de comercialización (primer día con
    generación real) para los proyectos que la necesitan.

    Idempotente. Sin ``force`` solo toca los que tienen la fecha en NULL y no
    fueron editados a mano. Devuelve el detalle de lo actualizado + las listas de
    diagnóstico (sin generación / sin identificador de monitoreo).
    """
    from app.services.comercializacion import backfill_comercializacion
    return backfill_comercializacion(db, force=force, dry_run=dry_run)


@router.get("/sin-fecha-comercializacion")
def get_sin_fecha_comercializacion(
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Proyectos (no autoconsumo) que aún NO tienen fecha de inicio de
    comercialización, con el motivo probable. Es el reporte final que pide el
    negocio para saber qué plantas faltan por entrar a Cumplimiento."""
    from app.services.comercializacion import proyectos_sin_fecha_comercializacion
    filas = proyectos_sin_fecha_comercializacion(db)
    return {"total": len(filas), "proyectos": filas}


@router.get("/energia-transada")
def get_energia_transada(
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Energía transada por planta en el mes (solo datos reales, sin proyección).

    Para cada planta representada: generación del período (mes cerrado completo,
    mes actual hasta hoy), cuánta se transó vía PPA (asignación GESCON ×
    % despacho, prorrateado por días activos dentro del período) y cuánta
    quedó en bolsa (remanente sin asignación). Asignaciones duplicadas
    (exposición bolsa) no cuentan como PPA.

    Optimizado: 2 queries DB principales + un solo fetch por planta en paralelo.
    """
    from app.models.proyectos import Proyecto, TipoProyectoEnum, EstadoProyectoEnum

    today = date.today()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_corte = today.day if es_mes_actual else total_dias
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)
    corte = date(year, month, dia_corte)

    periodo = {
        "year": year,
        "month": month,
        "dias_mes": total_dias,
        "dia_corte": dia_corte,
        "fecha_corte": corte.isoformat(),
        "es_mes_actual": es_mes_actual,
        "es_mes_futuro": es_mes_futuro,
    }

    if es_mes_futuro:
        return {
            "periodo": periodo,
            "plantas": [],
            "totales": {"gen_mwh": 0.0, "ppa_mwh": 0.0, "bolsa_mwh": 0.0, "n_plantas": 0},
        }

    # ── 1. Plantas representadas activas en el período (1 query) ──────────────
    plantas_db = (
        db.query(Proyecto)
        .filter(
            Proyecto.tipo_proyecto != TipoProyectoEnum.autoconsumo,
            Proyecto.estado == EstadoProyectoEnum.en_operacion,
            # Entra a Cumplimiento si YA tiene fecha de inicio de comercialización
            # (primer día con generación real, autoderivada) O si tiene sub_project
            # (comportamiento previo). Aditivo: no saca ninguna planta que ya salía.
            or_(
                Proyecto.fecha_inicio_comercializacion.isnot(None),
                Proyecto.sub_project.isnot(None),
            ),
            # Solo plantas con servicio de representación activo (flag de Proyectos → Servicios).
            # Es la fuente correcta (la que edita el usuario), no contratos_servicio.
            Proyecto.srv_representacion.is_(True),
            or_(Proyecto.fecha_entrada_operacion.is_(None), Proyecto.fecha_entrada_operacion <= last_day),
            or_(Proyecto.fecha_fin_representacion.is_(None), Proyecto.fecha_fin_representacion >= first_day),
        )
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    plantas_by_id = {p.id: p for p in plantas_db}

    # ── 2. Asignaciones GESCON de contratos de venta vigentes ─────────────────
    contratos_venta = [
        c for c in _contratos_vigentes(db, year, month, solo_relevantes=not incluir_todos)
        if (getattr(c, "tipo_contrato", None) or "venta") != "compra"
    ]
    asignaciones: dict[int, list[dict]] = defaultdict(list)
    for c in contratos_venta:
        if not c.numero_codigo_contrato:
            continue
        nombre_c = c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}"
        for asic in _resolve_gescon(db, c.numero_codigo_contrato, year, month):
            if not asic.proyecto_id:
                continue
            # Ventana efectiva dentro del período [primer día .. corte]
            eff_start, eff_end = _vigencia_window(asic.fecha_inicio, asic.fecha_fin, first_day, corte)
            dias_activos = max(0, (eff_end - eff_start).days + 1)
            if dias_activos == 0:
                continue
            asignaciones[asic.proyecto_id].append({
                "contrato_id": c.id,
                "contrato": nombre_c,
                "pct": float(asic.porcentaje_despacho or 0),
                "dias_activos": dias_activos,
                "eff_start": eff_start,
                "eff_end": eff_end,
                "es_duplicado": bool(asic.es_duplicado),
            })

    # Plantas con GESCON que no entraron en el filtro inicial (1 query extra solo si hace falta)
    missing_ids = set(asignaciones) - set(plantas_by_id)
    if missing_ids:
        extra = db.query(Proyecto).filter(
            Proyecto.id.in_(missing_ids),
            or_(Proyecto.fecha_inicio_comercializacion.isnot(None), Proyecto.sub_project.isnot(None)),
        ).all()
        for p in extra:
            plantas_by_id[p.id] = p
        plantas_db = sorted(plantas_by_id.values(), key=lambda p: p.nombre_comercial or "")

    # ── 3. Generación en paralelo (un fetch por sub_project único) ────────────
    # need_range: registros con vigencia PARCIAL del período (relevo/arranque/
    # terminación intra-mes) — su energía se suma día a día (misma fuente que la
    # Matriz y Generación solar), no se prorratea el total del período.
    need_range: set = set()
    for pid, asigs in asignaciones.items():
        sp_p = _mon_id(plantas_by_id[pid]) if pid in plantas_by_id else None
        if not sp_p:
            continue
        for a in asigs:
            if 0 < a["dias_activos"] < dia_corte:
                need_range.add((sp_p, a["eff_start"], a["eff_end"]))

    sp_set = {_mon_id(p) for p in plantas_db if _mon_id(p)}
    gen_cache: dict[str, dict] = {}
    range_cache: dict = {}
    warning = None
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in energia-transada: %s", exc)
            token = None
            warning = "No se pudo autenticar con la API de generación."
        if token:
            sp_list = list(sp_set)

            def _fetch_sp(sp: str) -> tuple:
                return sp, _fetch_month(token, sp, year, month)

            with ThreadPoolExecutor(max_workers=min(len(sp_list), 12)) as pool:
                for sp, res in pool.map(_fetch_sp, sp_list):
                    gen_cache[sp] = res

            if need_range:
                def _fetch_rg(task: tuple) -> tuple:
                    sp, start, end = task
                    return task, _fetch_range(token, sp, start, end)

                with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                    for task, res in pool.map(_fetch_rg, list(need_range)):
                        range_cache[task] = res

    # ── 4. Cálculo por planta ─────────────────────────────────────────────────
    plantas_out = []
    total_gen = total_ppa = total_bolsa = 0.0
    for p in plantas_db:
        gd = gen_cache.get(_mon_id(p), {"mwh": None, "ultimo_dia": None})
        gen = gd.get("mwh")
        asigs = asignaciones.get(p.id, [])
        contratos_planta = [
            {
                "id": a["contrato_id"],
                "nombre": a["contrato"],
                "pct": a["pct"],
                "dias_activos": a["dias_activos"],
                "es_duplicado": a["es_duplicado"],
            }
            for a in asigs
        ]
        if gen is None:
            plantas_out.append({
                "id": p.id,
                "nombre": p.nombre_comercial,
                "sub_project": p.sub_project,
                "potencia_kwp": float(p.potencia_instalada_kwp) if p.potencia_instalada_kwp else None,
                "gen_mwh": None, "ppa_mwh": None, "bolsa_mwh": None,
                "modo": "sin_datos", "contratos": contratos_planta, "ultimo_dia": None,
            })
            continue

        # Energía PPA = Σ (generación REAL de la vigencia de cada asignación ×
        # % despacho). Vigencia mes completo → gen del período; parcial → suma
        # real de esos días (helper compartido con Estrategia/Matriz), nunca
        # regla de tres sobre el total del período.
        ppa = 0.0
        for a in asigs:
            if a["es_duplicado"]:
                continue
            range_gen = range_cache.get((_mon_id(p), a["eff_start"], a["eff_end"]), {}).get("mwh")
            gv = _gen_vigencia_mwh(a["eff_start"], a["eff_end"], dia_corte, gen, range_gen)
            if gv is not None:
                ppa += gv * a["pct"]
        ppa = round(min(ppa, gen), 3)
        bolsa = round(max(0.0, gen - ppa), 3)
        if not any(not a["es_duplicado"] for a in asigs) or ppa <= 0:
            modo = "bolsa"
        elif bolsa <= max(0.001, gen * 0.005):
            modo, bolsa = "ppa", 0.0
        else:
            modo = "mixto"

        total_gen += gen
        total_ppa += ppa
        total_bolsa += bolsa
        plantas_out.append({
            "id": p.id,
            "nombre": p.nombre_comercial,
            "sub_project": p.sub_project,
            "potencia_kwp": float(p.potencia_instalada_kwp) if p.potencia_instalada_kwp else None,
            "gen_mwh": round(gen, 3),
            "ppa_mwh": ppa,
            "bolsa_mwh": bolsa,
            "modo": modo,
            "contratos": contratos_planta,
            "ultimo_dia": gd.get("ultimo_dia"),
        })

    plantas_out.sort(key=lambda x: -(x["gen_mwh"] or 0))

    result = {
        "periodo": periodo,
        "plantas": plantas_out,
        "totales": {
            "gen_mwh": round(total_gen, 3),
            "ppa_mwh": round(total_ppa, 3),
            "bolsa_mwh": round(total_bolsa, 3),
            "n_plantas": len([x for x in plantas_out if x["gen_mwh"] is not None]),
            "n_sin_datos": len([x for x in plantas_out if x["gen_mwh"] is None]),
        },
    }
    if warning:
        result["warning"] = warning
    return result


def _rollup_cumplimiento(meses: list[dict]) -> dict:
    """Deriva el rollup anual de cumplimiento a partir de los 12 meses de un contrato.

    Consume la lista producida por `_anual_meses_para_contrato` y devuelve un resumen
    con las métricas clave de cumplimiento anual.
    """
    deficit = sum(1 for m in meses if m.get("estado") == "deficit")
    bolsa = sum(
        (m.get("compras_bolsa_mwh") or 0) + (m.get("exposicion_bolsa_duplicados_mwh") or 0)
        for m in meses
    )
    return {
        "estado_cumplimiento": "no_cumple" if deficit > 0 else "cumple",
        "meses_en_deficit": deficit,
        "requiere_bolsa": bolsa > 0,
        "total_anual_mwh": round(sum(m.get("valor_mwh") or 0 for m in meses), 3),
        "total_min_anual_mwh": round(sum(m.get("min_mwh") or 0 for m in meses), 3),
        "bolsa_anual_mwh": round(bolsa, 3),
    }


def _anual_meses_para_contrato(contrato, year, gescon_per_month, comp_map, month_cache, avg_cache, today, range_cache=None):
    """Construye los 12 meses + desglose por proyecto para un contrato.

    Caches (month_cache/avg_cache/range_cache), gescon_per_month y comp_map vienen
    ya poblados (sin I/O aquí). Retorna (meses, proyectos) con `valor_mwh` por mes
    tanto a nivel de contrato como por proyecto, manteniendo:
    contrato.valor_mwh == Σ proyectos.valor_mwh.

    range_cache: dict[(sub_project, eff_start, eff_end)] -> {"mwh": ...} con la
    generación REAL sumada día a día para un registro que estuvo vigente solo
    PARTE de un mes (relevo, arranque o terminación a mitad de mes). Cuando el
    registro cubre el mes completo se sigue usando month_cache (sin cambios).
    """
    range_cache = range_cache or {}
    # proyectos_acc: (pid, sp, nombre) -> {"pct": last_pct, "meses": [valor_mwh x 12]}
    proyectos_acc: dict = {}

    meses = []
    for m in range(1, 13):
        total_dias = calendar.monthrange(year, m)[1]
        first_day_m = date(year, m, 1)
        last_day_m = date(year, m, total_dias)
        is_current = (year == today.year and m == today.month)
        is_future = (year > today.year) or (year == today.year and m > today.month)
        dia_actual = today.day if is_current else total_dias

        # Vigencia del contrato en el mes: un compromiso del mes M solo cuenta si el
        # contrato está vigente en M. Respeta fecha_inicio y fecha_fin del PPAContrato.
        # Caso real: Naos 2/3 terminaron el 30-abr-2026 (se acabó la representación de
        # las plantas); sus compromisos de may-dic NO deben contar ni marcar déficit.
        c_ini = getattr(contrato, "fecha_inicio", None)
        c_fin = getattr(contrato, "fecha_fin", None)
        vigente_m = (
            (c_ini is None or c_ini <= last_day_m)
            and (c_fin is None or c_fin >= first_day_m)
        )

        comp = comp_map.get(m)
        min_mwh: Optional[float] = float(comp.energia_minima) if comp and comp.energia_minima is not None else None
        max_mwh: Optional[float] = float(comp.energia_maxima) if comp and comp.energia_maxima is not None else None
        plantas_esp: Optional[int] = int(comp.cantidad_proyectos) if comp and comp.cantidad_proyectos is not None else None

        plantas_mes = []
        gen_total = 0.0
        bolsa_dup_total = 0.0
        for asic in gescon_per_month[m]:
            proyecto = asic.proyecto
            nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
            sp = proyecto.sub_project if proyecto else None
            pid = asic.proyecto_id
            pct = float(asic.porcentaje_despacho or 0)
            is_dup = bool(asic.es_duplicado)

            eff_start, eff_end = _vigencia_window(asic.fecha_inicio, asic.fecha_fin, first_day_m, last_day_m)
            dias_activos = max(0, (eff_end - eff_start).days + 1)
            proration = dias_activos / total_dias

            if sp:
                if is_future:
                    # Mes futuro sin datos reales: se sigue proyectando con el
                    # promedio diario reciente × días vigentes (avg × dias_activos).
                    avg = avg_cache.get(sp)
                    gp: Optional[float] = round(avg * total_dias, 3) if avg is not None else None
                    gen_contrato = round(gp * pct * proration, 3) if gp is not None else None
                else:
                    # Mes pasado/actual: generación REAL de la vigencia — total del
                    # mes si cubre el mes completo, o suma real de los días exactos
                    # si la vigencia fue parcial (helper compartido con Estrategia y
                    # Energía transada; nunca regla de tres sobre el total).
                    month_gen = month_cache.get((m, sp), {}).get("mwh")
                    range_gen = range_cache.get((sp, eff_start, eff_end), {}).get("mwh")
                    gp = _gen_vigencia_mwh(eff_start, eff_end, total_dias, month_gen, range_gen)
                    gen_contrato = round(gp * pct, 3) if gp is not None else None
            else:
                gp = None
                gen_contrato = None
            if gen_contrato is not None:
                # Cuenta para el cumplimiento sin importar el origen; el duplicado
                # se registra además en bolsa_dup_total como sub-cifra (origen bolsa).
                gen_total += gen_contrato
                if is_dup:
                    bolsa_dup_total += gen_contrato
            plantas_mes.append({
                "nombre": nombre,
                "sub_project": sp,
                "pct_despacho": pct,
                "dias_en_contrato": dias_activos,
                "dias_mes": total_dias,
                "gen_planta_mwh": gp,
                "gen_contrato_mwh": gen_contrato,
                "es_duplicado": is_dup,
                # getattr: los tests de endpoints usan fakes sin el atributo nuevo
                "uso_del_recurso": bool(getattr(asic, "uso_del_recurso", False)),
            })

            # Accumulate per-project valor_mwh (preliminary: gen_contrato for past/future)
            key = (pid, sp or "", nombre)
            if key not in proyectos_acc:
                proyectos_acc[key] = {"pct": pct, "is_dup": is_dup,
                                      "is_ur": bool(getattr(asic, "uso_del_recurso", False)),
                                      "meses": [None] * 12}
            proyectos_acc[key]["pct"] = pct  # last seen
            proyectos_acc[key]["meses"][m - 1] = gen_contrato  # will be overwritten for current month below

        gen_total = round(gen_total, 3)
        bolsa_dup_total = round(bolsa_dup_total, 3)

        # Projection based on 30-day rolling average
        gen_cierre: Optional[float] = None
        if is_current:
            dias_restantes = total_dias - dia_actual
            avg_30d_total = 0.0
            avg_available = False
            for asic in gescon_per_month[m]:
                proyecto = asic.proyecto
                sp = proyecto.sub_project if proyecto else None
                if not sp:
                    continue
                nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
                pid = asic.proyecto_id
                pct = float(asic.porcentaje_despacho or 0)
                is_dup = bool(asic.es_duplicado)
                # La proyección incluye duplicados: la compra en bolsa también
                # cubre el contrato de cara a la contraparte.
                eff_start = max(first_day_m, asic.fecha_inicio) if asic.fecha_inicio else first_day_m
                eff_end = min(last_day_m, asic.fecha_fin) if asic.fecha_fin else last_day_m
                dias_activos = max(0, (eff_end - eff_start).days + 1)
                proration = dias_activos / total_dias
                avg_daily = avg_cache.get(sp)
                if avg_daily is not None:
                    avg_30d_total += avg_daily * pct * proration
                    avg_available = True

                # Per-project valor_mwh for current month = gen_contrato_actual + projection
                key = (pid, sp, nombre)
                gen_contrato_actual = proyectos_acc.get(key, {}).get("meses", [None] * 12)[m - 1] or 0.0
                if avg_daily is not None:
                    proj_planta = gen_contrato_actual + avg_daily * pct * proration * dias_restantes
                    if key in proyectos_acc:
                        proyectos_acc[key]["meses"][m - 1] = round(proj_planta, 3)
                # If avg_daily is None, leave gen_contrato as best estimate (already set)

            if avg_available and gen_total >= 0:
                gen_cierre = round(gen_total + avg_30d_total * dias_restantes, 3)
            gen_proy = gen_cierre
        elif is_future:
            gen_proy = gen_total if gen_total > 0 else None
        else:
            gen_proy = None

        # Contract-level valor_mwh: real → gen_total; current → gen_cierre; future → gen_proy
        if is_current and gen_cierre is not None:
            valor_mwh_contrato = gen_cierre
        elif is_future:
            valor_mwh_contrato = gen_proy if gen_proy is not None else gen_total
        else:
            valor_mwh_contrato = gen_total

        val = gen_cierre if is_current and gen_cierre is not None else (gen_proy if is_future else gen_total)
        if not vigente_m:
            # Contrato no vigente este mes: terminó (posterior a fecha_fin) o aún no
            # inicia (anterior a fecha_inicio). No hay compromiso que cumplir → no contar
            # ni marcar déficit. Se anula min/max/plantas y se marca el estado para que
            # el front muestre "finalizado"/"no_iniciado" en vez de 0 plantas / déficit.
            min_mwh = None
            max_mwh = None
            plantas_esp = None
            valor_mwh_contrato = None
            estado = "finalizado" if (c_fin and c_fin < first_day_m) else "no_iniciado"
            compras, excedentes = None, None
        elif min_mwh is not None or max_mwh is not None:
            if val is None:
                # Hay compromiso pero aún no hay generación/proyección (p.ej. mes futuro
                # sin plantas asignadas todavía): no se puede evaluar cumplimiento.
                estado, compras, excedentes = "sin_datos", None, None
            else:
                effective_min = min_mwh if min_mwh is not None else 0.0
                effective_max = max_mwh if max_mwh is not None else float('inf')
                if val < effective_min:
                    estado, compras, excedentes = "deficit", round(max(0., effective_min - val), 3), 0.
                elif val > effective_max:
                    estado, compras, excedentes = "excedente", 0., round(max(0., val - effective_max), 3)
                else:
                    estado, compras, excedentes = "ok", 0., 0.
        else:
            estado, compras, excedentes = "sin_compromisos", None, None

        tipo = "proyeccion_historica" if is_future else ("mes_actual" if is_current else "real")
        meses.append({
            "month": m,
            "gen_mwh": gen_total,
            "gen_proyectada_mwh": gen_proy,
            "gen_proyectada_cierre": gen_cierre,
            "min_mwh": min_mwh,
            "max_mwh": max_mwh,
            "estado": estado,
            "tipo_datos": tipo,
            "dia_actual": dia_actual if is_current else None,
            "dias_restantes": (total_dias - dia_actual) if is_current else None,
            "compras_bolsa_mwh": compras,
            "excedentes_bolsa_mwh": excedentes,
            "exposicion_bolsa_duplicados_mwh": bolsa_dup_total if bolsa_dup_total > 0 else None,
            "plantas": plantas_mes,
            "n_plantas": len(plantas_mes),
            # Cumplimiento de plantas: registradas (n_plantas) vs esperadas para el mes.
            # plantas_esp se anula en meses no vigentes (finalizado/no_iniciado).
            "plantas_esperadas": plantas_esp,
            "valor_mwh": valor_mwh_contrato,
        })

    # Build proyectos list
    proyectos = []
    for (pid, sp, nombre), acc in proyectos_acc.items():
        proy_meses = []
        for idx in range(12):
            proy_meses.append({
                "month": idx + 1,
                "valor_mwh": acc["meses"][idx],
                "pct_despacho": acc["pct"],
                "es_duplicado": acc["is_dup"],
                "uso_del_recurso": acc.get("is_ur", False),
            })
        proyectos.append({
            "id": pid,
            "nombre": nombre,
            "sub_project": sp,
            "pct_despacho_rep": acc["pct"],
            "meses": proy_meses,
        })

    return meses, proyectos


def _query_contratos_venta(db: Session, year: int | None = None, month: int | None = None,
                           solo_relevantes: bool = True):
    """Retorna contratos PPA de venta (tipo_contrato != 'compra').

    Replica EXACTAMENTE el filtro que usa get_simulador para construir contratos_venta:
    primero obtiene todos los vigentes del año/mes dado, luego excluye compras.
    Si year es None usa el año en curso (para el endpoint anual-matriz).

    `solo_relevantes` se delega a _contratos_vigentes (ver allí la regla y por qué
    el default es True).
    """
    if year is None:
        year = date.today().year
    contratos_db = _contratos_vigentes(db, year, month, solo_relevantes=solo_relevantes)
    return [c for c in contratos_db if (c.tipo_contrato or "venta") != "compra"]


def _build_fetch_sets(gpm_por_contrato: dict, year: int, today) -> tuple:
    """Construye sets deduplicados de fetches a Unergy para todos los contratos.

    Replica la lógica de detección need_month/need_avg de get_anual pero sobre TODOS los
    contratos, devolviendo sets deduplicados:
      - need_month: set de (month, sub_project) para meses pasados/actuales
      - need_avg: set de sub_project para mes actual/futuros (proyección rolling avg)
      - need_range: set de (sub_project, eff_start, eff_end) para registros que
        estuvieron vigentes solo PARTE de un mes (pasado/actual) — su energía se
        calcula sumando los días reales, no prorrateando el total del mes.

    Clave: tuple order es (m, sp) igual que get_anual y month_cache[(m, sp)].
    """
    need_month: set = set()
    need_avg: set = set()
    need_range: set = set()
    for gpm in gpm_por_contrato.values():
        for m in range(1, 13):
            total_dias = calendar.monthrange(year, m)[1]
            first_day_m = date(year, m, 1)
            last_day_m = date(year, m, total_dias)
            is_current = (year == today.year and m == today.month)
            is_future = (year > today.year) or (year == today.year and m > today.month)
            for asic in gpm[m]:
                sp = asic.proyecto.sub_project if asic.proyecto else None
                if not sp:
                    continue
                if is_future:
                    need_avg.add(sp)
                    continue
                if is_current:
                    need_avg.add(sp)
                eff_start = max(first_day_m, asic.fecha_inicio) if asic.fecha_inicio else first_day_m
                eff_end = min(last_day_m, asic.fecha_fin) if asic.fecha_fin else last_day_m
                dias_activos = max(0, (eff_end - eff_start).days + 1)
                if 0 < dias_activos < total_dias:
                    need_range.add((sp, eff_start, eff_end))
                else:
                    need_month.add((m, sp))
    return need_month, need_avg, need_range


@router.get("/anual-matriz")
def get_anual_matriz(
    year: int = Query(..., ge=2020, le=2050),
    incluir_todos: bool = Query(False, description="Incluir contratos cuyo responsable está marcado como no relevante"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Matriz anual contrato->proyectos x 12 meses (solo venta). Deduplica fetches a Unergy.

    Por defecto oculta los contratos de responsables no relevantes; además de limpiar
    la vista, ahorra sus llamadas a la API de Unergy."""
    today = date.today()

    # 1. Contratos de venta (mismo universo que el simulador, sin restricción de mes)
    contratos = _query_contratos_venta(db, year, solo_relevantes=not incluir_todos)

    # 2. GESCON por contrato/mes + compromisos por contrato
    gpm_por_contrato: dict = {}
    comp_por_contrato: dict = {}
    for c in contratos:
        gpm_por_contrato[c.id] = {
            m: (_resolve_gescon(db, c.numero_codigo_contrato, year, m) if c.numero_codigo_contrato else [])
            for m in range(1, 13)
        }
        comp_por_contrato[c.id] = {
            r.mes: r for r in db.query(PPACompromisoEnergia).filter(
                PPACompromisoEnergia.contrato_id == c.id,
                PPACompromisoEnergia.año == year,
            ).all()
        }

    # 3. Set global de fetches deduplicado
    need_month, need_avg, need_range = _build_fetch_sets(gpm_por_contrato, year, today)

    # 4. Fetch único en paralelo (mismo patrón que get_anual)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}
    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_anual_matriz: %s", exc)
            token = None

        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res  # key = (m, sp) — misma orientación que get_anual

        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")

        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res

    # 5. Ensamblar por contrato
    out = []
    for c in contratos:
        meses, proyectos = _anual_meses_para_contrato(
            c, year, gpm_por_contrato[c.id], comp_por_contrato[c.id],
            month_cache, avg_cache, today, range_cache,
        )
        rollup = _rollup_cumplimiento(meses)
        n_plantas = max((len(gpm_por_contrato[c.id][m]) for m in range(1, 13)), default=0)
        out.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            **_responsable_payload(c),
            "meses": meses,
            "proyectos": proyectos,
            "n_plantas": n_plantas,
            **rollup,
        })
    return {"year": year, "contratos": out}


def _matriz_un_contrato(db: Session, contrato, year: int, today) -> dict:
    """Ensambla la fila de matriz anual de UN contrato (meses + proyectos + rollup).

    Hace los fetches a Unergy solo de las plantas de este contrato → ~2-3s, apto para
    carga progresiva fila por fila (evita el timeout del endpoint agregado con muchos contratos).
    """
    gpm = {
        m: (_resolve_gescon(db, contrato.numero_codigo_contrato, year, m) if contrato.numero_codigo_contrato else [])
        for m in range(1, 13)
    }
    comp_map = {
        r.mes: r for r in db.query(PPACompromisoEnergia).filter(
            PPACompromisoEnergia.contrato_id == contrato.id,
            PPACompromisoEnergia.año == year,
        ).all()
    }
    need_month, need_avg, need_range = _build_fetch_sets({contrato.id: gpm}, year, today)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}
    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in _matriz_un_contrato: %s", exc)
            token = None
        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res
        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")
        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res
    meses, proyectos = _anual_meses_para_contrato(contrato, year, gpm, comp_map, month_cache, avg_cache, today, range_cache)
    rollup = _rollup_cumplimiento(meses)
    n_plantas = max((len(gpm[m]) for m in range(1, 13)), default=0)
    return {
        "id": contrato.id,
        "nombre_interno": contrato.nombre_interno,
        "numero_codigo_contrato": contrato.numero_codigo_contrato,
        "comprador_nombre": contrato.comprador_nombre,
        **_responsable_payload(contrato),
        "meses": meses,
        "proyectos": proyectos,
        "n_plantas": n_plantas,
        **rollup,
    }


@router.get("/anual-matriz/contratos")
def get_anual_matriz_contratos(
    year: int = Query(..., ge=2020, le=2050),
    incluir_todos: bool = Query(False, description="Incluir contratos cuyo responsable está marcado como no relevante"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Lista ligera de contratos de venta para la matriz anual (sin generación → carga instantánea).
    El frontend pinta las filas y luego pide el detalle de cada una vía /anual-matriz/contrato/{id}."""
    contratos = _query_contratos_venta(db, year, solo_relevantes=not incluir_todos)
    return {
        "year": year,
        "contratos": [
            {
                "id": c.id,
                "nombre_interno": c.nombre_interno,
                "numero_codigo_contrato": c.numero_codigo_contrato,
                "comprador_nombre": c.comprador_nombre,
                **_responsable_payload(c),
            }
            for c in contratos
        ],
    }


@router.get("/anual-matriz/contrato/{contrato_id}")
def get_anual_matriz_contrato(
    contrato_id: int,
    year: int = Query(..., ge=2020, le=2050),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Detalle de matriz anual de un solo contrato (meses + proyectos + rollup). Carga progresiva."""
    contrato = db.query(PPAContrato).filter(PPAContrato.id == contrato_id).first()
    if not contrato:
        raise HTTPException(404, "Contrato PPA no encontrado")
    return _matriz_un_contrato(db, contrato, year, date.today())


@router.get("/ppa/{contrato_id}/anual")
def get_anual(
    contrato_id: int,
    year: int = Query(..., ge=2020, le=2050),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Annual chart data for a contract: 12 months of generation vs commitments."""
    today = date.today()

    contrato = db.query(PPAContrato).filter(PPAContrato.id == contrato_id).first()
    if not contrato:
        raise HTTPException(404, "Contrato PPA no encontrado")

    comp_map = {
        r.mes: r
        for r in db.query(PPACompromisoEnergia).filter(
            PPACompromisoEnergia.contrato_id == contrato_id,
            PPACompromisoEnergia.año == year,
        ).all()
    }

    gescon_per_month: dict = {}
    for m in range(1, 13):
        gescon_per_month[m] = (
            _resolve_gescon(db, contrato.numero_codigo_contrato, year, m)
            if contrato.numero_codigo_contrato else []
        )

    need_month, need_avg, need_range = _build_fetch_sets({contrato.id: gescon_per_month}, year, today)

    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}

    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_anual: %s", exc)
            token = None

        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res

        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")

        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res

    meses, _proyectos = _anual_meses_para_contrato(
        contrato, year, gescon_per_month, comp_map, month_cache, avg_cache, today, range_cache
    )

    return {
        "contrato": {
            "id": contrato.id,
            "nombre_interno": contrato.nombre_interno,
            "numero_codigo_contrato": contrato.numero_codigo_contrato,
            "comprador_nombre": contrato.comprador_nombre,
        },
        "year": year,
        "meses": meses,
    }


@router.get("/ppa/{contrato_id}/plantas-inscritas-por-mes")
def get_plantas_inscritas_por_mes(
    contrato_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Plantas INSCRITAS por año/mes = plantas registradas y despachando energía al
    contrato (asignaciones GESCON vigentes ese mes). Es el numerador del indicador de
    cumplimiento de plantas; la plataforma lo calcula (no se monta).

    Devuelve solo los periodos que tienen compromiso del contrato. Cuenta asignaciones
    GESCON desde BD (`_resolve_gescon`) sin traer generación de la API → barato.
    """
    contrato = db.query(PPAContrato).filter(PPAContrato.id == contrato_id).first()
    if not contrato:
        raise HTTPException(404, "Contrato PPA no encontrado")

    periodos = (
        db.query(PPACompromisoEnergia.año, PPACompromisoEnergia.mes)
        .filter(PPACompromisoEnergia.contrato_id == contrato_id)
        .order_by(PPACompromisoEnergia.año, PPACompromisoEnergia.mes)
        .all()
    )
    codigo = contrato.numero_codigo_contrato
    out = []
    for año, mes in periodos:
        n = len(_resolve_gescon(db, codigo, año, mes)) if codigo else 0
        out.append({"año": año, "mes": mes, "plantas_inscritas": n})
    return out


@router.get("/ppa/{contrato_id}")
def get_cumplimiento(
    contrato_id: int,
    year: int = Query(..., ge=2020, le=2050),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Retorna el cumplimiento energético de un contrato PPA para un mes dado.
    Consulta la API de Unergy en paralelo (hasta 10 plantas simultáneas).
    """
    # ── 1. Contrato ───────────────────────────────────────────
    contrato = db.query(PPAContrato).filter(PPAContrato.id == contrato_id).first()
    if not contrato:
        raise HTTPException(404, "Contrato PPA no encontrado")

    # ── 2. Compromisos y tarifa ───────────────────────────────
    compromiso = (
        db.query(PPACompromisoEnergia)
        .filter(
            PPACompromisoEnergia.contrato_id == contrato_id,
            PPACompromisoEnergia.año == year,
            PPACompromisoEnergia.mes == month,
        )
        .first()
    )
    tarifa_row = (
        db.query(PPATarifa)
        .filter(
            PPATarifa.contrato_id == contrato_id,
            PPATarifa.año == year,
            PPATarifa.mes == month,
        )
        .first()
    )

    # ── 3. Período ────────────────────────────────────────────
    today = date.today()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 4. GESCON assignments ─────────────────────────────────
    if not contrato.numero_codigo_contrato:
        assignments = []
    else:
        assignments = _resolve_gescon(db, contrato.numero_codigo_contrato, year, month)

    # ── 5. Generación desde API Unergy (paralelo) ─────────────
    plantas_data = []
    sin_api_id: list[str] = []

    plants_with_id = []
    plants_without_id = []
    for asic in assignments:
        proyecto = asic.proyecto
        nombre = proyecto.nombre_comercial if proyecto else f"Proyecto {asic.proyecto_id}"
        pct = float(asic.porcentaje_despacho or 0)
        is_dup = bool(asic.es_duplicado)
        if proyecto and proyecto.sub_project:
            plants_with_id.append({
                "nombre": nombre,
                "sub_project": proyecto.sub_project,
                "pct_despacho": pct,
                "es_duplicado": is_dup,
            })
        else:
            sin_api_id.append(nombre)
            plants_without_id.append({"nombre": nombre, "pct_despacho": pct, "es_duplicado": is_dup})

    if plants_with_id:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("No se pudo autenticar con la API de Unergy: %s", exc)
            token = None

        if token:
            def _fetch_one(p: dict) -> dict:
                if es_mes_futuro:
                    recent = _fetch_recent_avg(token, p["sub_project"])
                    avg = recent["avg_daily_mwh"]
                    gen_planta = round(avg * total_dias, 3) if avg is not None else None
                    n_registros = recent["n_days_used"]
                    ultimo_dia = None
                else:
                    gen = _fetch_month(token, p["sub_project"], year, month)
                    gen_planta = gen["mwh"]
                    n_registros = gen["n_records"]
                    ultimo_dia = gen["ultimo_dia"]
                gen_contrato = round(gen_planta * p["pct_despacho"], 3) if gen_planta is not None else None
                return {
                    "nombre": p["nombre"],
                    "sub_project": p["sub_project"],
                    "pct_despacho": p["pct_despacho"],
                    "es_duplicado": p["es_duplicado"],
                    "gen_planta_mwh": gen_planta,
                    "gen_contrato_mwh": gen_contrato,
                    "n_registros": n_registros,
                    "ultimo_dia": ultimo_dia,
                    "sin_datos": gen_planta is None,
                    "sin_api_id": False,
                }

            max_workers = min(len(plants_with_id), 10)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_fetch_one, plants_with_id))
            plantas_data.extend(results)
        else:
            for p in plants_with_id:
                plantas_data.append({
                    "nombre": p["nombre"], "sub_project": p["sub_project"],
                    "pct_despacho": p["pct_despacho"], "es_duplicado": p["es_duplicado"],
                    "gen_planta_mwh": None, "gen_contrato_mwh": None,
                    "n_registros": 0, "ultimo_dia": None, "sin_datos": True, "sin_api_id": False,
                })

    for p in plants_without_id:
        plantas_data.append({
            "nombre": p["nombre"],
            "sub_project": None,
            "pct_despacho": p["pct_despacho"],
            "es_duplicado": p["es_duplicado"],
            "gen_planta_mwh": None,
            "gen_contrato_mwh": None,
            "n_registros": 0,
            "ultimo_dia": None,
            "sin_datos": True,
            "sin_api_id": True,
        })

    # ── 6. Totales ────────────────────────────────────────────
    # gen_total cuenta TODO lo suministrado al contrato (real + compra en bolsa);
    # bolsa_dup es el subconjunto informativo proveniente de duplicados (origen bolsa).
    gen_total = round(
        sum(p["gen_contrato_mwh"] for p in plantas_data if p["gen_contrato_mwh"] is not None),
        3,
    )
    bolsa_dup = round(
        sum(p["gen_contrato_mwh"] for p in plantas_data if p["gen_contrato_mwh"] is not None and p["es_duplicado"]),
        3,
    )
    plantas_sin_datos = [p["nombre"] for p in plantas_data if p["sin_datos"]]

    # Proyección lineal al fin de mes (solo mes actual)
    if es_mes_actual and dia_actual > 0 and gen_total > 0:
        gen_proyectada = round(gen_total * total_dias / dia_actual, 3)
    else:
        gen_proyectada = gen_total

    # Día del último dato más antiguo entre plantas con datos
    dias_ultimo_dato = [p["ultimo_dia"] for p in plantas_data if p["ultimo_dia"] is not None]
    dia_min_datos = min(dias_ultimo_dato) if dias_ultimo_dato else None
    dia_max_datos = max(dias_ultimo_dato) if dias_ultimo_dato else None

    # ── 7. Balance ────────────────────────────────────────────
    min_mwh: Optional[float] = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None
    max_mwh: Optional[float] = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else None

    val_balance = gen_proyectada if (es_mes_actual or es_mes_futuro) else gen_total

    if min_mwh is not None or max_mwh is not None:
        effective_min = min_mwh if min_mwh is not None else 0.0
        effective_max = max_mwh if max_mwh is not None else float('inf')
        if val_balance < effective_min:
            estado = "deficit"
        elif val_balance > effective_max:
            estado = "excedente"
        else:
            estado = "ok"
        compras = round(max(0.0, effective_min - val_balance), 3)
        excedentes = round(max(0.0, val_balance - effective_max), 3) if max_mwh is not None else 0.0
        margen = round(effective_max - val_balance, 3) if max_mwh is not None else None
    else:
        estado = "sin_compromisos"
        compras = excedentes = margen = None

    # ── 8. Valoración COP con precios de bolsa ───────────────
    bolsa = _get_bolsa_avg(db, year, month)
    tarifa_ppa = float(tarifa_row.tarifa) if tarifa_row and tarifa_row.tarifa else None
    precio_bolsa = bolsa["precio_promedio"]

    valoracion = None
    if precio_bolsa is not None and (compras or excedentes):
        compras_cop = round(compras * 1000 * precio_bolsa, 0) if compras else 0
        excedentes_cop = round(excedentes * 1000 * precio_bolsa, 0) if excedentes else 0
        delta_vs_ppa = None
        if tarifa_ppa and compras:
            delta_vs_ppa = round(compras * 1000 * (precio_bolsa - tarifa_ppa), 0)
        valoracion = {
            "precio_bolsa_avg_cop_kwh": precio_bolsa,
            "precio_bolsa_min_cop_kwh": bolsa["precio_min"],
            "precio_bolsa_max_cop_kwh": bolsa["precio_max"],
            "precio_escasez_cop_kwh": bolsa["precio_escasez"],
            "dias_con_precios": bolsa["dias_con_datos"],
            "tarifa_ppa_cop_kwh": tarifa_ppa,
            "compras_bolsa_cop": compras_cop,
            "excedentes_bolsa_cop": excedentes_cop,
            "sobrecosto_vs_ppa_cop": delta_vs_ppa,
        }

    return {
        "contrato": {
            "id": contrato.id,
            "nombre_interno": contrato.nombre_interno,
            "numero_codigo_contrato": contrato.numero_codigo_contrato,
            "comprador_nombre": contrato.comprador_nombre,
            "fecha_inicio": contrato.fecha_inicio.isoformat() if contrato.fecha_inicio else None,
            "fecha_fin": contrato.fecha_fin.isoformat() if contrato.fecha_fin else None,
        },
        "periodo": {
            "year": year,
            "month": month,
            "dia_actual": dia_actual,
            "dias_mes": total_dias,
            "es_mes_actual": es_mes_actual,
            "es_mes_futuro": es_mes_futuro,
            "tipo_datos": "proyeccion_historica" if es_mes_futuro else ("proyeccion_lineal" if es_mes_actual else "real"),
            "dia_min_datos": dia_min_datos,
            "dia_max_datos": dia_max_datos,
        },
        "compromisos": {
            "energia_minima_mwh": min_mwh,
            "energia_maxima_mwh": max_mwh,
        },
        "generacion": {
            "gen_total_mwh": gen_total,
            "gen_proyectada_mwh": gen_proyectada,
            "exposicion_bolsa_duplicados_mwh": bolsa_dup if bolsa_dup > 0 else None,
            "tarifa_cop_kwh": tarifa_ppa,
            "plantas": plantas_data,
            "plantas_sin_datos": plantas_sin_datos,
            "sin_api_id": sin_api_id,
            "n_plantas_activas": len(assignments),
        },
        "balance": {
            "estado": estado,
            "compras_bolsa_mwh": compras,
            "excedentes_bolsa_mwh": excedentes,
            "margen_mwh": margen,
        },
        "valoracion_bolsa": valoracion,
    }


# ── Descubrimientos ─────────────────────────────────────────────────────────

@router.get("/descubrimientos")
def get_descubrimientos(
    year: int = Query(..., ge=2020, le=2050),
    month_from: int = Query(1, ge=1, le=12),
    month_to: int = Query(12, ge=1, le=12),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Exposición financiera por descubrimientos de energía en bolsa.
    Cruza deltas MWh (cumplimiento) × precio promedio bolsa del mes.
    Solo usa datos de DB — no llama la API de Unergy.
    """
    # Sin filtro de responsable: /descubrimientos no es una vista de
    # /mem/cumplimiento y su chamba es destapar exposición, no esconderla.
    contratos = _contratos_vigentes(db, year, solo_relevantes=False)

    meses_data = []
    gran_total_compras_cop = 0.0
    gran_total_excedentes_cop = 0.0
    gran_total_compras_mwh = 0.0
    gran_total_excedentes_mwh = 0.0

    for m in range(month_from, month_to + 1):
        bolsa = _get_bolsa_avg(db, year, m)
        precio = bolsa["precio_promedio"]

        compromisos = {
            c.contrato_id: c
            for c in db.query(PPACompromisoEnergia).filter(
                PPACompromisoEnergia.año == year,
                PPACompromisoEnergia.mes == m,
            ).all()
        }

        tarifas = {
            t.contrato_id: float(t.tarifa)
            for t in db.query(PPATarifa).filter(
                PPATarifa.contrato_id.in_([c.id for c in contratos]),
                PPATarifa.año == year,
                PPATarifa.mes == m,
            ).all()
            if t.tarifa is not None
        }

        # Get real generation from generacion_diaria (monthly sum)
        gen_rows = db.execute(text("""
            SELECT p.id as proyecto_id, SUM(g.kwh_real) / 1000.0 as mwh
            FROM generacion_diaria g
            JOIN proyectos p ON g.proyecto_id = p.id
            WHERE EXTRACT(YEAR FROM g.fecha) = :year
              AND EXTRACT(MONTH FROM g.fecha) = :month
              AND g.kwh_real IS NOT NULL
            GROUP BY p.id
        """), {"year": year, "month": m}).fetchall()
        gen_by_proyecto = {int(r.proyecto_id): float(r.mwh) for r in gen_rows}

        mes_compras_cop = 0.0
        mes_excedentes_cop = 0.0
        mes_compras_mwh = 0.0
        mes_excedentes_mwh = 0.0
        contratos_mes = []

        for c in contratos:
            comp = compromisos.get(c.id)
            if not comp:
                continue
            min_mwh = float(comp.energia_minima) if comp.energia_minima is not None else None
            max_mwh = float(comp.energia_maxima) if comp.energia_maxima is not None else None
            if min_mwh is None or max_mwh is None:
                continue

            # Sum generation from GESCON-assigned plants
            if c.numero_codigo_contrato:
                assignments = _resolve_gescon(db, c.numero_codigo_contrato, year, m)
            else:
                assignments = []

            gen_assigned = 0.0
            for asic in assignments:
                if asic.proyecto_id and asic.proyecto_id in gen_by_proyecto:
                    pct = float(asic.porcentaje_despacho or 0)
                    gen_assigned += gen_by_proyecto[asic.proyecto_id] * pct

            gen_assigned = round(gen_assigned, 3)
            compras_mwh = round(max(0.0, min_mwh - gen_assigned), 3)
            excedentes_mwh = round(max(0.0, gen_assigned - max_mwh), 3)

            tarifa_ppa = tarifas.get(c.id)
            compras_cop = 0.0
            excedentes_cop = 0.0
            sobrecosto = None

            if precio is not None:
                compras_cop = round(compras_mwh * 1000 * precio, 0)
                excedentes_cop = round(excedentes_mwh * 1000 * precio, 0)
                if tarifa_ppa and compras_mwh > 0:
                    sobrecosto = round(compras_mwh * 1000 * (precio - tarifa_ppa), 0)

            if compras_mwh > 0 or excedentes_mwh > 0:
                mes_compras_cop += compras_cop
                mes_excedentes_cop += excedentes_cop
                mes_compras_mwh += compras_mwh
                mes_excedentes_mwh += excedentes_mwh
                contratos_mes.append({
                    "contrato_id": c.id,
                    "nombre": c.nombre_interno or c.numero_codigo_contrato,
                    "comprador": c.comprador_nombre,
                    "min_mwh": min_mwh,
                    "max_mwh": max_mwh,
                    "gen_asignada_mwh": gen_assigned,
                    "compras_mwh": compras_mwh,
                    "excedentes_mwh": excedentes_mwh,
                    "compras_cop": compras_cop,
                    "excedentes_cop": excedentes_cop,
                    "sobrecosto_vs_ppa_cop": sobrecosto,
                    "tarifa_ppa_kwh": tarifa_ppa,
                })

        gran_total_compras_cop += mes_compras_cop
        gran_total_excedentes_cop += mes_excedentes_cop
        gran_total_compras_mwh += mes_compras_mwh
        gran_total_excedentes_mwh += mes_excedentes_mwh

        meses_data.append({
            "month": m,
            "precio_bolsa_avg": precio,
            "dias_con_precios": bolsa["dias_con_datos"],
            "compras_mwh": round(mes_compras_mwh, 3),
            "excedentes_mwh": round(mes_excedentes_mwh, 3),
            "compras_cop": round(mes_compras_cop, 0),
            "excedentes_cop": round(mes_excedentes_cop, 0),
            "contratos": contratos_mes,
        })

    return {
        "year": year,
        "month_from": month_from,
        "month_to": month_to,
        "totales": {
            "compras_bolsa_mwh": round(gran_total_compras_mwh, 3),
            "excedentes_bolsa_mwh": round(gran_total_excedentes_mwh, 3),
            "compras_bolsa_cop": round(gran_total_compras_cop, 0),
            "excedentes_bolsa_cop": round(gran_total_excedentes_cop, 0),
            "exposicion_neta_cop": round(gran_total_compras_cop - gran_total_excedentes_cop, 0),
        },
        "meses": meses_data,
    }


# ── Cumplimiento Mensual — Persistencia ──────────────────────────────────────


def _build_cumplimiento_out(row: CumplimientoMensual) -> dict:
    """Serialize a CumplimientoMensual row to dict matching CumplimientoMensualOut."""
    contrato = row.contrato_ppa
    return {
        "id": row.id,
        "contrato_ppa_id": row.contrato_ppa_id,
        "proyecto_id": row.proyecto_id,
        "anio": row.anio,
        "mes": row.mes,
        "gen_total_mwh": float(row.gen_total_mwh) if row.gen_total_mwh is not None else None,
        "compromiso_mwh": float(row.compromiso_mwh) if row.compromiso_mwh is not None else None,
        "compras_bolsa_mwh": float(row.compras_bolsa_mwh) if row.compras_bolsa_mwh is not None else None,
        "excedentes_bolsa_mwh": float(row.excedentes_bolsa_mwh) if row.excedentes_bolsa_mwh is not None else None,
        "precio_bolsa_promedio": float(row.precio_bolsa_promedio) if row.precio_bolsa_promedio is not None else None,
        "compras_bolsa_cop": float(row.compras_bolsa_cop) if row.compras_bolsa_cop is not None else None,
        "excedentes_bolsa_cop": float(row.excedentes_bolsa_cop) if row.excedentes_bolsa_cop is not None else None,
        "estado": row.estado,
        "tarifa_ppa_cop_mwh": float(row.tarifa_ppa_cop_mwh) if row.tarifa_ppa_cop_mwh is not None else None,
        "valoracion_contrato_cop": float(row.valoracion_contrato_cop) if row.valoracion_contrato_cop is not None else None,
        "liquidacion_id": row.liquidacion_id,
        "contrato_nombre": contrato.nombre_interno if contrato else None,
        "comprador_nombre": contrato.comprador_nombre if contrato else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/cerrar-periodo")
def cerrar_periodo(
    body: CerrarPeriodoRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Cierra un periodo (anio, mes) para todos los contratos PPA activos.

    Calcula el cumplimiento usando la misma logica que /ppa/resumen
    (generacion real desde Unergy API + compromisos de la DB),
    y persiste un snapshot en cumplimiento_mensual.

    Si ya existen registros para el periodo, los actualiza (upsert).
    """
    year, month = body.anio, body.mes
    today = date.today()
    es_mes_actual = year == today.year and month == today.month
    es_mes_futuro = (year > today.year) or (year == today.year and month > today.month)
    total_dias = calendar.monthrange(year, month)[1]
    dia_actual = today.day if es_mes_actual else total_dias

    # ── 1. Contratos y compromisos ────────────────────────────────────────────
    # Sin filtro de responsable A PROPÓSITO: esto PERSISTE el cierre mensual. Dejar
    # contratos fuera cambiaría el histórico guardado, no solo lo que se ve en
    # pantalla — y marcar un responsable como no relevante borraría su cierre.
    contratos = _contratos_vigentes(db, year, month, solo_relevantes=False)
    if not contratos:
        raise HTTPException(404, "No hay contratos PPA registrados")

    compromisos_map = {
        c.contrato_id: c
        for c in db.query(PPACompromisoEnergia).filter(
            PPACompromisoEnergia.año == year,
            PPACompromisoEnergia.mes == month,
        ).all()
    }

    tarifas_map = {
        t.contrato_id: float(t.tarifa)
        for t in db.query(PPATarifa).filter(
            PPATarifa.año == year,
            PPATarifa.mes == month,
        ).all()
        if t.tarifa is not None
    }

    # ── 2. GESCON assignments ─────────────────────────────────────────────────
    contrato_assignments: dict[int, list] = {}
    for c in contratos:
        if c.numero_codigo_contrato:
            contrato_assignments[c.id] = _resolve_gescon(db, c.numero_codigo_contrato, year, month)
        else:
            contrato_assignments[c.id] = []

    # ── 3. Sub-projects unicos ────────────────────────────────────────────────
    sp_set: set[str] = set()
    for assignments in contrato_assignments.values():
        for asic in assignments:
            if asic.proyecto and asic.proyecto.sub_project:
                sp_set.add(asic.proyecto.sub_project)

    # ── 4. Generacion en paralelo ─────────────────────────────────────────────
    gen_cache: dict[str, dict] = {}
    if sp_set:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in cerrar_periodo: %s", exc)
            raise HTTPException(503, "No se pudo autenticar con la API de Unergy")

        sp_list = list(sp_set)

        def _fetch_sp(sp: str) -> tuple:
            if es_mes_futuro:
                recent = _fetch_recent_avg(token, sp)
                avg = recent["avg_daily_mwh"]
                mwh = round(avg * total_dias, 3) if avg is not None else None
                return sp, {"mwh": mwh, "n_records": recent["n_days_used"], "ultimo_dia": None}
            return sp, _fetch_month(token, sp, year, month)

        max_workers = min(len(sp_list), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for sp, result in pool.map(_fetch_sp, sp_list):
                gen_cache[sp] = result

    # ── 5. Precios de bolsa ───────────────────────────────────────────────────
    bolsa = _get_bolsa_avg(db, year, month)
    precio_bolsa = bolsa["precio_promedio"]

    # ── 6. Calculo y persistencia por contrato ────────────────────────────────
    registros: list[dict] = []
    n_deficit = 0
    n_cumplidos = 0

    for c in contratos:
        assignments = contrato_assignments[c.id]
        compromiso = compromisos_map.get(c.id)

        gen_total_c = 0.0
        for asic in assignments:
            proyecto = asic.proyecto
            pct = float(asic.porcentaje_despacho or 0)
            if proyecto and proyecto.sub_project:
                gd = gen_cache.get(proyecto.sub_project, {"mwh": None})
                gp = gd.get("mwh")
                if gp is not None:
                    gen_total_c += gp * pct

        gen_total_c = round(gen_total_c, 3)

        # Linear projection for current month
        gen_val = gen_total_c
        if es_mes_actual and dia_actual > 0 and gen_total_c > 0:
            gen_val = round(gen_total_c * total_dias / dia_actual, 3)

        min_mwh = float(compromiso.energia_minima) if compromiso and compromiso.energia_minima is not None else None

        compras_mwh = None
        excedentes_mwh = None
        compras_cop = None
        excedentes_cop = None

        if min_mwh is not None:
            compras_mwh = round(max(0.0, min_mwh - gen_val), 3)
            max_mwh_val = float(compromiso.energia_maxima) if compromiso and compromiso.energia_maxima is not None else min_mwh
            excedentes_mwh = round(max(0.0, gen_val - max_mwh_val), 3)

            if precio_bolsa is not None:
                compras_cop = round(compras_mwh * 1000 * precio_bolsa, 2)
                excedentes_cop = round(excedentes_mwh * 1000 * precio_bolsa, 2)

            if compras_mwh > 0:
                n_deficit += 1
            else:
                n_cumplidos += 1

        tarifa_ppa = tarifas_map.get(c.id)

        # Valoracion del contrato: generacion * tarifa PPA (COP/kWh -> COP)
        valoracion_cop = None
        if tarifa_ppa is not None and gen_val > 0:
            valoracion_cop = round(gen_val * 1000 * tarifa_ppa, 2)

        # Upsert
        existing = (
            db.query(CumplimientoMensual)
            .filter(
                CumplimientoMensual.contrato_ppa_id == c.id,
                CumplimientoMensual.anio == year,
                CumplimientoMensual.mes == month,
            )
            .first()
        )

        if existing:
            # Don't overwrite facturado records
            if existing.estado == EstadoCumplimientoEnum.facturado:
                registros.append(_build_cumplimiento_out(existing))
                continue
            existing.gen_total_mwh = gen_total_c
            existing.compromiso_mwh = min_mwh
            existing.compras_bolsa_mwh = compras_mwh
            existing.excedentes_bolsa_mwh = excedentes_mwh
            existing.precio_bolsa_promedio = precio_bolsa
            existing.compras_bolsa_cop = compras_cop
            existing.excedentes_bolsa_cop = excedentes_cop
            existing.tarifa_ppa_cop_mwh = tarifa_ppa
            existing.valoracion_contrato_cop = valoracion_cop
            existing.estado = EstadoCumplimientoEnum.cerrado
            db.flush()
            registros.append(_build_cumplimiento_out(existing))
        else:
            new_row = CumplimientoMensual(
                contrato_ppa_id=c.id,
                proyecto_id=None,
                anio=year,
                mes=month,
                gen_total_mwh=gen_total_c,
                compromiso_mwh=min_mwh,
                compras_bolsa_mwh=compras_mwh,
                excedentes_bolsa_mwh=excedentes_mwh,
                precio_bolsa_promedio=precio_bolsa,
                compras_bolsa_cop=compras_cop,
                excedentes_bolsa_cop=excedentes_cop,
                estado=EstadoCumplimientoEnum.cerrado,
                tarifa_ppa_cop_mwh=tarifa_ppa,
                valoracion_contrato_cop=valoracion_cop,
            )
            db.add(new_row)
            db.flush()
            registros.append(_build_cumplimiento_out(new_row))

    db.commit()

    return {
        "anio": year,
        "mes": month,
        "contratos_procesados": len(contratos),
        "contratos_con_deficit": n_deficit,
        "contratos_cumplidos": n_cumplidos,
        "registros": registros,
    }


@router.get("/historico")
def historico_cumplimiento(
    contrato_id: Optional[int] = Query(None),
    proyecto_id: Optional[int] = Query(None),
    anio: Optional[int] = Query(None, ge=2020, le=2050),
    mes: Optional[int] = Query(None, ge=1, le=12),
    estado: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Lista registros historicos de cumplimiento con filtros opcionales."""
    q = (
        db.query(CumplimientoMensual)
        .join(PPAContrato, CumplimientoMensual.contrato_ppa_id == PPAContrato.id)
    )

    if contrato_id is not None:
        q = q.filter(CumplimientoMensual.contrato_ppa_id == contrato_id)
    if proyecto_id is not None:
        q = q.filter(CumplimientoMensual.proyecto_id == proyecto_id)
    if anio is not None:
        q = q.filter(CumplimientoMensual.anio == anio)
    if mes is not None:
        q = q.filter(CumplimientoMensual.mes == mes)
    if estado is not None:
        q = q.filter(CumplimientoMensual.estado == estado)

    rows = q.order_by(
        CumplimientoMensual.anio.desc(),
        CumplimientoMensual.mes.desc(),
        CumplimientoMensual.contrato_ppa_id,
    ).all()

    return [_build_cumplimiento_out(r) for r in rows]


@router.get("/historico/{record_id}")
def historico_detalle(
    record_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Detalle de un registro de cumplimiento."""
    row = (
        db.query(CumplimientoMensual)
        .join(PPAContrato, CumplimientoMensual.contrato_ppa_id == PPAContrato.id)
        .filter(CumplimientoMensual.id == record_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "Registro de cumplimiento no encontrado")
    return _build_cumplimiento_out(row)


@router.post("/historico/{record_id}/facturar")
def facturar_cumplimiento(
    record_id: int,
    body: FacturarRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Marca un registro de cumplimiento como facturado y lo vincula a una liquidacion."""
    row = db.query(CumplimientoMensual).filter(CumplimientoMensual.id == record_id).first()
    if not row:
        raise HTTPException(404, "Registro de cumplimiento no encontrado")
    if row.estado == EstadoCumplimientoEnum.facturado:
        raise HTTPException(400, "El registro ya esta facturado")

    row.estado = EstadoCumplimientoEnum.facturado
    if body.liquidacion_id is not None:
        row.liquidacion_id = body.liquidacion_id
    db.commit()
    db.refresh(row)
    return _build_cumplimiento_out(row)


@router.get("/diagnostico")
def diagnostico_enlaces(
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Diagnostic: dump all contract→GESCON→project→sub_project mappings."""
    from app.models.proyectos import Proyecto
    today = date.today()
    year, month = today.year, today.month

    contratos = (
        db.query(PPAContrato)
        .filter(PPAContrato.deleted_at.is_(None))
        .order_by(PPAContrato.nombre_interno.nullslast(), PPAContrato.id)
        .all()
    )

    result = []
    for c in contratos:
        gescon_raw = []
        resolved = []
        if c.numero_codigo_contrato:
            raw_records = (
                db.query(AsicSolicitud)
                .options(joinedload(AsicSolicitud.proyecto))
                .filter(AsicSolicitud.contrato_interno == c.numero_codigo_contrato)
                .order_by(AsicSolicitud.fecha_solicitud.asc().nullsfirst())
                .all()
            )
            for r in raw_records:
                gescon_raw.append({
                    "id": r.id,
                    "tipo": r.tipo_solicitud.value if r.tipo_solicitud else None,
                    "estado": r.estado_solicitud.value if r.estado_solicitud else None,
                    "codigo_sic": r.codigo_sic_contrato,
                    "proyecto_id": r.proyecto_id,
                    "planta": r.proyecto.nombre_comercial if r.proyecto else None,
                    "sub_project": r.proyecto.sub_project if r.proyecto else None,
                    "pct_despacho": float(r.porcentaje_despacho) if r.porcentaje_despacho else None,
                    "es_duplicado": bool(r.es_duplicado),
                    "reemplaza_anterior": bool(r.reemplaza_anterior),
                    "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
                    "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
                })
            resolved_asics = _resolve_gescon(db, c.numero_codigo_contrato, year, month)
            for a in resolved_asics:
                resolved.append({
                    "asic_id": a.id,
                    "planta": a.proyecto.nombre_comercial if a.proyecto else None,
                    "sub_project": a.proyecto.sub_project if a.proyecto else None,
                    "pct_despacho": float(a.porcentaje_despacho) if a.porcentaje_despacho else None,
                    "es_duplicado": bool(a.es_duplicado),
                })

        result.append({
            "contrato_id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador": c.comprador_nombre,
            "tipo": c.tipo_contrato or "venta",
            "gescon_raw": gescon_raw,
            "gescon_resolved": resolved,
            "n_plantas_activas": len(resolved),
        })

    all_projects = (
        db.query(Proyecto)
        .filter(Proyecto.sub_project.isnot(None))
        .order_by(Proyecto.nombre_comercial)
        .all()
    )
    projects_info = [
        {"id": p.id, "nombre": p.nombre_comercial, "sub_project": p.sub_project, "estado": p.estado.value if p.estado else None}
        for p in all_projects
    ]

    return {"contratos": result, "proyectos_con_sub_project": projects_info}


@router.post("/fix-enlaces")
def fix_enlaces(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Auto-fix missing GESCON assignments for known contracts.
    Creates AsicSolicitud records where they're missing.
    """
    if current_user.email != "juanjose@unergy.io":
        raise HTTPException(403, "Solo el admin puede ejecutar esta acción")

    from app.models.proyectos import Proyecto
    import unicodedata

    def norm(s):
        s = unicodedata.normalize("NFD", str(s or ""))
        return "".join(c for c in s if unicodedata.category(c) != "Mn").strip().lower()

    projects = db.query(Proyecto).all()
    proj_by_norm = {norm(p.nombre_comercial): p for p in projects}

    FIXES = [
        {
            "contrato_interno": "MNRNEU-2024-006",
            "nombre_interno": "NEU II - Ibirico",
            "plantas": [
                {"nombre_norm": "mgs 0021 ibirico", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2025-03-01", "fecha_fin": "2040-12-31"},
            ],
        },
        {
            "contrato_interno": "OC.UNER-063-2025",
            "nombre_interno": "Nitro Energy",
            "plantas": [
                {"nombre_norm": "mgs 0040 cacica", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2026-01-01", "fecha_fin": "2040-12-31"},
                {"nombre_norm": "mgs 0041 piloneras", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2026-01-01", "fecha_fin": "2040-12-31"},
            ],
        },
    ]

    actions = []

    for fix in FIXES:
        contrato_code = fix["contrato_interno"]
        for planta_def in fix["plantas"]:
            proj = proj_by_norm.get(planta_def["nombre_norm"])
            if not proj:
                actions.append({"action": "skip", "reason": f"Proyecto '{planta_def['nombre_norm']}' no encontrado en BD",
                                "contrato": contrato_code})
                continue

            existing = (
                db.query(AsicSolicitud)
                .filter(
                    AsicSolicitud.contrato_interno == contrato_code,
                    AsicSolicitud.proyecto_id == proj.id,
                    AsicSolicitud.tipo_solicitud == TipoSolicitudAsicEnum.registro,
                    AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
                )
                .first()
            )

            if existing:
                actions.append({"action": "exists", "contrato": contrato_code,
                                "planta": proj.nombre_comercial, "asic_id": existing.id})
                continue

            new_asic = AsicSolicitud(
                proyecto_id=proj.id,
                contrato_interno=contrato_code,
                tipo_solicitud=TipoSolicitudAsicEnum.registro,
                estado_solicitud=EstadoSolicitudAsicEnum.publicado,
                fecha_inicio=date.fromisoformat(planta_def["fecha_inicio"]),
                fecha_fin=date.fromisoformat(planta_def["fecha_fin"]),
                porcentaje_despacho=planta_def["pct"],
                es_duplicado=planta_def["duplicado"],
                reemplaza_anterior=False,
                tipo_mercado="No regulado",
                nombre_interno=fix["nombre_interno"],
            )
            db.add(new_asic)
            db.flush()
            actions.append({"action": "created", "contrato": contrato_code,
                            "planta": proj.nombre_comercial, "sub_project": proj.sub_project,
                            "asic_id": new_asic.id})

    # Fix Uruaco duplicate in KLIK
    klik_uruaco = (
        db.query(AsicSolicitud)
        .options(joinedload(AsicSolicitud.proyecto))
        .filter(
            AsicSolicitud.contrato_interno == "OM-UNERGY-010-2025",
            AsicSolicitud.estado_solicitud == EstadoSolicitudAsicEnum.publicado,
        )
        .all()
    )

    uruaco_records = [r for r in klik_uruaco if r.proyecto and norm(r.proyecto.nombre_comercial).find("uruaco") >= 0]
    if len(uruaco_records) > 1:
        for dup in uruaco_records[1:]:
            actions.append({"action": "delete_duplicate", "contrato": "OM-UNERGY-010-2025",
                            "planta": dup.proyecto.nombre_comercial if dup.proyecto else None,
                            "asic_id": dup.id})
            db.delete(dup)
    elif len(uruaco_records) == 1 and uruaco_records[0].es_duplicado:
        uruaco_records[0].es_duplicado = False
        actions.append({"action": "unflag_duplicate", "contrato": "OM-UNERGY-010-2025",
                        "planta": uruaco_records[0].proyecto.nombre_comercial,
                        "asic_id": uruaco_records[0].id})
    elif not uruaco_records:
        proj_uruaco = proj_by_norm.get("minigranja solar uruaco")
        if proj_uruaco:
            new_asic = AsicSolicitud(
                proyecto_id=proj_uruaco.id,
                contrato_interno="OM-UNERGY-010-2025",
                tipo_solicitud=TipoSolicitudAsicEnum.registro,
                estado_solicitud=EstadoSolicitudAsicEnum.publicado,
                fecha_inicio=date(2026, 4, 1),
                fecha_fin=date(2041, 3, 31),
                porcentaje_despacho=1.0,
                es_duplicado=False,
                reemplaza_anterior=False,
                tipo_mercado="No regulado",
                nombre_interno="KLIK - Uruaco",
            )
            db.add(new_asic)
            db.flush()
            actions.append({"action": "created", "contrato": "OM-UNERGY-010-2025",
                            "planta": proj_uruaco.nombre_comercial, "sub_project": proj_uruaco.sub_project,
                            "asic_id": new_asic.id})
        else:
            actions.append({"action": "skip", "reason": "Uruaco no encontrado en BD",
                            "contrato": "OM-UNERGY-010-2025"})

    db.commit()
    return {"status": "ok", "actions": actions}


# ═══════════════════════════════════════════════════════════════════════════════
# Panel anual — una sola llamada con todo lo que dibuja la pestaña Cumplimiento
# ═══════════════════════════════════════════════════════════════════════════════
#
# Existe para consumidores externos (paneles de gerencia) que necesitan replicar
# la gráfica de /mem/cumplimiento sin reimplementar lógica de negocio.
#
# Por qué no basta con los endpoints que ya había:
#   - /cumplimiento/ppa/{id}/anual devuelve UN contrato. El consolidado obligaba a
#     llamarlo N veces y sumar del lado del cliente (~60 líneas en el frontend),
#     con dos consecuencias: N tokens + fetches repetidos de plantas compartidas
#     entre contratos (los duplicados), y una regla de negocio duplicada que se
#     desincroniza en cuanto se toca de este lado.
#   - Este endpoint reusa la maquinaria de /anual-matriz, que deduplica los
#     fetches a la API de Unergy sobre TODOS los contratos a la vez.

_PANEL_CACHE: dict[str, tuple[float, dict]] = {}   # key → (monotonic_ts, payload)
PANEL_CACHE_TTL = 900   # 15 min. La generación de meses cerrados no cambia; la del
                        # mes en curso se refresca en el siguiente ciclo.


def _panel_cache_get(key: str) -> dict | None:
    entry = _PANEL_CACHE.get(key)
    if entry and (_time.monotonic() - entry[0]) < PANEL_CACHE_TTL:
        return entry[1]
    return None


def _panel_cache_set(key: str, data: dict) -> None:
    _PANEL_CACHE[key] = (_time.monotonic(), data)


def _sumar_opcional(valores: list) -> float | None:
    """Suma ignorando None. Devuelve None si TODOS son None.

    Distingue "nadie tiene compromiso" (None) de "el compromiso es cero" (0.0).
    Un contrato sin compromiso cargado no debe arrastrar el consolidado a cero.
    """
    presentes = [v for v in valores if v is not None]
    return round(sum(presentes), 3) if presentes else None


def _consolidar_meses(meses_por_contrato: list[list[dict]]) -> list[dict]:
    """Suma los 12 meses de N contratos en una sola serie consolidada.

    Función pura: recibe las listas de meses ya construidas por
    `_anual_meses_para_contrato` y no hace I/O.

    Reglas (equivalentes a `loadConsolidado()` del frontend, más los casos que
    aquél no contempla):
      - min/max se suman solo entre los contratos que los tienen. Si ninguno
        tiene, el mes queda en None, no en 0.
      - El valor que se compara contra el compromiso es `valor_mwh`, que el
        backend ya resolvió por mes (real / cierre proyectado / proyección).
        Los meses en que un contrato no está vigente traen valor_mwh=None y por
        lo tanto no aportan — que es justo lo que corresponde.
      - `compras_bolsa_mwh` es el déficit DEL CONSOLIDADO (lo que dibuja la
        gráfica). `suma_compras_bolsa_mwh` es la suma de los déficits de cada
        contrato, que es el número operativo real: los contratos no se netean
        entre sí, un excedente en uno no cubre el faltante de otro.
    """
    if not meses_por_contrato:
        return []

    consolidado = []
    for i in range(12):
        fila = [c[i] for c in meses_por_contrato if i < len(c)]
        if not fila:
            continue

        min_mwh = _sumar_opcional([m.get("min_mwh") for m in fila])
        max_mwh = _sumar_opcional([m.get("max_mwh") for m in fila])
        valor   = _sumar_opcional([m.get("valor_mwh") for m in fila])
        gen     = round(sum(m.get("gen_mwh") or 0 for m in fila), 3)

        estado, compras, excedentes = "sin_compromisos", None, None
        if min_mwh is not None or max_mwh is not None:
            if valor is None:
                estado = "sin_datos"
            else:
                efectivo_min = min_mwh if min_mwh is not None else 0.0
                if valor < efectivo_min:
                    estado, compras, excedentes = "deficit", round(efectivo_min - valor, 3), 0.0
                elif max_mwh is not None and valor > max_mwh:
                    estado, compras, excedentes = "excedente", 0.0, round(valor - max_mwh, 3)
                else:
                    estado, compras, excedentes = "ok", 0.0, 0.0

        bolsa_dup = sum(m.get("exposicion_bolsa_duplicados_mwh") or 0 for m in fila)
        ref = fila[0]

        plantas = []
        for m in fila:
            for p in (m.get("plantas") or []):
                plantas.append({**p, "contrato": m.get("_contrato_label")})

        consolidado.append({
            "month": i + 1,
            "min_mwh": min_mwh,
            "max_mwh": max_mwh,
            "gen_mwh": gen,
            "gen_proyectada_mwh": _sumar_opcional([m.get("gen_proyectada_mwh") for m in fila]),
            "gen_proyectada_cierre": _sumar_opcional([m.get("gen_proyectada_cierre") for m in fila]),
            "valor_mwh": valor,
            "estado": estado,
            "tipo_datos": ref.get("tipo_datos"),
            "dia_actual": ref.get("dia_actual"),
            "dias_restantes": ref.get("dias_restantes"),
            "compras_bolsa_mwh": compras,
            "excedentes_bolsa_mwh": excedentes,
            "suma_compras_bolsa_mwh": _sumar_opcional([m.get("compras_bolsa_mwh") for m in fila]),
            "suma_excedentes_bolsa_mwh": _sumar_opcional([m.get("excedentes_bolsa_mwh") for m in fila]),
            "exposicion_bolsa_duplicados_mwh": round(bolsa_dup, 3) if bolsa_dup > 0 else None,
            "n_contratos_con_compromiso": sum(
                1 for m in fila if m.get("min_mwh") is not None or m.get("max_mwh") is not None
            ),
            "plantas": plantas,
            "n_plantas": len(plantas),
        })
    return consolidado


def _totales_tabla(meses: list[dict]) -> dict:
    """Fila de la tabla 'Resumen anual por contrato': mín/máx anual y meses con compromiso."""
    return {
        "total_min_mwh": _sumar_opcional([m.get("min_mwh") for m in meses]),
        "total_max_mwh": _sumar_opcional([m.get("max_mwh") for m in meses]),
        "meses_con_compromisos": sum(
            1 for m in meses if m.get("min_mwh") is not None or m.get("max_mwh") is not None
        ),
    }


@router.get("/panel-anual")
def get_panel_anual(
    year: int = Query(..., ge=2020, le=2050, description="Año a consultar"),
    incluir_plantas: bool = Query(True, description="Incluir el desglose planta por planta de cada mes"),
    refrescar: bool = Query(False, description="Ignorar la caché y volver a consultar la generación"),
    incluir_todos: bool = Query(False, description=INCLUIR_TODOS_DESC),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Todo lo que dibuja la pestaña Cumplimiento de /mem/cumplimiento, en una llamada.

    Devuelve, para el año pedido:
      - `consolidado`: los 12 meses con todos los contratos de venta sumados.
      - `contratos[]`: cada contrato con sus 12 meses y los totales de la tabla resumen.

    Pensado para paneles externos: el valor que se compara contra el compromiso ya
    viene resuelto en `valor_mwh`, así que el consumidor no reimplementa reglas de
    negocio y sus números no pueden divergir de los de la plataforma.

    Cacheado 15 minutos en memoria (`?refrescar=true` para saltarla).
    """
    # incluir_todos va en la llave: si no, la respuesta filtrada y la completa se
    # pisarían entre sí en la caché.
    cache_key = f"panel-anual:{year}:{int(incluir_plantas)}:{int(incluir_todos)}"
    if not refrescar:
        cached = _panel_cache_get(cache_key)
        if cached is not None:
            return {**cached, "desde_cache": True}

    today = date.today()
    contratos = _query_contratos_venta(db, year, solo_relevantes=not incluir_todos)

    # GESCON por contrato/mes + compromisos, igual que get_anual_matriz.
    gpm_por_contrato: dict = {}
    comp_por_contrato: dict = {}
    for c in contratos:
        gpm_por_contrato[c.id] = {
            m: (_resolve_gescon(db, c.numero_codigo_contrato, year, m) if c.numero_codigo_contrato else [])
            for m in range(1, 13)
        }
        comp_por_contrato[c.id] = {
            r.mes: r for r in db.query(PPACompromisoEnergia).filter(
                PPACompromisoEnergia.contrato_id == c.id,
                PPACompromisoEnergia.año == year,
            ).all()
        }

    # Un solo set deduplicado de fetches para TODOS los contratos: una planta que
    # despacha a tres contratos se consulta una vez, no tres.
    need_month, need_avg, need_range = _build_fetch_sets(gpm_por_contrato, year, today)
    month_cache: dict = {}
    avg_cache: dict = {}
    range_cache: dict = {}

    if need_month or need_avg or need_range:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in get_panel_anual: %s", exc)
            token = None

        if token and need_month:
            def _ft(task):
                m, sp = task
                return task, _fetch_month(token, sp, year, m)
            with ThreadPoolExecutor(max_workers=min(len(need_month), 12)) as pool:
                for task, res in pool.map(_ft, list(need_month)):
                    month_cache[task] = res

        if token and need_avg:
            def _fa(sp):
                return sp, _fetch_recent_avg(token, sp, n_days=30)
            with ThreadPoolExecutor(max_workers=min(len(need_avg), 8)) as pool:
                for sp, res in pool.map(_fa, list(need_avg)):
                    avg_cache[sp] = res.get("avg_daily_mwh")

        if token and need_range:
            def _fr(task):
                sp, start, end = task
                return task, _fetch_range(token, sp, start, end)
            with ThreadPoolExecutor(max_workers=min(len(need_range), 12)) as pool:
                for task, res in pool.map(_fr, list(need_range)):
                    range_cache[task] = res

    out_contratos = []
    meses_por_contrato = []
    for c in contratos:
        meses, _proyectos = _anual_meses_para_contrato(
            c, year, gpm_por_contrato[c.id], comp_por_contrato[c.id],
            month_cache, avg_cache, today, range_cache,
        )
        etiqueta = c.nombre_interno or c.numero_codigo_contrato or f"Contrato {c.id}"
        # `_contrato_label` lo consume _consolidar_meses para etiquetar cada planta
        # con el contrato al que aporta; no se expone en la respuesta.
        for m in meses:
            m["_contrato_label"] = etiqueta
        meses_por_contrato.append(meses)

        limpios = []
        for m in meses:
            fila = {k: v for k, v in m.items() if k != "_contrato_label"}
            if not incluir_plantas:
                fila.pop("plantas", None)
            limpios.append(fila)

        out_contratos.append({
            "id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador_nombre": c.comprador_nombre,
            "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
            **_totales_tabla(meses),
            **_rollup_cumplimiento(meses),
            "meses": limpios,
        })

    consolidado_meses = _consolidar_meses(meses_por_contrato)
    if not incluir_plantas:
        for m in consolidado_meses:
            m.pop("plantas", None)

    payload = {
        "year": year,
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "consolidado": {
            "nombre": "Consolidado (todos)",
            "n_contratos": len(out_contratos),
            **_totales_tabla(consolidado_meses),
            "meses": consolidado_meses,
        },
        "contratos": out_contratos,
    }
    _panel_cache_set(cache_key, payload)
    return {**payload, "desde_cache": False}
