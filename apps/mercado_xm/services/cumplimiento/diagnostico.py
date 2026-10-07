"""Diagnóstico de enlaces contrato → GESCON → planta, y su corrección puntual.

Puerto de `/diagnostico` y `/fix-enlaces`.

`fix_enlaces` **no es una herramienta general**: lleva la lista de arreglos en el
código, está limitada a un correo concreto y es idempotente. Se conserva tal cual
porque el endpoint sigue publicado; si algún día se retira, se retira entero.
"""

from __future__ import annotations

import logging
from datetime import date

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from apps.mercado_xm.models import AsicSolicitud
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato
from apps.proyectos.models import Proyecto
from apps.contratos.services import contrato_partes

from .consultas import _asc_nulls_first, _asc_nulls_last, _resolve_gescon

logger = logging.getLogger("operaciones.cumplimiento")


def diagnostico_enlaces() -> dict:
    """Volcado de todos los enlaces contrato → GESCON → proyecto → sub_project.

    Es la herramienta para entender por qué una planta no aparece donde se
    espera: muestra el crudo de `asic_solicitudes` junto al resultado ya resuelto
    por vigencias, y los dos deberían explicar cualquier diferencia.
    """
    today = hoy_col()
    year, month = today.year, today.month

    contratos = (
        PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .prefetch_related(contrato_partes.CON_PARTES)
        .order_by(_asc_nulls_last("nombre_interno"), "id")
    )

    result = []
    for c in contratos:
        gescon_raw = []
        resolved = []
        if c.numero_codigo_contrato:
            raw_records = (
                AsicSolicitud.objects
                .select_related("proyecto")
                .filter(contrato_interno=c.numero_codigo_contrato)
                .order_by(_asc_nulls_first("fecha_solicitud"))
            )
            for r in raw_records:
                gescon_raw.append({
                    "id": r.id,
                    "tipo": r.tipo_solicitud or None,
                    "estado": r.estado_solicitud or None,
                    "codigo_sic": r.codigo_sic_contrato,
                    "proyecto_id": r.proyecto_id,
                    "planta": r.proyecto.nombre_comercial if r.proyecto else None,
                    "sub_project": r.proyecto.sub_project if r.proyecto else None,
                    "pct_despacho": float(r.porcentaje_despacho) if r.porcentaje_despacho else None,
                    "es_duplicado": bool(r.es_duplicado),
                    "reemplaza_anterior": bool(r.reemplaza_anterior),
                    "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
                    "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
                })
            resolved_asics = _resolve_gescon(c.numero_codigo_contrato, year, month)
            for a in resolved_asics:
                resolved.append({
                    "asic_id": a.id,
                    "planta": a.proyecto.nombre_comercial if a.proyecto else None,
                    "sub_project": a.proyecto.sub_project if a.proyecto else None,
                    "pct_despacho": float(a.porcentaje_despacho) if a.porcentaje_despacho else None,
                    "es_duplicado": bool(a.es_duplicado),
                })

        result.append({
            "contrato_id": c.id,
            "nombre_interno": c.nombre_interno,
            "numero_codigo_contrato": c.numero_codigo_contrato,
            "comprador": c.comprador_nombre,
            "tipo": c.tipo_contrato or "venta",
            "gescon_raw": gescon_raw,
            "gescon_resolved": resolved,
            "n_plantas_activas": len(resolved),
        })

    projects_info = [
        {"id": p.id, "nombre": p.nombre_comercial, "sub_project": p.sub_project,
         "estado": p.estado or None}
        for p in Proyecto.objects
        .filter(sub_project__isnull=False)
        .order_by("nombre_comercial")
    ]

    return {"contratos": result, "proyectos_con_sub_project": projects_info}


def fix_enlaces(email_usuario: str) -> dict:
    """Todo o nada: el original hacía un solo `commit()` al final y acá se
    conserva esa semántica, para que un fallo a mitad no deje media corrección."""
    with transaction.atomic():
        return _fix_enlaces(email_usuario)


