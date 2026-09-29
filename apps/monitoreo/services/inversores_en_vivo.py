"""Los inversores de una planta en vivo, desde SolarView (`api.sole.tech`).

Una sola función para los dos lugares que los muestran: el informe de puesta
en marcha (`apps/om/services/vivo.py`) y el informe mensual de O&M
(`api/v1/monitoreo/queryset.py`). Hasta el 2026-09-29 eran dos copias contra
Solenium (`data.sole.tech`), que ya no responde.

**La planta se busca por `project_id_solarview`, sin fallback por nombre**,
igual que el resto del monitoreo solar (`apps/energia/services/solarview_monitoreo.py`).
Una planta sin ese id sale sin inversores y lo dice: adivinar por nombre ya
emparejó plantas equivocadas.

`ponytail: caché en un dict de módulo con TTL de 60 s`. Al abrir una ficha se
consulta varias veces seguidas; con varios workers cada uno tiene su copia, lo
que solo son más llamadas, no datos incorrectos.
"""

import logging
import re
import time

logger = logging.getLogger("operaciones.monitoreo")

TTL = 60

_cache: dict[int, tuple[float, list[dict]]] = {}
_cliente = None

# SolarView no expone la capacidad nominal; se aproxima leyendo el número con
# el que empieza el nombre del dispositivo ("330KTL-Inversor1" -> 330). Es una
# aproximación del MODELO: puede no coincidir con la ficha técnica.
_CAPACIDAD_EN_NOMBRE = re.compile(r"^(\d+(?:\.\d+)?)")


def capacidad_kw(nombre: str | None) -> float | None:
    if not nombre:
        return None
    encontrado = _CAPACIDAD_EN_NOMBRE.match(nombre)
    return float(encontrado.group(1)) if encontrado else None


def _cliente_solarview():
    global _cliente
    if _cliente is None:
        from app.services.mgs.solarview_client import SolarViewClient

        _cliente = SolarViewClient()
    return _cliente if _cliente.enabled else None


def _id_solarview(proyecto) -> int | None:
    try:
        return int(proyecto.project_id_solarview)
    except (TypeError, ValueError):
        return None


def inversores(proyecto) -> tuple[list[dict], str | None]:
    """(inversores, error). Nunca levanta: el error viaja como texto.

    Cada inversor: `{id, nombre, potencia_nominal_kw, power_kw, state}`.
    """
    sv_id = _id_solarview(proyecto)
    if sv_id is None:
        return [], "El proyecto no tiene project_id_solarview"

    guardado = _cache.get(sv_id)
    if guardado and time.monotonic() - guardado[0] < TTL:
        return guardado[1], None

    cliente = _cliente_solarview()
    if cliente is None:
        return [], "SolarView no configurado: falta SOLARVIEW_TOKEN"
    try:
        crudos = cliente.get_project_inverters(sv_id)
    except Exception:
        logger.warning("inversores de SolarView fallaron proyecto_id=%s", proyecto.id,
                       exc_info=True)
        return [], "SolarView no respondió"

    # El cliente devuelve [] tanto si la planta no tiene inversores como si la
    # llamada falló: no se puede distinguir desde acá, y no se guarda en caché
    # para que el siguiente intento vuelva a preguntar.
    if not crudos:
        return [], "SolarView no devolvió inversores para este proyecto"

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
    _cache[sv_id] = (time.monotonic(), lista)
    return lista, None
