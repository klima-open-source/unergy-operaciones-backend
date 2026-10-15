"""Vista de contratos: la foto de UN día, lista para consumir desde afuera.

Puerto de `app/services/vista_contratos.py`. Responde "¿en qué contrato está cada
planta el 20 de agosto, cuánto se comprometió ese contrato y cuánto genera cada
planta?" en **una sola llamada**, sin que quien consulta tenga que entender
GESCON, vigencias ni piscinas.

Se apoya directo en `plantas_contratos`, que ya resuelve lo difícil (vigencias,
relevos, recortes intra-mes): acá **no se reimplementa nada de eso**, solo se
filtra por día y se enriquece con lo que falta.

**Es la foto de un día, no del mes.** Cada fila de planta trae su ventana dentro
del mes (`segmento_inicio`/`segmento_fin`); solo pasa la que cubre el día pedido.
Una planta que salió el 19 no aparece el 20, aunque haya estado en el contrato
casi todo agosto.

Trae todos los contratos de venta vigentes: hasta el 2026-10-14 un filtro por
empresa responsable (`responsable=Unergy`) dejaba fuera los que gestiona un
tercero; se quitó junto con `ppa_responsables`.
"""

from __future__ import annotations

import calendar
import unicodedata
from datetime import date

from apps.ppa.models import PpaCompromisoEnergia
from apps.proyectos.models import Proyecto


