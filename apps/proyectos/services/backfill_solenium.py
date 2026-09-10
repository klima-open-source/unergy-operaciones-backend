"""Info técnica de un proyecto, traída de Solenium.

Puerto de `app/services/proyectos_backfill_solenium.py`.

**Nunca sobreescribe**: solo rellena campos vacíos. `sincronizar_..._si_aplica`
además nunca lanza — si Solenium falla o no hay match seguro, el proyecto queda
como estaba y la creación no se bloquea.

`project_id_solenium` es UNIQUE: si dos filas nuestras matchean al mismo proyecto
de Solenium —pasa con duplicados reales—, solo la primera se queda con el vínculo.
"""

from __future__ import annotations

import logging

from django.db.models import Q

from apps.comun.nombre_matching import mejor_candidato
from apps.proyectos.models import Proyecto, ProyectoInfoTecnica

# `ponytail: el cliente de Solenium sigue en app/services/mgs/`. Es HTTP puro.
from app.services.mgs.solenium_client import SoleniumClient

logger = logging.getLogger("operaciones.proyectos.solenium")

UMBRAL_INFO_TECNICA_SOLENIUM = 0.95

# Solenium usa este texto como placeholder de "sin dato" -- no es un valor real.
_SIN_DATO = "Desconocida"


def _num(v) -> float | None:
    if v is None or v == _SIN_DATO:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _texto(v) -> str | None:
    if v is None or v == _SIN_DATO:
        return None
    return str(v)


def _match_solenium_por_id(proyecto: Proyecto, solenium_projects: list[dict]) -> dict | None:
    if not proyecto.project_id_solenium:
        return None
    try:
        pid = int(proyecto.project_id_solenium)
    except (TypeError, ValueError):
        return None
    return next((p for p in solenium_projects if p.get("id") == pid), None)


def _match_solenium_seguro(proyecto: Proyecto, solenium_projects: list[dict]) -> dict | None:
    item = _match_solenium_por_id(proyecto, solenium_projects)
    if item:
        return item
    candidatos = [(p, [p.get("name")]) for p in solenium_projects if p.get("name")]
    item, score = mejor_candidato(proyecto.nombre_comercial, candidatos)
    return item if item and score >= UMBRAL_INFO_TECNICA_SOLENIUM else None


def _cambios_info_tecnica(proyecto: Proyecto, it: ProyectoInfoTecnica, detalle: dict) -> dict:
    """Solo los campos vacíos -- nunca pisa un valor ya cargado. `detalle` es
    la respuesta de get_project_detail()['results']."""
    cambios: dict = {}

    # `installed_capacity` de Solenium es capacidad DC. Antes esto también
    # pisaba `proyecto.potencia_ac_kw` (que es AC, pese al nombre --
    # ver fix 2026-08-19 en upsert_info_tecnica) con este mismo valor DC, el
    # mismo error que ya se corrigió del lado manual. Este endpoint de
    # Solenium no expone un valor AC separado, así que simplemente no se
    # escribe -- mejor vacío que un DC mal etiquetado como AC.
    capacidad = _num(detalle.get("installed_capacity"))
    if capacidad is not None and it.capacidad_instalada_kwp is None:
        cambios["info_tecnica.capacidad_instalada_kwp"] = capacidad

    voltaje = _texto(detalle.get("grid_voltage"))
    if voltaje is not None and it.voltaje_red is None:
        cambios["info_tecnica.voltaje_red"] = voltaje

    paneles = detalle.get("panel_quantity")
    paneles = int(paneles) if isinstance(paneles, (int, float)) or (isinstance(paneles, str) and paneles.isdigit()) else None
    if paneles is not None and it.cantidad_total_paneles is None:
        cambios["info_tecnica.cantidad_total_paneles"] = paneles

    potencia_panel = _texto(detalle.get("panel_power"))
    if potencia_panel is not None and it.potencia_panel_kwp is None:
        cambios["info_tecnica.potencia_panel_kwp"] = potencia_panel

    potencia_inv = _texto(detalle.get("inverter_power"))
    if potencia_inv is not None and it.potencia_inversores_kwp is None:
        cambios["info_tecnica.potencia_inversores_kwp"] = potencia_inv

    cant_inv = detalle.get("inverter_quantity")
    cant_inv = int(cant_inv) if isinstance(cant_inv, (int, float)) or (isinstance(cant_inv, str) and cant_inv.isdigit()) else None
    if cant_inv is not None and it.cantidad_inversores is None:
        cambios["info_tecnica.cantidad_inversores"] = cant_inv

    return cambios


def _aplicar_cambios(proyecto: Proyecto, it: ProyectoInfoTecnica, cambios: dict) -> None:
    for clave, valor in cambios.items():
        objeto, campo = clave.split(".", 1)
        setattr(it if objeto == "info_tecnica" else proyecto, campo, valor)


