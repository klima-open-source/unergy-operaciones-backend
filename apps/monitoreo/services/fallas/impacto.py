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
import json
import math
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from apps.monitoreo.models import Falla, FallaCatPrioridad, FallaInversor
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
# "Generando sin datos": solo se sabe el % cuando alguien fue a verificar.
_SIN_DATOS_VERIFICADO = {"verificado_apagado": 100.0, "verificado_encendido": 0.0}
# Equipos de planta cuya falla no le quita energía a la planta. Un tracker o un
# string sí, pero cuánto depende de cuál: eso se deja a la prioridad manual.
_EQUIPOS_SIN_PERDIDA = {"smartlogger", "estacion_meteorologica"}
# Un inversor que solo perdió internet sigue generando.
_TIPOS_INVERSOR_SIN_PERDIDA = {"perdida_comunicacion"}

# Los reportes que llegan del monitoreo y del celular no vinculan el inversor del
# proyecto: traen solo un nombre, a veces con varios equipos en una misma fila
# ("300KTL-Inversor1|330KTL-Inversor2"). La potencia sale primero del catálogo
# del proyecto por número de inversor, y solo si no está, del modelo en el
# nombre ("330KTL" es el modelo, no siempre la potencia nominal).
_SEPARADORES_NOMBRE = re.compile(r"[|,;]")
_KW_EN_NOMBRE = re.compile(r"(\d+(?:[.,]\d+)?)\s*k(?:tl|w)\b", re.IGNORECASE)
_NUMERO_INVERSOR = re.compile(r"\b(?:inversor|inv)[\s_\-]*(\d+)", re.IGNORECASE)

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
        return _SIN_DATOS_VERIFICADO.get(falla.subtipo_codigo)
    if categoria == "frontera":
        # La planta sigue generando: lo que falla es la medición o el enlace.
        return 0.0
    if categoria == "inversores":
        return _pct_inversores(falla)
    if categoria == "equipos" and falla.subtipo_codigo in _EQUIPOS_SIN_PERDIDA:
        return 0.0
    return None


def _pct_inversores(falla: Falla) -> float | None:
    inversores = list(falla.inversores_afectados.all())
    # Sin tipos no se sabe que solo sea comunicación: cuenta como pérdida.
    con_perdida = [
        inv for inv in inversores
        if not inv.tipos or set(inv.tipos) - _TIPOS_INVERSOR_SIN_PERDIDA
    ]
    if inversores and not con_perdida:
        return 0.0

    total_kw = float(falla.proyecto.potencia_ac_kw or 0) if falla.proyecto_id else 0.0
    if not con_perdida or total_kw <= 0:
        return None

    catalogo: dict[int, float] | None = None
    afectado_kw = 0.0
    for inv in con_perdida:
        kw = _kw_vinculado(inv)
        if kw is None:
            if catalogo is None:
                catalogo = _catalogo_inversores(falla)
            kw = _kw_por_nombre(inv.nombre or "", catalogo)
        if kw is None:
            # Sin la potencia de un inversor el % sería inventado.
            return None
        afectado_kw += kw
    return round(min(100.0, afectado_kw / total_kw * 100), 1)


def _kw_vinculado(inv: FallaInversor) -> float | None:
    """La potencia que trae la fila o el inversor del proyecto al que apunta."""
    if inv.potencia_kw is not None:
        return float(inv.potencia_kw)
    if inv.proyecto_inversor_id and inv.proyecto_inversor.potencia_nominal_kw is not None:
        return float(inv.proyecto_inversor.potencia_nominal_kw)
    return None


def _catalogo_inversores(falla: Falla) -> dict[int, float]:
    """`{número de inversor: kW}` del catálogo del proyecto ("Inversor 3" → 3)."""
    catalogo: dict[int, float] = {}
    for inv in falla.proyecto.inversores.all():
        numero = _NUMERO_INVERSOR.search(inv.nombre or "")
        if numero and inv.potencia_nominal_kw is not None:
            catalogo[int(numero.group(1))] = float(inv.potencia_nominal_kw)
    return catalogo


