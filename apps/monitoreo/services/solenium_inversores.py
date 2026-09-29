"""Inversores de una planta desde Solenium, con su token en caché.

`ponytail: caché de token en un dict de módulo`. El token vale ~20 h y con
`WORKERS=1` un solo proceso lo comparte. Al subir workers cada uno pedirá el
suyo — funciona igual, solo son más llamadas de auth.
"""

import logging
import os
import time

import httpx

logger = logging.getLogger("operaciones.monitoreo")

TTL_TOKEN = 20 * 3600
# Cuántas palabras del nombre tienen que coincidir para dar por bueno el match.
UMBRAL_MATCH = 0.5

_token: dict = {"valor": None, "expira": 0.0}


def _env(nombre: str) -> str:
    return os.environ.get(nombre, "")


def token() -> str | None:
    ahora = time.time()
    if _token["valor"] and ahora < _token["expira"]:
        return _token["valor"]
    if not _env("SOLENIUM_USER") or not _env("SOLENIUM_PASS"):
        return None
    try:
        with httpx.Client(timeout=30) as http:
            respuesta = http.post(
                _env("SOLENIUM_AUTH_URL"),
                json={
                    "username": _env("SOLENIUM_USER"),
                    "password": _env("SOLENIUM_PASS"),
                },
            )
        if respuesta.status_code != 200:
            return None
        datos = respuesta.json()
    except Exception:
        logger.debug("auth de Solenium falló", exc_info=True)
        return None

    valor = datos.get("access") or datos.get("token") or datos.get("key") or ""
    if valor:
        _token["valor"], _token["expira"] = valor, ahora + TTL_TOKEN
    return valor or None


def _invalidar():
    _token["valor"] = None


def inversores(proyecto) -> tuple[list, str | None]:
    """(inversores, error). Nunca levanta: el error viaja como texto."""
    tok = token()
    if not tok:
        return [], "Solenium no configurado o sin credenciales"

    cabeceras = {"Authorization": f"Bearer {tok}"}
    try:
        with httpx.Client(timeout=30) as http:
            listado = http.get(
                f'{_env("SOLENIUM_DATA_URL")}/project/',
                params={"menu": "1"}, headers=cabeceras,
            )
            if listado.status_code == 401:
                _invalidar()
                return [], "Solenium: sesión expirada"

            sol_id = proyecto.project_id_solenium or ""
            if not sol_id:
                sol_id = _adivinar_id(proyecto, _lista(listado))
            if not sol_id:
                return [], (
                    "No se encontró el proyecto en Solenium (configura "
                    "project_id_solenium en el proyecto)"
                )

            detalle = http.get(
                f'{_env("SOLENIUM_DATA_URL")}/project/{sol_id}/inverter/',
                headers=cabeceras,
            )
            # El token se invalida TAMBIÉN acá: si solo se limpiaba en la
            # primera llamada, un 401 en /inverter/ dejaba el token vencido en
            # caché hasta 20 h y todos los sondeos seguían fallando.
            if detalle.status_code == 401:
                _invalidar()
                return [], "Solenium: sesión expirada"
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