def backfill_info_tecnica_solenium(apply: bool = False) -> dict:
    """Corrida masiva sobre proyectos existentes a los que les falte
    capacidad_instalada_kwp (como proxy de "sin info técnica de Solenium").
    Ver scripts/backfill_info_tecnica_solenium.py para el CLI (dry-run por
    defecto)."""
    # Sin capacidad instalada = sin info técnica de Solenium. Incluye a los
    # que no tienen fila de info técnica en absoluto.
    candidatos_proyecto = list(
        Proyecto.objects
        .filter(deleted_at__isnull=True)
        .filter(
            Q(info_tecnica__isnull=True)
            | Q(info_tecnica__capacidad_instalada_kwp__isnull=True)
        )
        .distinct()
        .prefetch_related("info_tecnica")
        .order_by("nombre_comercial")
    )
    if not candidatos_proyecto:
        return {"ok": True, "revisados": 0, "asignados": [], "sin_match_seguro": []}

    client = SoleniumClient()
    if not client.enabled:
        return {"ok": False, "error": "Credenciales de Solenium no configuradas."}
    try:
        solenium_projects = client.get_projects()
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo listar proyectos de Solenium: {exc}"}
    if not solenium_projects:
        return {"ok": False, "error": "Solenium no devolvió proyectos"}

    # project_id_solenium es UNIQUE -- si dos filas de nuestra BD (ej. un
    # duplicado real, se han visto casos) matchean al mismo proyecto de
    # Solenium, solo la primera se queda con el vínculo.
    usados_solenium_id = set(
        Proyecto.objects
        .filter(deleted_at__isnull=True, project_id_solenium__isnull=False)
        .values_list("project_id_solenium", flat=True)
    )

    asignados: list[dict] = []
    sin_match_seguro: list[dict] = []

    for p in candidatos_proyecto:
        item = _match_solenium_seguro(p, solenium_projects)
        if not item:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": "sin match seguro en Solenium",
            })
            continue

        try:
            detalle_resp = client.get_project_detail(item["id"])
        except Exception as exc:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": f"matcheó pero get_project_detail falló: {exc}",
            })
            continue
        detalle = (detalle_resp or {}).get("results") or {}

        it = p.info_tecnica.first() or ProyectoInfoTecnica(proyecto_id=p.id)
        cambios = _cambios_info_tecnica(p, it, detalle)

        # Vincular project_id_solenium de paso, si no lo tenía y nadie más lo
        # reclamó ya en esta misma corrida -- evita re-adivinar por nombre la
        # próxima vez.
        nuevo_id = str(item["id"])
        if not p.project_id_solenium and nuevo_id not in usados_solenium_id:
            cambios["proyecto.project_id_solenium"] = nuevo_id
            usados_solenium_id.add(nuevo_id)

        if not cambios:
            sin_match_seguro.append({
                "proyecto_id": p.id, "nombre": p.nombre_comercial,
                "motivo": "matcheó con Solenium, pero ese proyecto tampoco tiene datos técnicos diligenciados",
            })
            continue

        asignados.append({"proyecto_id": p.id, "nombre": p.nombre_comercial, "cambios": cambios})
        if apply:
            _aplicar_cambios(p, it, cambios)
            it.save()
            p.save()

    return {
        "ok": True,
        "revisados": len(candidatos_proyecto),
        "asignados": asignados,
        "sin_match_seguro": sin_match_seguro,
    }


def sincronizar_info_tecnica_solenium_si_aplica(proyecto: Proyecto) -> dict | None:
    """Best-effort para UN proyecto, en el momento de crearlo/confirmarlo (ver
    app/api/v1/proyectos.py). Nunca sobreescribe, y nunca lanza."""
    it = proyecto.info_tecnica.first()
    if it and it.capacidad_instalada_kwp is not None:
        return None  # ya tiene info técnica de alguna fuente, no hay nada que rellenar
    try:
        client = SoleniumClient()
        if not client.enabled:
            return None
        solenium_projects = client.get_projects()
        if not solenium_projects:
            return None
        item = _match_solenium_seguro(proyecto, solenium_projects)
        if not item:
            return None
        detalle_resp = client.get_project_detail(item["id"])
        detalle = (detalle_resp or {}).get("results") or {}

        if it is None:
            it = ProyectoInfoTecnica(proyecto_id=proyecto.id)
        cambios = _cambios_info_tecnica(proyecto, it, detalle)
        nuevo_id = str(item["id"])
        if not proyecto.project_id_solenium:
            conflicto = Proyecto.objects.filter(
                project_id_solenium=nuevo_id,
            ).exclude(pk=proyecto.id).first()
            if not conflicto:
                cambios["proyecto.project_id_solenium"] = nuevo_id
        if not cambios:
            return None
        _aplicar_cambios(proyecto, it, cambios)
        it.save()
        proyecto.save()
        return cambios
    except Exception:
        logger.warning(
            "No se pudo sincronizar info técnica de Solenium para proyecto %s (%s)",
            proyecto.id, proyecto.nombre_comercial, exc_info=True,
        )
        return None
