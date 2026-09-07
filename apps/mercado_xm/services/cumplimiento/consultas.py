"""Lo que Cumplimiento le pregunta a la base, en ORM de Django.

Reescritura de los helpers con sesión de `app/api/v1/cumplimiento.py`. Las
funciones puras que los rodean se movieron sin tocar (`periodos.py`, `anual.py`,
`xm_api.py`); acá está lo único que hubo que traducir.

Dos cosas que NO cambiaron y no deben cambiar:

- **El walk de vigencias GESCON no se reimplementa.** Vive en
  `apps/mercado_xm/services/gescon_vigencia.py` y lo comparten `/asic`,
  `/alertas` y esta vista. Duplicarlo es como se produce el falso positivo de
  "planta activa en dos contratos a la vez".
- **`_contratos_vigentes(solo_relevantes=True)` es el default a propósito.**
  Todas las vistas de /mem/cumplimiento ocultan los contratos de un responsable
  marcado `incluir_en_cumplimiento=False`. `/descubrimientos` y
  `/cerrar-periodo` pasan False adrede: cerrar-periodo PERSISTE registros
  mensuales, y dejar contratos fuera del cierre cambiaría datos históricos.
"""

from __future__ import annotations

import calendar
from datetime import date

from django.db import connection
from django.db.models import F, Q

from apps.mercado_xm.models import AsicSolicitud
from apps.mercado_xm.services.gescon_vigencia import resolver_vigencias
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato, PpaResponsable

from .periodos import UNGC_COMERCIALIZADOR

# Los tres filtros que se repiten en cada consulta a GESCON: publicada y sin
# contar los desistimientos. Escribirlos una vez evita que una consulta nueva
# se olvide de uno y vea solicitudes que XM nunca aceptó.
GESCON_PUBLICADA = Q(estado_solicitud="publicado") & ~Q(tipo_solicitud="desistimiento")


# `nullsfirst()`/`nullslast()` de SQLAlchemy. El orden de los NULL importa:
# `fecha_inicio IS NULL` significa "vigente desde siempre" y tiene que entrar
# PRIMERO al walk de vigencias, no al final.
def _asc_nulls_first(campo: str):
    return F(campo).asc(nulls_first=True)


def _asc_nulls_last(campo: str):
    return F(campo).asc(nulls_last=True)


def _desc_nulls_last(campo: str):
    return F(campo).desc(nulls_last=True)


def _mes(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _get_bolsa_avg(year: int, month: int) -> dict:
    """Precio de bolsa promedio del mes, desde `precios_bolsa_diario`.

    SQL crudo porque esa tabla es una de las 28 sin modelo (ver apps/README.md):
    nació en `_PENDING_DDLS` y solo la lee esta consulta.
    """
    with connection.cursor() as cur:
        cur.execute("""
            SELECT
                AVG(precio_promedio) AS precio_promedio,
                MIN(precio_min) AS precio_min,
                MAX(precio_max) AS precio_max,
                AVG(precio_escasez) AS precio_escasez,
                COUNT(*) AS dias_con_datos
            FROM precios_bolsa_diario
            WHERE EXTRACT(YEAR FROM fecha) = %s
              AND EXTRACT(MONTH FROM fecha) = %s
              AND precio_promedio IS NOT NULL
        """, [year, month])
        fila = cur.fetchone()

    if not fila or fila[0] is None:
        return {"precio_promedio": None, "precio_min": None, "precio_max": None,
                "precio_escasez": None, "dias_con_datos": 0}
    promedio, minimo, maximo, escasez, dias = fila
    return {
        "precio_promedio": round(float(promedio), 2),
        "precio_min": round(float(minimo), 2) if minimo else None,
        "precio_max": round(float(maximo), 2) if maximo else None,
        "precio_escasez": round(float(escasez), 2) if escasez else None,
        "dias_con_datos": int(dias),
    }


def _ids_responsables_ocultos() -> set:
    """Responsables marcados como NO relevantes: sus contratos los gestiona un
    tercero y no deben aparecer en las vistas de /mem/cumplimiento."""
    return set(
        PpaResponsable.objects
        .filter(incluir_en_cumplimiento=False)
        .values_list("id", flat=True)
    )


def _filtro_responsable_relevante() -> Q | None:
    """Deja pasar los contratos sin responsable y los de un responsable
    relevante. `None` si no hay ninguno oculto (no filtra)."""
    ocultos = _ids_responsables_ocultos()
    if not ocultos:
        return None
    return Q(responsable_id__isnull=True) | ~Q(responsable_id__in=ocultos)


def _contratos_vigentes(year: int, month: int | None = None,
                        solo_relevantes: bool = True) -> list:
    """Contratos PPA activos en el período, sin los borrados. `month=None` → el año.

    Contrato SIN responsable = se incluye: nada se esconde por omisión, solo por
    marca explícita.
    """
    if month:
        first_day, last_day = _mes(year, month)
    else:
        first_day, last_day = date(year, 1, 1), date(year, 12, 31)

    qs = (
        PpaContrato.objects
        # el responsable se lee en las filas de la matriz: precargarlo evita N+1
        .select_related("responsable")
        .filter(deleted_at__isnull=True)
        .filter(Q(fecha_inicio__isnull=True) | Q(fecha_inicio__lte=last_day))
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=first_day))
    )
    if solo_relevantes:
        clausula = _filtro_responsable_relevante()
        if clausula is not None:
            qs = qs.filter(clausula)
    # `nullslast` de SQLAlchemy: en Django es el `F(...).asc(nulls_last=True)`
    # que produce el mismo ORDER BY … NULLS LAST.
    return list(qs.order_by(_asc_nulls_last("nombre_interno"), "id"))



