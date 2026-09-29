"""Datos en vivo del informe de puesta en marcha: inversores y Gaia.

Los inversores salen de SolarView por `apps.monitoreo.services.inversores_en_vivo`,
la misma función que usa el informe mensual. La instantánea de Gaia se consulta
acá, con caché corto: sin él, cada clic dentro de la misma ficha golpea la API.

`ponytail: caché en dicts de módulo con TTL de 60 s`. Al subir workers cada
proceso tendrá el suyo, lo que solo significa más llamadas, no datos incorrectos.
"""

import logging
import time

from apps.monitoreo.services import inversores_en_vivo

logger = logging.getLogger("operaciones.informe_om")

TTL = 60

_frontera_cache: dict[int, tuple[float, dict]] = {}

_gaia = None


def _cliente_gaia():
    global _gaia
    if _gaia is None:
        from app.services.mgs.gaia_client import GaiaClient

        _gaia = GaiaClient()
    return _gaia if _gaia.enabled else None


def inversores(proyecto) -> list[dict]:
    """Inversores en vivo de SolarView, no los de `proyecto_inversores`: la
    fuente en vivo trae potencia actual y estado, que la tabla no tiene. Sin
    inversores (o sin `project_id_solarview`) la ficha muestra la lista vacía."""
    return inversores_en_vivo.inversores(proyecto)[0]


def frontera(proyecto) -> dict:
    """Instantánea eléctrica de Gaia del medidor principal y del de respaldo."""
    guardado = _frontera_cache.get(proyecto.id)
    if guardado and time.monotonic() - guardado[0] < TTL:
        return guardado[1]

    vacio = {"principal": None, "respaldo": None}
    gaia = _cliente_gaia()
    if gaia is None:
        return vacio

    from app.services.mgs.gaia_client import (
        build_db_proyecto_frt_map, find_gaia_node_pair,
    )
    from apps.fronteras import models as fr_models

    fronteras = list(
        fr_models.Frontera.objects
        .filter(
            tipo_frontera__in=["generacion", "generacion_consumo"],
            codigo_frontera__isnull=False,
        )
        .values_list("proyecto_id", "codigo_frontera")
    )
    mapa = build_db_proyecto_frt_map(fronteras)
    nodo_principal, nodo_respaldo = find_gaia_node_pair(
        gaia=gaia, proyecto_id=proyecto.id, db_proyecto_frt_map=mapa
    )

    capacidad_kwp = float(proyecto.potencia_ac_kw or 0) or None
    resultado = {
        "principal": _instantanea(gaia, nodo_principal, capacidad_kwp),
        "respaldo": _instantanea(gaia, nodo_respaldo, capacidad_kwp),
    }
    _frontera_cache[proyecto.id] = (time.monotonic(), resultado)
    return resultado


def _instantanea(gaia, nodo_id, capacidad_kwp: float | None = None) -> dict | None:
    if not nodo_id:
        return None
    try:
        medida = gaia.get_node_electrical_snapshot(nodo_id)
    except Exception:
        logger.warning("no se pudo obtener snapshot de Gaia node_id=%s", nodo_id)
        return None
    if not medida:
        return None

    # La clave se llama `eae_wh` pero CONTIENE kWh: quien la produce la deja ya
    # normalizada (`gaia_client.py`, "Cumulative energy today [kWh]", con la
    # variable llamada `_eae_kwh`). Dividir entre 1000 creyéndole al nombre
    # reportaba un día de 5.995 kWh como 6,0 (arreglado en FastAPI el
    # 2026-09-03; el puerto había heredado el error).
    energia_kwh = medida.get("eae_wh")

    # `ap_total` SÍ viene crudo: `gaia_client` lo suma tal cual de la API
    # ("Active power [W]") y solo normaliza la unidad en su serie temporal, no
    # en este escalar. Los nodos no coinciden entre sí —unos entregan vatios y
    # otros kilovatios— así que exponerlo directo estaba 1000× alto en la mitad
    # de los medidores. `divisor_a_kw` decide cuál es cuál por la magnitud.
    from app.services.mgs.medidor_tiempo_real import divisor_a_kw

    potencia_kw = None
    ap_total = medida.get("ap_total")
    if ap_total is not None:
        try:
            bruto = float(ap_total)
            potencia_kw = round(bruto / divisor_a_kw(abs(bruto), capacidad_kwp), 2)
        except (TypeError, ValueError):
            potencia_kw = None

    return {
        "voltaje_v": [medida.get("vp1"), medida.get("vp2"), medida.get("vp3")],
        "corriente_a": [medida.get("cp1"), medida.get("cp2"), medida.get("cp3")],
        "potencia_activa_kw": potencia_kw,
        "potencia_reactiva_kvar": medida.get("rp_total"),
        "factor_potencia": medida.get("pf_avg"),
        "energia_exportada_hoy_kwh": (
            round(energia_kwh, 2) if energia_kwh is not None else None
        ),
        "ultima_actualizacion": medida.get("last_time"),
    }