def _fix_enlaces(email_usuario: str) -> dict:
    """Crea las asignaciones GESCON que faltan para unos contratos concretos.

    Es una corrección puntual con la lista de arreglos EN EL CÓDIGO, no una
    herramienta general: se ejecuta una vez y es idempotente (si el registro ya
    existe, no lo duplica). El filtro por correo viene del original.
    """
    if email_usuario != "juanjose@unergy.io":
        raise PermissionDenied("Solo el admin puede ejecutar esta acción")

    import unicodedata

    def norm(s):
        s = unicodedata.normalize("NFD", str(s or ""))
        return "".join(c for c in s if unicodedata.category(c) != "Mn").strip().lower()

    projects = Proyecto.objects.all()
    proj_by_norm = {norm(p.nombre_comercial): p for p in projects}

    FIXES = [
        {
            "contrato_interno": "MNRNEU-2024-006",
            "nombre_interno": "NEU II - Ibirico",
            "plantas": [
                {"nombre_norm": "mgs 0021 ibirico", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2025-03-01", "fecha_fin": "2040-12-31"},
            ],
        },
        {
            "contrato_interno": "OC.UNER-063-2025",
            "nombre_interno": "Nitro Energy",
            "plantas": [
                {"nombre_norm": "mgs 0040 cacica", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2026-01-01", "fecha_fin": "2040-12-31"},
                {"nombre_norm": "mgs 0041 piloneras", "pct": 1.0, "duplicado": False,
                 "fecha_inicio": "2026-01-01", "fecha_fin": "2040-12-31"},
            ],
        },
    ]

    actions = []

    for fix in FIXES:
        contrato_code = fix["contrato_interno"]
        for planta_def in fix["plantas"]:
            proj = proj_by_norm.get(planta_def["nombre_norm"])
            if not proj:
                actions.append({"action": "skip", "reason": f"Proyecto '{planta_def['nombre_norm']}' no encontrado en BD",
                                "contrato": contrato_code})
                continue

            existing = AsicSolicitud.objects.filter(
                contrato_interno=contrato_code,
                proyecto_id=proj.id,
                tipo_solicitud="registro",
                estado_solicitud="publicado",
            ).first()

            if existing:
                actions.append({"action": "exists", "contrato": contrato_code,
                                "planta": proj.nombre_comercial, "asic_id": existing.id})
                continue

            new_asic = AsicSolicitud.objects.create(
                proyecto_id=proj.id,
                contrato_interno=contrato_code,
                tipo_solicitud="registro",
                estado_solicitud="publicado",
                fecha_inicio=date.fromisoformat(planta_def["fecha_inicio"]),
                fecha_fin=date.fromisoformat(planta_def["fecha_fin"]),
                porcentaje_despacho=planta_def["pct"],
                es_duplicado=planta_def["duplicado"],
                reemplaza_anterior=False,
                tipo_mercado="No regulado",
                nombre_interno=fix["nombre_interno"],
            )
            actions.append({"action": "created", "contrato": contrato_code,
                            "planta": proj.nombre_comercial, "sub_project": proj.sub_project,
                            "asic_id": new_asic.id})

    # Fix Uruaco duplicate in KLIK
    klik_uruaco = list(
        AsicSolicitud.objects
        .select_related("proyecto")
        .filter(contrato_interno="OM-UNERGY-010-2025", estado_solicitud="publicado")
    )

    uruaco_records = [r for r in klik_uruaco if r.proyecto and norm(r.proyecto.nombre_comercial).find("uruaco") >= 0]
    if len(uruaco_records) > 1:
        for dup in uruaco_records[1:]:
            actions.append({"action": "delete_duplicate", "contrato": "OM-UNERGY-010-2025",
                            "planta": dup.proyecto.nombre_comercial if dup.proyecto else None,
                            "asic_id": dup.id})
            dup.delete()
    elif len(uruaco_records) == 1 and uruaco_records[0].es_duplicado:
        uruaco_records[0].es_duplicado = False
        uruaco_records[0].save(update_fields=["es_duplicado"])
        actions.append({"action": "unflag_duplicate", "contrato": "OM-UNERGY-010-2025",
                        "planta": uruaco_records[0].proyecto.nombre_comercial,
                        "asic_id": uruaco_records[0].id})
    elif not uruaco_records:
        proj_uruaco = proj_by_norm.get("minigranja solar uruaco")
        if proj_uruaco:
            new_asic = AsicSolicitud.objects.create(
                proyecto_id=proj_uruaco.id,
                contrato_interno="OM-UNERGY-010-2025",
                tipo_solicitud="registro",
                estado_solicitud="publicado",
                fecha_inicio=date(2026, 4, 1),
                fecha_fin=date(2041, 3, 31),
                porcentaje_despacho=1.0,
                es_duplicado=False,
                reemplaza_anterior=False,
                tipo_mercado="No regulado",
                nombre_interno="KLIK - Uruaco",
            )
            actions.append({"action": "created", "contrato": "OM-UNERGY-010-2025",
                            "planta": proj_uruaco.nombre_comercial, "sub_project": proj_uruaco.sub_project,
                            "asic_id": new_asic.id})
        else:
            actions.append({"action": "skip", "reason": "Uruaco no encontrado en BD",
                            "contrato": "OM-UNERGY-010-2025"})

    return {"status": "ok", "actions": actions}