class _AsicVigenciaRecortada:
    """Envoltura de solo lectura sobre un AsicSolicitud que acota su `fecha_fin`
    efectiva sin tocar el registro real.

    Necesaria porque la misma instancia se reutiliza en las 12 llamadas por mes
    de `_anual_meses_para_contrato`: mutar `.fecha_fin` directamente contaminaría
    el cálculo de los demás meses. Reenvía cualquier otro atributo (proyecto,
    porcentaje_despacho, fecha_inicio, …) al original vía `__getattr__`.
    """

    __slots__ = ("_r", "fecha_fin")

    def __init__(self, r, fecha_fin: date):
        self._r = r
        self.fecha_fin = fecha_fin

    def __getattr__(self, name):
        return getattr(self._r, name)


def _versiones_del_contrato(contrato_interno: str):
    """TODAS las versiones del contrato, pasadas y futuras, en orden cronológico
    por cuándo tomó efecto cada una — no por cuándo se radicó."""
    return list(
        AsicSolicitud.objects
        .select_related("proyecto")
        .filter(GESCON_PUBLICADA, contrato_interno=contrato_interno)
        .order_by(
            _asc_nulls_first("fecha_inicio"),
            _asc_nulls_first("fecha_solicitud"),
            "created_at",
        )
    )



def _resolve_gescon(contrato_interno: str, year: int, month: int) -> list:
    """Registros ASIC vigentes para el contrato en el mes dado, reconstruyendo la
    vigencia HISTÓRICA (no la versión más reciente global).

    Procesa cronológicamente por `fecha_inicio` y, para el mes consultado, solo
    reproduce los eventos que ya habían tomado efecto (`fecha_inicio <= último
    día del mes). Así un mes anterior a una modificación ve la versión que estaba
    vigente entonces, no la que la reemplazó después.

    Una planta relevada A MITAD DE MES no desaparece: se devuelve envuelta en
    `_AsicVigenciaRecortada` con su fecha_fin efectiva, para que el prorrateo por
    días cuente a cada una solo sus días reales sin solaparse. Caso real: SIC
    89115 (Terpel 2, feb-2026), Baraya del 5 al 25 y Yurbaqua del 26 al 28.
    """
    first_day, last_day = _mes(year, month)
    records = _versiones_del_contrato(contrato_interno)

    # `hasta=last_day` reproduce la vista histórica: eventos que aún no tomaban
    # efecto no desplazan la versión vigente del mes.
    vigencias = resolver_vigencias(records, hasta=last_day)

    result = []
    for r in records:
        v = vigencias[r.id]
        if not v.procesado:
            continue
        if r.tipo_solicitud == "terminacion":
            continue
        if v.vigente:
            result.append(r)
        elif v.saliente_por_relevo:
            result.append(_AsicVigenciaRecortada(r, v.fecha_fin_efectiva))
        # Superseded en sitio (misma planta): la versión nueva la representa.

    return [
        r for r in result
        if (r.fecha_fin is None or r.fecha_fin >= first_day)
        and (r.fecha_inicio is None or r.fecha_inicio <= last_day)
    ]


def _clasificar_remanente_bolsa(proyecto_id: int, first_day: date, last_day: date):
    """Clasifica una planta del remanente (sin contrato PPA) en su piscina de bolsa.

    Paso POSTERIOR a la lógica de contratos: solo se aplica a plantas que ya
    quedaron sin contrato PPA asignado vía GESCON.

      - Código SIC vigente con `codigo_sic_comprador == 'UNGC'` → 'comercializador'.
      - Sin código SIC vigente con comprador UNGC en esas fechas → 'libre'.

    Devuelve `(piscina, asic_vigente|None)`; el asic sale para diagnóstico.
    """
    asic = (
        AsicSolicitud.objects
        .filter(
            GESCON_PUBLICADA,
            proyecto_id=proyecto_id,
            codigo_sic_contrato__isnull=False,
            codigo_sic_comprador=UNGC_COMERCIALIZADOR,
        )
        .exclude(tipo_solicitud="terminacion")
        .filter(Q(fecha_inicio__isnull=True) | Q(fecha_inicio__lte=last_day))
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=first_day))
        .order_by(_desc_nulls_last("fecha_inicio"))
        .first()
    )
    if asic is not None:
        return "comercializador", asic
    return "libre", None



def _fin_efectivo_asic(asic, last_day: date) -> date | None:
    """Fin EFECTIVO de la ventana de `asic`, recortado por supersesiones/relevos
    en su SIC (vista histórica al mes consultado, mismo criterio que la piscina b
    — caso La Reserva). Fallback: la fecha_fin cruda del registro."""
    universo = list(
        AsicSolicitud.objects
        .filter(GESCON_PUBLICADA, codigo_sic_contrato=asic.codigo_sic_contrato)
        .order_by(
            _asc_nulls_first("fecha_inicio"),
            _asc_nulls_first("fecha_solicitud"),
            "created_at",
        )
    )
    v = resolver_vigencias(universo, hasta=last_day).get(asic.id)
    return v.fecha_fin_efectiva if v else asic.fecha_fin


def _query_contratos_venta(year: int | None = None, month: int | None = None,
                           solo_relevantes: bool = True) -> list:
    """Contratos PPA de venta (`tipo_contrato != 'compra'`).

    Replica EXACTAMENTE el filtro con que `/simulador` arma `contratos_venta`:
    primero todos los vigentes del año/mes, después se excluyen las compras. Sin
    `year` usa el año en curso (lo necesita `/anual-matriz`).
    """
    if year is None:
        year = hoy_col().year
    return [
        c for c in _contratos_vigentes(year, month, solo_relevantes=solo_relevantes)
        if (c.tipo_contrato or "venta") != "compra"
    ]
