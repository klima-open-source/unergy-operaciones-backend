"""La nota de inversores que se le agrega a una alarma: "I3 Off", "Sin generacion"…

Sale de SolarView (`api.sole.tech`) por `project_id_solarview`. Hasta el
2026-09-29 la armaba `SoleniumChecker` (`app/services/mgs/`) contra Solenium,
que ya no responde, y emparejaba las plantas POR NOMBRE.

**Solo se consulta para las plantas que tienen una alarma en este ciclo.** La
nota no se usa para nada más, y el checker viejo la pedía para toda la flota en
cada ciclo: la disponibilidad entera y los inversores de cada planta por debajo
de 100 %. Un ciclo sin alarmas ahora no llama a SolarView.

Las reglas de la nota son las del checker viejo, y sus formateadores se reusan
tal cual para que el texto no cambie:

  - sin dato o categoría `disconnect`  → "Inv. desconectados"
  - disponibilidad 0                    → "Sin generacion"
  - por debajo de 100                   → los inversores que no están en
                                          "Grid-connected", abreviados
"""

import logging

from apps.proyectos.models import Proyecto

logger = logging.getLogger("operaciones.mgs")

ESTADO_SANO = "Grid-connected"

_cliente = None


def _cliente_solarview():
    global _cliente
    if _cliente is None:
        from apps.comun.integraciones.solarview_client import SolarViewClient

        _cliente = SolarViewClient()
    return _cliente if _cliente.enabled else None


def _ids_solarview(proyecto_ids) -> dict[int, int]:
    """`proyecto_id → id de SolarView`, de los que lo tienen y es numérico."""
    ids: dict[int, int] = {}
    filas = Proyecto.objects.filter(id__in=proyecto_ids).values_list("id", "project_id_solarview")
    for pid, sv in filas:
        try:
            ids[pid] = int(sv)
        except (TypeError, ValueError):
            continue
    return ids


def observaciones(proyecto_ids) -> dict[int, str]:
    """`{proyecto_id: nota}` para las plantas pedidas que tengan algo que decir."""
    from apps.monitoreo.services.alarmas.solenium_checker import _format_bad, _short_name, _short_state

    if not proyecto_ids:
        return {}
    cliente = _cliente_solarview()
    if cliente is None:
        return {}
    ids = _ids_solarview(proyecto_ids)
    if not ids:
        return {}

    disponibilidad = cliente.get_availability()
    if not disponibilidad:
        return {}

    notas: dict[int, str] = {}
    for pid, sv_id in ids.items():
        info = disponibilidad.get(sv_id)
        if info is None:
            continue
        avail = info.get("availability")
        if avail is None or info.get("category") == "disconnect":
            notas[pid] = "Inv. desconectados"
        elif avail == 0:
            notas[pid] = "Sin generacion"
        elif avail < 100:
            malos = [
                (_short_name(inv.get("dev_name") or "?"), _short_state(inv["state"]))
                for inv in cliente.get_project_inverters(sv_id)
                if inv.get("state") and inv["state"] != ESTADO_SANO
            ]
            if malos:
                notas[pid] = _format_bad(malos)

    logger.info("observaciones de inversores: %d plantas con alarma, %d con nota",
                len(proyecto_ids), len(notas))
    return notas