def _kw_por_nombre(nombre: str, catalogo: dict[int, float]) -> float | None:
    """kW de uno o varios inversores nombrados en texto; `None` si alguno no se sabe."""
    partes = [n.strip() for n in _SEPARADORES_NOMBRE.split(nombre) if n.strip()]
    if not partes:
        return None
    total = 0.0
    for parte in partes:
        numero = _NUMERO_INVERSOR.search(parte)
        en_nombre = _KW_EN_NOMBRE.search(parte)
        if numero and int(numero.group(1)) in catalogo:
            total += catalogo[int(numero.group(1))]
        elif en_nombre:
            total += float(en_nombre.group(1).replace(",", "."))
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

class _SinSimulacion(Exception):
    """Falta el P90 de un mes que la falla toca."""


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

    p90 = _p90_mensual(falla.proyecto.p90_mensual_kwh if falla.proyecto_id else None)
    tramos = _tramos_afectacion(falla)
    if p90 is None or tramos is None:
        return None

    def kwh_dia(dia: date) -> float:
        kwh_mes = p90[dia.month - 1]
        if kwh_mes is None:
            raise _SinSimulacion
        return kwh_mes / calendar.monthrange(dia.year, dia.month)[1]

    try:
        total = sum(_energia_tramo(inicio, fin, kwh_dia) for inicio, fin in tramos)
    except _SinSimulacion:
        return None
    return round(total * pct / 100, 1)


def _p90_mensual(valor: object) -> list[float | None] | None:
    """Los 12 kWh del P90 como números; un mes ilegible queda en `None`.

    Hay P90 guardados como texto JSON (dato histórico) y listas con valores no
    numéricos: ninguno puede tumbar el listado de fallas.
    """
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except json.JSONDecodeError:
            return None
    if not isinstance(valor, list) or len(valor) != 12:
        return None
    meses: list[float | None] = []
    for kwh in valor:
        try:
            meses.append(float(kwh))
        except (TypeError, ValueError):
            meses.append(None)
    return meses


def _tramos_afectacion(falla: Falla) -> list[tuple[datetime, datetime]] | None:
    """Los tramos en que la planta estuvo afectada, en hora Colombia.

    Mandan los intervalos de disparo si los hay; si no, de la ocurrencia (o
    identificación) a la resolución. Una falla abierta se cuenta hasta ahora; una
    cerrada sin fecha de solución (dato legacy) no tiene fin conocido: `None`.
    """
    cerrada = bool(falla.estado_id and falla.estado.es_estado_final)
    ahora = ahora_col()

    intervalos = [iv for iv in falla.intervalos.all() if iv.inicio]
    if intervalos:
        if cerrada and any(iv.fin is None for iv in intervalos):
            return None
        return [
            (_col(iv.inicio), _col(iv.fin) if iv.fin else ahora) for iv in intervalos
        ]

    if cerrada and not falla.fecha_resolucion:
        return None
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


def _energia_tramo(inicio: datetime, fin: datetime, kwh_dia: Callable[[date], float]) -> float:
    """kWh de P90 (al 100 %) entre dos instantes en hora Colombia.

    Los días completos del medio se suman por mes, sin recorrerlos uno a uno: una
    falla legacy abierta hace años no puede volver lento el listado.
    """
    if fin <= inicio:
        return 0.0
    if inicio.date() == fin.date():
        return kwh_dia(inicio.date()) * _fraccion_solar(_hora(inicio), _hora(fin))

    total = kwh_dia(inicio.date()) * _fraccion_solar(_hora(inicio), 24.0)
    total += kwh_dia(fin.date()) * _fraccion_solar(0.0, _hora(fin))

    dia = inicio.date() + timedelta(days=1)
    while dia < fin.date():
        inicio_mes_siguiente = date(dia.year + dia.month // 12, dia.month % 12 + 1, 1)
        corte = min(fin.date(), inicio_mes_siguiente)
        total += (corte - dia).days * kwh_dia(dia)
        dia = corte
    return total


def _hora(dt: datetime) -> float:
    return dt.hour + dt.minute / 60 + dt.second / 3600 + dt.microsecond / 3_600_000_000


def _fraccion_solar(desde_h: float, hasta_h: float) -> float:
    """Parte de la energía del día generada entre dos horas (campana seno)."""
    def acumulada(h: float) -> float:
        if h <= _AMANECER_H:
            return 0.0
        if h >= _ATARDECER_H:
            return 1.0
        return (1 - math.cos(math.pi * (h - _AMANECER_H) / (_ATARDECER_H - _AMANECER_H))) / 2

    return max(0.0, acumulada(hasta_h) - acumulada(desde_h))
