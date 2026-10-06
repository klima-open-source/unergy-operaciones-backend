"""El reparto del Panel Contable entre los inversionistas de cada planta.

Puerto de los helpers de `app/api/v1/panel_contable.py`.

**La escala del porcentaje se detecta por la SUMA, no por un umbral por valor.**
`porcentaje_participacion` está cargado unas veces en 0-1 (0.5 = 50 %) y otras en
0-100 (50 = 50 %), según cómo se creó cada proyecto. Un umbral por valor fallaba
en los dos extremos: un único inversionista al 100 % guardado como `1.0` se
mostraba como "1 %", y El Son con `0.21` como "0.21 %". Comparar la distancia de
la suma a 1 contra su distancia a 100 tolera además las sumas parciales de un mes
en el que no todos estuvieron activos.

**Un reparto que no suma 100 % se avisa, no se corrige en silencio.** Puede pasar
cuando un inversionista termina a mitad de mes: el saliente y el entrante se
traslapan y suman 200 %. Dividir callado se lleva plata a ninguna parte o la
duplica.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date

from django.db.models import F

from apps.clientes.models import Cliente
from apps.proyectos.models import ProyectoInversionista

from apps.contabilidad.services.er_loader import IVA

logger = logging.getLogger("operaciones.panel_contable")


def _construir_lineas_base(parsed: dict) -> list[dict]:
    """
    Renglones del ER al 100% (sin dividir). Cada uno: {grupo, concepto, valor,
    hoja, celda}. hoja/celda = la celda del ER de donde salió el valor (PROPONER);
    el frontend la muestra y la usuaria puede corregirla. Los totales y la utilidad
    los calcula el frontend; aquí solo van renglones base.
    """
    lineas: list[dict] = []
    tipo = (parsed.get("tipo") or "normal").lower()

    def _orig(d: dict) -> dict:
        # `fuente` viaja junto a hoja/celda: las líneas armadas desde la API la
        # traen, y sin propagarla la vista las mostraba como "ER" -- que es lo
        # que muestra cuando la fuente viene vacía.
        return {"hoja": d.get("hoja"), "celda": d.get("celda"),
                "fuente": d.get("fuente")}

    # INGRESOS. Un proyecto puede tener varias fuentes de ingreso bruto (columnas
    # "Venta ($)" independientes, ej. Terpel 1 / Terpel 2): el parser ya devuelve una
    # línea por fuente (más Venta/Compra en bolsa si aplica) en `ingresos_detalle`,
    # con su etiqueta, celda y valor propios. Se guarda una línea por cada una, para
    # normal/NEU/NITRO por igual (antes 'normal' colapsaba todo en una sola línea
    # "Ingreso Bruto <comercializador>" y perdía las fuentes adicionales).
    detalle_ingresos = parsed.get("ingresos_detalle", [])
    if detalle_ingresos:
        for d in detalle_ingresos:
            lineas.append({"grupo": "ingresos", "concepto": d["concepto"], "valor": d["valor"], **_orig(d)})
    elif tipo not in ("neu", "nitro"):
        # Fallback: el parser no detectó ninguna fuente (ER atípico); al menos
        # dejar una línea "Ingreso Bruto" en 0 para que la usuaria pueda mapearla.
        com = parsed.get("comercializador") or ""
        etiqueta_ing = f"Ingreso Bruto{(' ' + com) if com else ''}".strip()
        lineas.append({"grupo": "ingresos", "concepto": etiqueta_ing, "valor": parsed["ingreso_bruto"]})

    # COMERCIALIZACIÓN XM (desglosada)
    for c in parsed.get("comercializacion", []):
        lineas.append({"grupo": "comercializacion", "concepto": c["concepto"], "valor": c["valor"], **_orig(c)})

    # COSTOS OPERATIVOS (IVA 19% como línea aparte sobre mantenimiento e internet)
    for c in parsed.get("costos", []):
        lineas.append({"grupo": "costos", "concepto": c["concepto"], "valor": c["valor"], **_orig(c)})
        if c.get("iva"):
            lineas.append({
                "grupo": "costos",
                "concepto": f"IVA {c['concepto']}",
                "valor": round(c["valor"] * IVA, 2),
                "hoja": None, "celda": None,  # el IVA es derivado, no es una celda del ER
            })

    # FACTURAS DE SERVICIO
    for f in parsed.get("facturas", []):
        lineas.append({"grupo": "facturas", "concepto": f["concepto"], "valor": f["valor"], **_orig(f)})

    return lineas


def _rango_periodo(periodo: str) -> tuple[date, date]:
    """Devuelve (primer_dia, ultimo_dia) del mes 'YYYY-MM'."""
    y, m = (int(x) for x in periodo.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _procesar_invs(rows, periodo: str | None = None) -> list[dict]:
    """Normaliza filas de proyecto_inversionistas (ya consultadas) a la lista de
    inversionistas con fracción/pct. Puro respecto a la DB — sirve para el caso de
    un proyecto y para el batch. rows: objetos con id, porcentaje_participacion,
    fecha_inicio, fecha_fin, razon_social_nombre."""
    # Filtrar por período: solo inversionistas activos durante el mes. Un
    # inversionista está activo si empezó antes del fin de mes y no terminó antes
    # de que empiece (misma lógica que match_inversionista de liquidaciones).
    if periodo:
        ini, fin = _rango_periodo(periodo)
        activos = [
            r for r in rows
            if (r.fecha_inicio is None or r.fecha_inicio <= fin)
            and (r.fecha_fin is None or r.fecha_fin >= ini)
        ]
        # Si el filtro deja a todos fuera (datos sin fechas o inconsistentes),
        # caer al conjunto completo para no perder la división.
        if activos:
            rows = activos

    # porcentaje_participacion puede venir en 0-1 (0.5 = 50%) o en 0-100 (50 = 50%)
    # según cómo se cargó cada proyecto (datos inconsistentes en proyecto_inversionistas).
    # Detección ROBUSTA por la SUMA de los porcentajes activos (no por un umbral por
    # valor, que era frágil): la participación total de un proyecto siempre debe
    # acercarse a "el 100%". Si la suma está más cerca de 1 que de 100, los datos
    # están en fracción (0-1); si está más cerca de 100, están en 0-100.
    #   - normal/NEU/NITRO con datos 0-1 (ej. Cacica 0.5/0.5 → suma 1.0)  → fracción
    #   - un único inversionista al 100% guardado como 1.0 (suma 1.0)     → fracción → 100%
    #   - datos 0-100 (ej. 60/40 → suma 100)                              → 0-100
    # Esto corrige el bug de que single 1.0 mostraba "1%" y El Son 0.21 mostraba "0.21%".
    pcts = [float(r.porcentaje_participacion) for r in rows if r.porcentaje_participacion is not None]
    suma = sum(pcts)
    # Si la suma está más cerca de 1 que de 100 ⇒ escala fracción (0-1); si no ⇒ 0-100.
    # Comparar distancias (no un umbral por valor) tolera sumas parciales cuando no
    # todos los inversionistas están activos el mes completo (ej. El Son en mayo).
    escala_0_1 = bool(pcts) and abs(suma - 1.0) <= abs(suma - 100.0)

    out = []
    for r in rows:
        raw = float(r.porcentaje_participacion) if r.porcentaje_participacion is not None else None
        # pct = porcentaje en forma 0-100 (lo que se muestra y se guarda en la línea);
        # fraccion = factor 0-1 para dividir los valores.
        if raw is None:
            pct = fraccion = None
        elif escala_0_1:
            pct, fraccion = raw * 100.0, raw
        else:
            pct, fraccion = raw, raw / 100.0
        out.append({
            "id": r.id,
            # El cliente, no la fila de participación: es lo que permite cruzar
            # con `contratos_servicio.inversionista_id` sin comparar nombres.
            "cliente_id": r.cliente_id,
            "nombre": r.razon_social_nombre or "—",
            "fraccion": fraccion,
            "pct": pct,
        })
    # Si ningún % está definido, repartir en partes iguales como fallback.
    sin_pct = [o for o in out if o["fraccion"] is None]
    if out and sin_pct:
        falt = 1.0 - sum(o["fraccion"] or 0 for o in out if o["fraccion"] is not None)
        cada = (falt / len(sin_pct)) if falt > 0 else (1.0 / len(out))
        for o in sin_pct:
            o["fraccion"] = cada
            o["pct"] = cada * 100
    return out


def _inversionistas_de(proyecto_id: int, periodo: str | None = None) -> list[dict]:
    """Los inversionistas del proyecto, con su fracción de reparto."""
    return _procesar_invs(_filas_inversionistas([proyecto_id]), periodo)


def _filas_inversionistas(proyecto_ids) -> list:
    """Las filas crudas que `_procesar_invs` sabe leer, en UNA consulta."""
    return list(
        ProyectoInversionista.objects
        .filter(proyecto_id__in=list(proyecto_ids))
        .select_related("cliente")
        .annotate(razon_social_nombre=F("cliente__razon_social_nombre"))
    )


def _inversionistas_de_batch(proyecto_ids, periodo: str | None = None) -> dict[int, list[dict]]:
    """Igual que _inversionistas_de pero para varios proyectos en UNA sola query
    (evita el N+1 en redividir). La detección de escala de % es por proyecto."""
    ids = list({pid for pid in proyecto_ids})
    if not ids:
        return {}
    rows = _filas_inversionistas(ids)
    por_proy: dict[int, list] = {}
    for r in rows:
        por_proy.setdefault(r.proyecto_id, []).append(r)
    return {pid: _procesar_invs(por_proy.get(pid, []), periodo) for pid in ids}
