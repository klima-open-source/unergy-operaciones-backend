"""Inversores de una planta desde Solenium (`data.sole.tech`).

Se autentica con el token de sole.tech (`apps.comun.sole_tech`). Hasta el
2026-09-29 hacía su propio login con usuario y contraseña y guardaba el JWT
20 h en un dict de módulo; ese login ya no funciona.
"""

import logging

import httpx

from apps.comun import sole_tech
from apps.comun.config import settings

logger = logging.getLogger("operaciones.monitoreo")

# Cuántas palabras del nombre tienen que coincidir para dar por bueno el match.
UMBRAL_MATCH = 0.5


def inversores(proyecto) -> tuple[list, str | None]:
    """(inversores, error). Nunca levanta: el error viaja como texto."""
    if not sole_tech.configurado():
        return [], "Solenium no configurado: falta el token de sole.tech"

    datos = settings.SOLENIUM_DATA_URL.rstrip("/")
    try:
        with httpx.Client(timeout=30, headers=sole_tech.cabeceras()) as http:
            listado = http.get(f"{datos}/project/", params={"menu": "1"})
            if listado.status_code == 401:
                return [], "Solenium: el token de sole.tech fue rechazado"

            sol_id = proyecto.project_id_solenium or ""
            if not sol_id:
                sol_id = _adivinar_id(proyecto, _lista(listado))
            if not sol_id:
                return [], (
                    "No se encontró el proyecto en Solenium (configura "
                    "project_id_solenium en el proyecto)"
                )

            detalle = http.get(f"{datos}/project/{sol_id}/inverter/")
            if detalle.status_code == 401:
                return [], "Solenium: el token de sole.tech fue rechazado"
            if detalle.status_code != 200:
                return [], f"Solenium inversores HTTP {detalle.status_code}"

            cuerpo = detalle.json()
    except Exception as exc:
        return [], str(exc)

    if isinstance(cuerpo, list):
        return cuerpo, None
    return cuerpo.get("results", cuerpo.get("inverters", [])), None


def _lista(respuesta) -> list:
    if respuesta.status_code != 200:
        return []
    cuerpo = respuesta.json()
    return cuerpo if isinstance(cuerpo, list) else cuerpo.get("results", [])


def _adivinar_id(proyecto, proyectos_solenium: list) -> str:
    """Empareja por nombre cuando el proyecto no tiene `project_id_solenium`.

    Cuenta cuántas palabras de más de dos letras del nombre nuestro aparecen en
    el de Solenium; con la mitad o más, se acepta. Es tosco a propósito: el
    camino bueno es configurar el id, y esto solo evita que la pantalla quede
    vacía mientras alguien lo hace.
    """
    candidatos = [
        (proyecto.nombre_comercial or "").lower(),
        (proyecto.sub_project or "").lower(),
    ]
    for remoto in proyectos_solenium:
        nombre_remoto = (remoto.get("name") or remoto.get("nombre") or "").lower()
        for candidato in candidatos:
            palabras = [p for p in candidato.split() if len(p) > 2]
            if not palabras:
                continue
            aciertos = sum(1 for p in palabras if p in nombre_remoto)
            if aciertos / len(palabras) >= UMBRAL_MATCH:
                return str(remoto.get("id") or "")
    return ""
