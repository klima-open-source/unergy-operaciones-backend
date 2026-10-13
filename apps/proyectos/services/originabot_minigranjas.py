"""La minigranja de originabot (TSF) de cada planta: `Proyecto.originabot_minifarm_id`.

Es la llave directa entre una planta de Operaciones y su minigranja en TSF. La
usan la unión de PPAs (`apps/ppa/services/originabot_sync.py`) y la sugerencia
de proyectos de TSF que faltan aquí (modal «Nuevo proyecto»).

«Importar desde TSF» la guarda al crear. Para las demás se resuelve UNA vez y se
guarda: la minigranja que se llame exactamente como su `origina_code` o, si no
hay, la ÚNICA que encuentre su código TSF. originabot renombró las antiguas
(`COLCORT7P1` es «Minigranja 0030 - Chimá Oriente»), pero su búsqueda sigue
indexando el código. Con más de una candidata no se adivina.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError

from apps.comun.integraciones.originabot import ClienteOriginabot
from apps.proyectos.models import Proyecto
from apps.proyectos.services.tsf_sync import _tsf_code_from_base_name

logger = logging.getLogger(__name__)


def resolver(proyecto: Proyecto, ob: ClienteOriginabot) -> int | None:
    """La minigranja de la planta en originabot, sin escribir. None si no se sabe."""
    if proyecto.origina_code:
        exactas = [m for m in ob.buscar_minifarms(proyecto.origina_code) if m.get("name") == proyecto.origina_code]
        if len(exactas) == 1:
            return exactas[0]["id"]
    codigo = proyecto.codigo_tsf or _tsf_code_from_base_name(proyecto.origina_code)
    candidatas = ob.buscar_minifarms(codigo) if codigo else []
    return candidatas[0]["id"] if len(candidatas) == 1 else None


def minigranja_de(proyecto: Proyecto, ob: ClienteOriginabot) -> int | None:
    """La minigranja de la planta; si no la tenía guardada, la resuelve y la guarda."""
    if proyecto.originabot_minifarm_id:
        return proyecto.originabot_minifarm_id
    minifarm = resolver(proyecto, ob)
    if minifarm is None:
        return None
    try:
        Proyecto.objects.filter(pk=proyecto.pk).update(originabot_minifarm_id=minifarm)
    except IntegrityError:
        # Otra planta viva ya tiene esa minigranja: es un duplicado que una persona
        # tiene que resolver; aquí no se adivina cuál es la buena.
        logger.warning("Minigranja %s ya asignada a otra planta (proyecto %s)", minifarm, proyecto.pk)
        return None
    proyecto.originabot_minifarm_id = minifarm
    return minifarm