def _norm(s) -> str:
    """Minúsculas, sin tildes, sin espacios de más. Para comparar nombres."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().replace("\xa0", " ").split())


def _fecha(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _cubre(fila: dict, dia: date) -> bool:
    """¿El segmento de esta fila incluye el día pedido?

    Sin `segmento_*` la fila pasa: descartarla sería inventar que la planta no
    estaba.
    """
    ini, fin = _fecha(fila.get("segmento_inicio")), _fecha(fila.get("segmento_fin"))
    if ini is None and fin is None:
        return True
    return not ((ini and dia < ini) or (fin and dia > fin))


def _nota_ventana(fila: dict, dia: date) -> list[str]:
    """Marca las plantas que entran o salen DENTRO del mes de la fecha.

    En la foto del día 20 se ven igual una planta que lleva todo el mes y una que
    entró el 12; la diferencia es justo lo que importa para decidir movimientos.
    """
    ini, fin = _fecha(fila.get("segmento_inicio")), _fecha(fila.get("segmento_fin"))
    marcas = []
    if ini and (ini.year, ini.month) == (dia.year, dia.month) and ini.day > 1:
        marcas.append(f"entra el {ini.day}")
    if fin and (fin.year, fin.month) == (dia.year, dia.month):
        if fin.day < calendar.monthrange(dia.year, dia.month)[1]:
            marcas.append(f"sale el {fin.day}")
    return marcas


def _pct(crudo) -> tuple[float | None, bool]:
    """`(fracción_0_1, dudoso)`.

    El % de despacho se guarda 0-1, pero un formulario viejo dejó filas en escala
    0-100. Por encima de 1 se convierte y se marca dudoso, en vez de multiplicar
    a ciegas: un 100 tomado como fracción inflaría el aporte 100 veces.
    """
    if crudo is None:
        return None, False
    try:
        v = float(crudo)
    except (TypeError, ValueError):
        return None, False
    return (min(v / 100.0, 1.0), True) if v > 1.0 else (v, False)


def construir(fecha: date) -> dict:
    """La vista completa para `fecha`. Ver el docstring del módulo."""
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    year, month = fecha.year, fecha.month
    # El núcleo canónico. No se toca: ya trae vigencias, relevos y recortes.
    piscinas = plantas_contratos(year, month)

    # ── datos de apoyo, en dos consultas ────────────────────────────────────
    compromisos = {
        c.contrato_id: c
        for c in PpaCompromisoEnergia.objects.filter(año=year, mes=month)
    }
    proyectos = [
        (p, p.portafolio.nombre if p.portafolio else None)
        for p in Proyecto.objects.select_related("portafolio")
        .filter(deleted_at__isnull=True)
    ]
    por_id = {p.id: (p, port) for p, port in proyectos}
    por_nombre = {}
    for p, port in proyectos:
        por_nombre.setdefault(_norm(p.nombre_comercial), (p, port))

    contratos_out, avisos = [], []

    for c in piscinas.get("venta", []) or []:
        filas = [f for f in (c.get("plantas") or []) if _cubre(f, fecha)]
        if not filas:
            # Vigente en el mes pero sin plantas ese día. No es lo mismo que "no
            # existe": se reporta con 0 plantas en vez de esconderlo.
            avisos.append(f"{c.get('nombre')}: sin plantas asignadas el {fecha.isoformat()}")

        plantas_out, portafolios, faltan = [], [], 0
        for f in filas:
            par = por_id.get(f.get("id")) or por_nombre.get(_norm(f.get("nombre")))
            proy, portafolio = par if par else (None, None)
            if portafolio:
                portafolios.append(portafolio)

            frac, dudoso = _pct(f.get("pct_despacho"))
            promedio = getattr(proy, "gen_mensual_promedio_mwh", None) if proy else None
            promedio = float(promedio) if promedio is not None else None
            if promedio is None:
                faltan += 1

            marcas = _nota_ventana(f, fecha)
            if f.get("es_duplicado"):
                marcas.insert(0, "duplicada")
            if f.get("uso_del_recurso"):
                marcas.insert(0, "uso del recurso")
            if dudoso:
                marcas.append("% dudoso")
            if promedio is None:
                marcas.append("falta promedio")

            plantas_out.append({
                "proyecto_id": f.get("id"),
                "planta": f.get("nombre"),
                "fpo": proy.fecha_entrada_operacion.isoformat()
                       if proy and proy.fecha_entrada_operacion else None,
                # Generación de un mes típico, de proyectos.gen_mensual_promedio_mwh.
                # `null` = todavía no se calculó para esa planta; NO se rellena con
                # otra cifra, que se leería como un promedio y no lo es.
                "gen_prom_mwh_mes": promedio,
                "gen_prom_origen": getattr(proy, "gen_promedio_origen", None) if proy else None,
                "pct_asignado": frac,
                "codigo_sic": f.get("codigo_sic"),
                "desde": f.get("segmento_inicio"),
                "hasta": f.get("segmento_fin"),
                "marcas": marcas,
            })

        plantas_out.sort(key=lambda x: _norm(x["planta"]))
        comp = compromisos.get(c.get("id"))
        mn = float(comp.energia_minima) if comp and comp.energia_minima is not None else None
        mx = float(comp.energia_maxima) if comp and comp.energia_maxima is not None else None

        # Suma de los promedios ponderada por el % de despacho. Solo se informa
        # si TODAS las plantas tienen promedio: una suma a la que le faltan
        # plantas se compararía contra el mínimo y daría un déficit falso.
        aporte = None
        if plantas_out and faltan == 0:
            aporte = round(sum((p["gen_prom_mwh_mes"] or 0) *
                               (p["pct_asignado"] if p["pct_asignado"] is not None else 1)
                               for p in plantas_out), 3)

        contratos_out.append({
            "contrato_id": c.get("id"),
            "contrato": c.get("nombre"),
            "codigo": c.get("numero_codigo_contrato"),
            "comprador": c.get("comprador_nombre"),
            "portafolio": _dominante(portafolios),
            "min_mes_mwh": mn,
            "max_mes_mwh": mx,
            "gen_prom_total_mwh": aporte,
            "plantas_sin_promedio": faltan,
            "n_plantas": len(plantas_out),
            "estado": _estado(mn, mx, aporte),
            "plantas": plantas_out,
        })

    contratos_out.sort(key=lambda c: _norm(c["contrato"]))
    return {
        "fecha": fecha.isoformat(),
        "mes_consultado": f"{year}-{month:02d}",
        "contratos": contratos_out,
        "avisos": avisos,
        "totales": {
            "n_contratos": len(contratos_out),
            "n_plantas": sum(c["n_plantas"] for c in contratos_out),
            # Un contrato sin mínimo cargado NO aporta 0: sumarlo como cero haría
            # ver como cumplido algo que solo está sin cargar.
            "min_mes_mwh": _suma(c["min_mes_mwh"] for c in contratos_out),
            "gen_prom_total_mwh": _suma(c["gen_prom_total_mwh"] for c in contratos_out),
            "contratos_sin_minimo": sum(1 for c in contratos_out if c["min_mes_mwh"] is None),
            "plantas_sin_promedio": sum(c["plantas_sin_promedio"] for c in contratos_out),
        },
    }


def _dominante(nombres: list[str]) -> str | None:
    """El portafolio que comparte la mayoría de las plantas. `(+N)` si hay más."""
    if not nombres:
        return None
    from collections import Counter
    conteo = Counter(nombres)
    top, _ = conteo.most_common(1)[0]
    otros = len(conteo) - 1
    return f"{top} (+{otros})" if otros else top


def _estado(mn: float | None, mx: float | None, gen: float | None) -> str:
    """Compara el compromiso del mes contra la suma de promedios mensuales.

    Las dos cifras son mensuales, así que son comparables. `sin_datos` cuando
    falta el promedio de alguna planta: mejor decirlo que dar un veredicto
    construido sobre una suma incompleta.
    """
    if mn is None and mx is None:
        return "sin_compromisos"
    if gen is None:
        return "sin_datos"
    if mn is not None and gen < mn:
        return "deficit"
    if mx is not None and gen > mx:
        return "excedente"
    return "ok"


def _suma(valores) -> float | None:
    """Suma ignorando None. Si TODO es None devuelve None, no 0."""
    vs = [v for v in valores if v is not None]
    return round(sum(vs), 3) if vs else None
