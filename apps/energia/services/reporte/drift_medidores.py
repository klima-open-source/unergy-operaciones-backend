"""Drift de medidores: si Quoia ya muestra otro número, la fila vuelve a revisión.

Puerto de `app/services/reporte_energia/drift_medidores.py`. Corre cada 5 min
entre las 4:00 y las 5:30 y vuelve a consultar Quoia para cada fila SIN
`revisar_manualmente`; si el medidor cambió, la marca.

**Los fallos de consulta se cuentan y se imprimen.** Antes se tragaban en
silencio: una falla sistemática —un token de Gaia vencido, que fallaría para
TODAS las filas— dejaba "marcadas=0", que se lee igual que "no hay drift"
(auditoría del Reporte ASIC, 2026-08-26).

**Solo Generación usa capacidad efectiva**: Consumo no tiene todavía un concepto
de capacidad definido.
"""

from __future__ import annotations

import traceback
from datetime import date

from apps.energia.models import ReporteEnergiaConsumo, ReporteEnergiaGeneracion
from apps.energia.services.reporte import curvas
from apps.energia.services.reporte.utils import curva_a_lista, curva_cambio

# `ponytail: el cliente de Quoia sigue en app/services/mgs/`.
from app.services.mgs.gaia_client import GaiaClient

def _revisar_tabla(
    Modelo, gaia, mapa_nodo, borders, fecha: date, var_name: str, es_generacion: bool,
) -> tuple[int, int]:
    filas = (
        Modelo.objects
        .filter(fecha=fecha, revisar_manualmente=False)
        .select_related("frontera__proyecto")
    )

    marcadas = 0
    fallos = 0
    a_marcar = []
    for rep in filas:
        front = rep.frontera
        proyecto = front.proyecto if front.proyecto_id else None
        if not rep.curva_medidor_principal and not rep.curva_medidor_respaldo:
            continue  # nada persistido con qué comparar (Caso CGM sin medidor consultado, o fila vieja)
        meta = borders.get((front.codigo_frontera or "").strip().lower())
        if not meta:
            continue
        # Solo Generación -- Consumo no tiene un concepto de capacidad
        # efectiva definido todavía (decidido con Sara 2026-08-26).
        capacidad_efectiva_mw = (
            float(proyecto.potencia_ac_kw) / 1000
            if es_generacion and proyecto and proyecto.potencia_ac_kw is not None else None
        )
        try:
            curva_p, curva_r = curvas.curva_medidor_en_vivo(
                gaia, mapa_nodo, meta.get("main_meter"), meta.get("backup_meter"),
                str(fecha), front.codigo_frontera, var_name, capacidad_efectiva_mw,
            )
        except Exception as exc:
            # El aviso es informativo -- si Quoia falla para ESTA frontera se
            # sigue con las demás, pero antes esto se tragaba en silencio sin
            # ningún log: una falla sistemática (ej. token de Gaia vencido,
            # que fallaría para TODAS las filas) corría cada 5 min de 4:00 a
            # 5:30am sin que nadie se enterara -- "marcadas=0" se veía igual
            # a "no hay drift" que a "no se pudo revisar nada" (auditoría
            # Reporte ASIC 2026-08-26). Se cuenta y se imprime -- mismo
            # patrón print-based del resto de este módulo.
            fallos += 1
            print(f"[reporte_energia] drift_medidores frontera={front.codigo_frontera} fecha={fecha} "
                  f"error consultando Quoia: {exc}")
            continue

        cambio_ppal = curva_cambio(rep.curva_medidor_principal, curva_a_lista(curva_p))
        cambio_resp = curva_cambio(rep.curva_medidor_respaldo, curva_a_lista(curva_r))
        if cambio_ppal or cambio_resp:
            rep.revisar_manualmente = True
            a_marcar.append(rep)
            marcadas += 1

    if a_marcar:
        Modelo.objects.bulk_update(a_marcar, ["revisar_manualmente"])
    return marcadas, fallos


def verificar_drift_medidores(fecha: date) -> dict:
    """Vuelve a consultar Quoia (mismo helper liviano del detalle, sin
    recuperación activa) para cada fila SIN revisar_manualmente y, si el
    medidor ya muestra un valor distinto, marca revisar_manualmente=True.

    Retorna {'generacion': N, 'consumo': N, 'fallos': M} -- cuántas filas se
    marcaron en cada tabla, y cuántas no se pudieron ni siquiera consultar
    (sumado entre ambas tablas) -- ver _revisar_tabla()."""
    gaia = GaiaClient()
    mapa_nodo = curvas.construir_mapa_medidor_nodo(gaia)
    borders = curvas.construir_mapa_borders(gaia)
    marcadas_gen, fallos_gen = _revisar_tabla(
        ReporteEnergiaGeneracion, gaia, mapa_nodo, borders, fecha, "eae", True)
    marcadas_con, fallos_con = _revisar_tabla(
        ReporteEnergiaConsumo, gaia, mapa_nodo, borders, fecha, "iae", False)
    return {"generacion": marcadas_gen, "consumo": marcadas_con, "fallos": fallos_gen + fallos_con}


def verificar_drift_medidores_background(fecha: date) -> dict:
    """Igual que verificar_drift_medidores(), pero abre su propia sesión de
    BD -- mismo patrón que ejecutar_dia_background() (orquestador.py),
    pensada para encadenarse después de clasificar en el scheduler."""
    from django.db import close_old_connections

    close_old_connections()
    try:
        return verificar_drift_medidores(fecha)
    except Exception:
        print(f"[reporte_energia] verificar_drift_medidores fecha={fecha} FALLÓ:")
        print(traceback.format_exc())
        return {"generacion": 0, "consumo": 0, "error": True}
    finally:
        close_old_connections()
