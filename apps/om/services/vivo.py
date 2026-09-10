"""Datos en vivo del informe de puesta en marcha: Solenium y Gaia.

Los dos se consultan al abrir la ficha, así que van con caché corto: sin él,
cada clic dentro de la misma ficha golpea las dos APIs externas.

`ponytail: caché en dicts de módulo con TTL de 60 s`. Igual que en el resto de
la migración, vale con `WORKERS=1`; al subir workers cada proceso tendrá el
suyo, lo que solo significa más llamadas, no datos incorrectos.
"""

import logging
import re
import time

logger = logging.getLogger("operaciones.informe_om")

TTL = 60

_inversores_cache: dict[str, tuple[float, list]] = {}
_frontera_cache: dict[int, tuple[float, dict]] = {}

_solenium = None
_gaia = None

# Solenium no expone la capacidad nominal; se aproxima leyendo el número con
# el que empieza el nombre del dispositivo ("330KTL-Inversor1" -> 330). Es una
# aproximación del MODELO: puede no coincidir con la ficha técnica.
_CAPACIDAD_EN_NOMBRE = re.compile(r"^(\d+(?:\.\d+)?)")


def _cliente_solenium():
    global _solenium
    if _solenium is None:
        from app.services.mgs.solenium_client import SoleniumClient

        _solenium = SoleniumClient()
    return _solenium if _solenium.enabled else None


def _cliente_gaia():
    global _gaia
    if _gaia is None:
        from app.services.mgs.gaia_client import GaiaClient

        _gaia = GaiaClient()
    return _gaia if _gaia.enabled else None


def capacidad_kw(nombre: str | None) -> float | None:
    if not nombre:
        return None
    encontrado = _CAPACIDAD_EN_NOMBRE.match(nombre)
    return float(encontrado.group(1)) if encontrado else None


def inversores(proyecto) -> list[dict]:
    """Inversores según la API de Solenium, no según `proyecto_inversores`.

    La fuente en vivo trae potencia actual y estado, que la tabla no tiene.

    NOTA: sigue en Solenium, no SolarView. Migrar esta fuente concreta
    (SolarView si hay `project_id_solarview`, si no Solenium) queda pendiente
    aparte.
    """
    if not proyecto.project_id_solenium:
        return []
    sol_id = str(proyecto.project_id_solenium)

    guardado = _inversores_cache.get(sol_id)
    if guardado and time.monotonic() - guardado[0] < TTL:
        return guardado[1]

    cliente = _cliente_solenium()
    if cliente is None:
        return []
    try:
        crudos = cliente.get_project_inverters(int(sol_id))
    except Exception:
        logger.warning(
            "no se pudo obtener inversores de Solenium proyecto_id=%s", proyecto.id
        )
        return []

    lista = [
        {
            "id": inv.get("id"),
            "nombre": inv.get("dev_name") or f'Inversor {inv.get("id")}',
            "potencia_nominal_kw": capacidad_kw(inv.get("dev_name")),
            "power_kw": inv.get("power"),
            "state": inv.get("state"),
        }
        for inv in crudos if inv.get("id") is not None
    ]
    _inversores_cache[sol_id] = (time.monotonic(), lista)
    return lista


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
