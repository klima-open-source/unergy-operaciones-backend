"""Relleno de campos de proyecto desde `data/proyectos_solares_completo.json`.

Solo rellena lo que está VACÍO: nunca pisa un dato ya cargado. Ese es el
criterio que hace que se pueda volver a correr sin daño.

Ya NO aplica el mapeo hardcodeado de operadores de red (`OR_MAP`): desde el
2026-07-02 `proyectos.operador_red` se llena de forma fiable desde
`fronteras.operador_red` (dato oficial de GESCON) por el vínculo
`fronteras.proyecto_id`. Ese mapeo quedaba obsoleto en cuanto se agregaba un
proyecto nuevo y podía pisar en silencio el dato bueno con un valor viejo.
"""

import json
import re
from pathlib import Path

from django.db import transaction

from apps.proyectos import models as py_models

# Nombres del JSON que no se parecen al nombre comercial en la base.
NOMBRE_A_BUSQUEDA = {
    "MGS 0004 Valle de Gandalf": "Gandalf",
    "MGS 0005 Cañahuate": "Cañahuate",
    "MGS 0006 Perijá": "Perija",
    "MGS 0007 La Paz Vallenata": "La Paz Vallenata",
    "MGS 0008 La Paz Verso": "La Paz Verso",
    "MGS 0009 El Molino": "Molino",
    "MGS 0010 - Villanueva": "Villanueva",
    "MGS 0011 El Roble": "El Roble",
    "MGS 0013 La Mesa": "La Mesa",
    "MGS 0014 - El Olimpo": "El Olimpo",
    "MGS 0016 - Puya": "La Puya",
    "MGS 0017- Esmeralda": "Esmeralda",
    "MGS 0018 La Paz Leyenda": "La Paz Leyenda",
    "MGS 0019 El Merengue": "merengue",
    "Complejo Industrial Cedillanos": "Cedillanos",
    "GRANJA SOLAR SAN AGUSTIN": "San Agustin",
}

_PREFIJO_MGS = re.compile(r"^MGS\s*\d+\s*[-\s]*")
_SUFIJO_DEPARTMENT = re.compile(r"\s+[Dd]epartment$")


def _buscar(clave: str):
    """Exacto primero, y si no, por contiene. En ese orden a propósito."""
    exacto = py_models.Proyecto.objects.filter(nombre_comercial=clave).first()
    if exacto:
        return exacto
    return py_models.Proyecto.objects.filter(
        nombre_comercial__icontains=clave
    ).first()


def _departamento(valor: str | None) -> str:
    return _SUFIJO_DEPARTMENT.sub("", (valor or "").strip()).strip()


def sincronizar(ruta: Path) -> tuple[list[str], list[str]]:
    """Devuelve (nombres actualizados, nombres del JSON que no se encontraron)."""
    filas = json.loads(ruta.read_text(encoding="utf-8"))
    actualizados: list[str] = []
    saltados: list[str] = []

    with transaction.atomic():
        for fila in filas:
            nombre = (fila.get("nombre_topico") or "").strip()
            clave = (
                NOMBRE_A_BUSQUEDA.get(nombre)
                or _PREFIJO_MGS.sub("", nombre).strip()
                or nombre
            )
            proyecto = _buscar(clave)
            if proyecto is None:
                saltados.append(nombre)
                continue
            if _rellenar(proyecto, fila):
                actualizados.append(proyecto.nombre_comercial)

    return actualizados, saltados


def _rellenar(proyecto, fila: dict) -> bool:
    cambios: list[str] = []

    departamento = _departamento(fila.get("departamento"))
    if departamento and not proyecto.departamento:
        proyecto.departamento = departamento
        cambios.append("departamento")

    municipio = (fila.get("ciudad") or "").strip()
    if municipio and not proyecto.municipio:
        proyecto.municipio = municipio
        cambios.append("municipio")

    kwp = fila.get("potencia_instalada_dc_kwp")
    if kwp is not None and not proyecto.potencia_ac_kw:
        proyecto.potencia_ac_kw = kwp
        cambios.append("potencia_ac_kw")

    if cambios:
        proyecto.save(update_fields=cambios)

    paneles = fila.get("numero_de_paneles")
    return bool(cambios) or (
        paneles is not None and _paneles(proyecto, paneles)
    )


def _paneles(proyecto, cantidad: int) -> bool:
    """Crea o rellena la info técnica con el número de paneles."""
    info = py_models.ProyectoInfoTecnica.objects.filter(proyecto=proyecto).first()
    if info is None:
        py_models.ProyectoInfoTecnica.objects.create(
            proyecto=proyecto, cantidad_total_paneles=cantidad
        )
        return True
    if not info.cantidad_total_paneles:
        info.cantidad_total_paneles = cantidad
        info.save(update_fields=["cantidad_total_paneles"])
        return True
    return False
