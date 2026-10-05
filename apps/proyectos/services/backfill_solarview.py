"""Vínculo con SolarView (`project_id_solarview`).

Puerto de `app/services/proyectos_backfill_solarview.py`.

`project_id_solarview` es UNIQUE: si dos filas nuestras matchean al mismo
proyecto de SolarView, solo la primera se queda con el vínculo — y la otra se
reporta con ese motivo en vez de fallar en silencio.
"""

from __future__ import annotations

import logging

from apps.comun.nombre_matching import mejor_candidato
from apps.proyectos.models import Proyecto

from apps.comun.integraciones.solarview_client import SolarViewClient

logger = logging.getLogger("operaciones.proyectos.solarview")

UMBRAL_PROJECT_ID_SOLARVIEW = 0.95


def _match_solarview_seguro(proyecto: Proyecto, solarview_projects: list[dict]) -> dict | None:
    candidatos = [(p, [p.get("name")]) for p in solarview_projects if p.get("name")]
    item, score = mejor_candidato(proyecto.nombre_comercial, candidatos)
    return item if item and score >= UMBRAL_PROJECT_ID_SOLARVIEW else None


def backfill_project_id_solarview(apply: bool = False) -> dict:
    """Corrida masiva sobre proyectos sin `project_id_solarview`. Ver
    scripts/backfill_project_id_solarview.py para el CLI (dry-run por
    defecto)."""
    candidatos_proyecto = list(
        Proyecto.objects
        .filter(deleted_at__isnull=True, project_id_solarview__isnull=True)
        .order_by("nombre_comercial")
    )
    if not candidatos_proyecto:
        return {"ok": True, "revisados": 0, "asignados": [], "sin_match_seguro": []}

    client = SolarViewClient()
    if not client.enabled:
        return {"ok": False, "error": "Credenciales de SolarView no configuradas."}
    try:
        solarview_projects = client.get_company_projects()
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo listar proyectos de SolarView: {exc}"}
    if not solarview_projects:
        return {"ok": False, "error": "SolarView no devolvió proyectos"}

    # project_id_solarview es UNIQUE -- si dos filas de nuestra BD matchean al
    # mismo proyecto de SolarView, solo la primera se queda con el vínculo.
    usados_solarview_id = set(
        Proyecto.objects
        .filter(deleted_at__isnull=True, project_id_solarview__isnull=False)
        .values_list("project_id_solarview", flat=True)
    )

    asignados: list[dict] = []
    sin_match_seguro: list[dict] = []
    a_guardar: list[Proyecto] = []

    for p in candidatos_proyecto:
        item = _match_solarview_seguro(p, solarview_projects)
        if not item:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": "sin match seguro en SolarView",
            })
            continue

        nuevo_id = str(item["id"])
        if nuevo_id in usados_solarview_id:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": f"matcheó con SolarView id={nuevo_id}, pero ya está tomado por otro proyecto en esta corrida",
            })
            continue

        asignados.append({
            "proyecto_id": p.id, "nombre": p.nombre_comercial,
            "cambios": {"proyecto.project_id_solarview": nuevo_id},
        })
        usados_solarview_id.add(nuevo_id)
        if apply:
            p.project_id_solarview = nuevo_id
            a_guardar.append(p)

    if a_guardar:
        Proyecto.objects.bulk_update(a_guardar, ["project_id_solarview"])

    return {
        "ok": True,
        "revisados": len(candidatos_proyecto),
        "asignados": asignados,
        "sin_match_seguro": sin_match_seguro,
    }


def sincronizar_project_id_solarview_si_aplica(proyecto: Proyecto) -> str | None:
    """Best-effort para UN proyecto, en el momento de crearlo/confirmarlo (ver
    app/api/v1/proyectos.py). Nunca sobreescribe, y nunca lanza."""
    if proyecto.project_id_solarview:
        return None
    try:
        client = SolarViewClient()
        if not client.enabled:
            return None
        solarview_projects = client.get_company_projects()
        if not solarview_projects:
            return None
        item = _match_solarview_seguro(proyecto, solarview_projects)
        if not item:
            return None
        nuevo_id = str(item["id"])
        conflicto = Proyecto.objects.filter(
            project_id_solarview=nuevo_id,
        ).exclude(pk=proyecto.id).first()
        if conflicto:
            return None
        proyecto.project_id_solarview = nuevo_id
        proyecto.save(update_fields=["project_id_solarview"])
        return nuevo_id
    except Exception:
        logger.warning(
            "No se pudo sincronizar project_id_solarview para proyecto %s (%s)",
            proyecto.id, proyecto.nombre_comercial, exc_info=True,
        )
        return None
