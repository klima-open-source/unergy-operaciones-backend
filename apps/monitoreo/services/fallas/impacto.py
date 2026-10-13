"""El impacto de una falla: qué parte de la planta afecta y cuánta energía se pierde.

Un solo dato, el **% de la potencia afectada**, decide tres cosas (decisión de
Operaciones del 2026-10-13):

- la **prioridad** de la falla, y con ella el SLA operativo en horas
  (`dominio.DEFAULT_SLA_HOURS`);
- la **gravedad del Anexo 4** del contrato de O&M (`sla_contractual.py`), que el
  contrato define justamente por % afectado;
- la **energía perdida**, estimada con la simulación P90 de la planta.

Antes la prioridad la elegía a mano quien reportaba y el Anexo 4 salía de la
categoría: tres respuestas para la misma pregunta, cada una por su lado.

`None` significa "no se puede calcular" (falla legacy sin categoría, inversores
sin potencia cargada, planta sin potencia AC o sin simulación). En ese caso se
respeta lo que haya elegido la persona y no se inventa un número.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from apps.monitoreo.models import Falla, FallaCatPrioridad
from apps.plataforma.services.fechas import COL_TZ, ahora_col


class Prioridad(StrEnum):
    """Los `codigo` de `fallas_cat_prioridades`."""

    CRITICA = "critica"
    GRAVE = "grave"
    MEDIA = "media"
    LEVE = "leve"


# Umbrales del Anexo 4, en % de la potencia AC de la planta.
UMBRAL_CRITICO = 90.0
UMBRAL_GRAVE = 66.0

# Categorías donde la planta entera deja de entregar energía.
_PLANTA_COMPLETA = {"red"}
# Subtipos de "generando sin datos" en que se verificó que la planta estaba apagada.
_SIN_DATOS_APAGADA = {"verificado_apagado"}
# Un inversor que solo perdió internet sigue generando.
_TIPOS_INVERSOR_SIN_PERDIDA = {"perdida_comunicacion"}

# Los reportes que llegan del monitoreo y del celular no vinculan el inversor del
# proyecto: traen solo un nombre, a veces con varios equipos en una misma fila
# ("300KTL-Inversor1|330KTL-Inversor2"). La potencia sale del nombre o del
# catálogo del proyecto.
_SEPARADORES_NOMBRE = re.compile(r"[|,;]")
_KW_EN_NOMBRE = re.compile(r"(\d+(?:[.,]\d+)?)\s*k(?:tl|w)\b", re.IGNORECASE)
_NUMERO_INVERSOR = re.compile(r"inversor\s*-?\s*(\d+)", re.IGNORECASE)

# Ventana solar para repartir la energía del día, en hora Colombia. La
# generación se modela como una campana seno entre estas dos horas: una falla
# de 6 a 8 a.m. pierde mucho menos que una de 11 a.m. a 1 p.m.
_AMANECER_H = 6.0
_ATARDECER_H = 18.0


def pct_afectado(falla: Falla) -> float | None:
    """% de la potencia AC de la planta que la falla deja sin generar (0-100)."""
    categoria = falla.categoria_codigo
    if not categoria:
        return None
    if categoria in _PLANTA_COMPLETA:
        return 100.0
    if categoria == "generando_sin_datos":
        return 100.0 if falla.subtipo_codigo in _SIN_DATOS_APAGADA else 0.0
    if categoria == "frontera":
        # La planta sigue generando: lo que falla es la medición o el enlace.
        return 0.0
    if categoria == "inversores":
        return _pct_inversores(falla)
    return None


def _pct_inversores(falla: Falla) -> float | None:
    inversores = list(falla.inversores_afectados.all())
    con_perdida = [
        inv for inv in inversores
        if set(inv.tipos or []) - _TIPOS_INVERSOR_SIN_PERDIDA
    ]
    if inversores and not con_perdida:
        return 0.0

    total_kw = float(falla.proyecto.potencia_ac_kw or 0) if falla.proyecto_id else 0.0
    if not con_perdida or total_kw <= 0:
        return None

    catalogo = _catalogo_inversores(falla)
    afectado_kw = 0.0
    for inv in con_perdida:
        kw = _kw_inversor(inv, catalogo)
        if kw is None:
            # Sin la potencia de un inversor el % sería inventado.
            return None
        afectado_kw += kw
    return round(min(100.0, afectado_kw / total_kw * 100), 1)


def _catalogo_inversores(falla: Falla) -> dict[int, float]:
    """`{número de inversor: kW}` del catálogo del proyecto ("Inversor 3" → 3)."""
    catalogo: dict[int, float] = {}
    for inv in falla.proyecto.inversores.all():
        numero = _NUMERO_INVERSOR.search(inv.nombre or "")
        if numero and inv.potencia_nominal_kw is not None:
            catalogo[int(numero.group(1))] = float(inv.potencia_nominal_kw)
    return catalogo


def _kw_inversor(inv, catalogo: dict[int, float]) -> float | None:
    """La potencia de una fila de `falla_inversores`, o `None` si no se sabe."""
    if inv.potencia_kw is not None:
        return float(inv.potencia_kw)
    if inv.proyecto_inversor_id and inv.proyecto_inversor.potencia_nominal_kw is not None:
        return float(inv.proyecto_inversor.potencia_nominal_kw)

    total = 0.0
    nombres = [n.strip() for n in _SEPARADORES_NOMBRE.split(inv.nombre or "") if n.strip()]
    if not nombres:
        return None
    for nombre in nombres:
        en_nombre = _KW_EN_NOMBRE.search(nombre)
        numero = _NUMERO_INVERSOR.search(nombre)
        if en_nombre:
            total += float(en_nombre.group(1).replace(",", "."))
        elif numero and int(numero.group(1)) in catalogo:
            total += catalogo[int(numero.group(1))]
        else:
            return None
    return total


def prioridad_calculada(falla: Falla, pct: float | None = None) -> Prioridad | None:
    """La prioridad que corresponde al % afectado; `None` si no se puede saber."""
    pct = pct_afectado(falla) if pct is None else pct
    if pct is None:
        return None
    if falla.categoria_codigo == "frontera" and falla.frontera_afecta_medicion:
        # No se pierde energía, pero sin medición no se le puede facturar.
        return Prioridad.GRAVE
    if pct >= UMBRAL_CRITICO:
        return Prioridad.CRITICA
    if pct >= UMBRAL_GRAVE:
        return Prioridad.GRAVE
    if pct > 0:
        return Prioridad.MEDIA
    return Prioridad.LEVE


def aplicar_prioridad(falla: Falla) -> None:
    """Asigna la prioridad calculada. Si no se puede calcular, deja la elegida."""
    codigo = prioridad_calculada(falla)
    if codigo is None:
        return
    prioridad_id = (
        FallaCatPrioridad.objects.filter(codigo=codigo).values_list("id", flat=True).first()
    )
    if prioridad_id:
        falla.prioridad_id = prioridad_id


# ── Energía perdida ──────────────────────────────────────────────────────────

def kwh_perdidos_p90(falla: Falla, pct: float | None = None) -> float | None:
    """kWh que la planta dejó de generar según su simulación P90.

    Por cada día afectado: P90 del mes ÷ días del mes × fracción de la energía
    solar del día que cayó dentro de la falla × % afectado. Una falla abierta se
    cuenta hasta ahora, así que su valor crece mientras siga abierta.
    """
    pct = pct_afectado(falla) if pct is None else pct
    if pct is None:
        return None
    if pct == 0:
        return 0.0

    p90 = falla.proyecto.p90_mensual_kwh if falla.proyecto_id else None
    if not isinstance(p90, list) or len(p90) != 12:
        return None

    tramos = _tramos_afectacion(falla)
    if tramos is None:
        return None

    total = 0.0
    for inicio, fin in tramos:
        for dia, desde_h, hasta_h in _por_dia(inicio, fin):
            kwh_mes = p90[dia.month - 1]
            if kwh_mes is None:
                return None
            kwh_dia = float(kwh_mes) / calendar.monthrange(dia.year, dia.month)[1]
            total += kwh_dia * _fraccion_solar(desde_h, hasta_h)
    return round(total * pct / 100, 1)


def _tramos_afectacion(falla: Falla) -> list[tuple[datetime, datetime]] | None:
    """Los tramos en que la planta estuvo afectada, en hora Colombia.

    Mismo criterio que `dominio.tiempo_afectacion_horas`: mandan los intervalos
    de disparo si los hay; si no, de la ocurrencia (o identificación) a la
    resolución, o a ahora si sigue abierta.
    """
    ahora = ahora_col()
    intervalos = [iv for iv in falla.intervalos.all() if iv.inicio]
    if intervalos:
        return [
            (_col(iv.inicio), _col(iv.fin) if iv.fin else ahora) for iv in intervalos
        ]

    inicio = falla.fecha_ocurrencia
    if inicio is None:
        if not falla.fecha_identificacion:
            return None
        inicio = datetime.combine(
            falla.fecha_identificacion, falla.hora_identificacion or time(0, 0),
        )
    fin = falla.fecha_resolucion or ahora
    return [(_col(inicio), _col(fin))]


def _col(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=COL_TZ)
    return dt.astimezone(COL_TZ)


def _por_dia(inicio: datetime, fin: datetime):
    """Parte `[inicio, fin)` en `(día, hora_desde, hora_hasta)` con horas decimales."""
    actual = inicio
    while actual < fin:
        dia: date = actual.date()
        siguiente = datetime.combine(dia + timedelta(days=1), time(0, 0), tzinfo=COL_TZ)
        corte = min(fin, siguiente)
        desde_h = (actual - actual.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600
        hasta_h = 24.0 if corte == siguiente else (
            corte - corte.replace(hour=0, minute=0, second=0, microsecond=0)
        ).total_seconds() / 3600
        yield dia, desde_h, hasta_h
        actual = corte


def _fraccion_solar(desde_h: float, hasta_h: float) -> float:
    """Parte de la energía del día generada entre dos horas (campana seno)."""
    def acumulada(h: float) -> float:
        if h <= _AMANECER_H:
            return 0.0
        if h >= _ATARDECER_H:
            return 1.0
        return (1 - math.cos(math.pi * (h - _AMANECER_H) / (_ATARDECER_H - _AMANECER_H))) / 2

    return max(0.0, acumulada(hasta_h) - acumulada(desde_h))
