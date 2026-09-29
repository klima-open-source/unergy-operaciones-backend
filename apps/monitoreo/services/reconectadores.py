"""Relays (reconectadores) de las plantas.

Todo el trato con el proveedor vive acá: la vista solo elige proyectos y
traduce a HTTP. Leer y mandar un comando van por caminos distintos:

- **Leer** el estado sale de SolarView (`api.sole.tech`), con el token del
  servidor y por `project_id_solarview`: `GET /solarview/config/recloser/?project_id=`
  devuelve la medición más reciente con las mismas claves que traía Solenium
  (`active`, `time`, `i_a`… `pf`). Verificado en vivo el 2026-09-29: 25 de las
  39 plantas de SolarView tienen reconectador; las otras responden 404.
- **Mandar un ON/OFF** sigue en Solenium (`data.sole.tech`): exige las
  credenciales del USUARIO en el cuerpo de la petición, se validan en cada
  llamada y NO se guardan. Abrir o cerrar un relay apaga una planta: tiene que
  quedar atribuido a una persona. SolarView todavía no tiene un endpoint
  documentado para el comando; se revisa aparte.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from apps.comun.config import settings

logger = logging.getLogger("operaciones.reconectadores")

# Las URLs del comando salen de la configuracion, no hardcodeadas. Estaban fijas
# en este archivo y cuando Solenium migro de solenium.co a sole.tech
# (2026-09-07) el ON/OFF de los relays quedo roto sin que hubiera forma de
# arreglarlo sin desplegar.
_AUTH = settings.SOLENIUM_AUTH_URL.rstrip("/").removesuffix("/token")
_DATA = settings.SOLENIUM_DATA_URL.rstrip("/")

AUTH_URL = f"{_AUTH}/token/"
RELAY_SET = f"{_DATA}/project/{{sol_id}}/relay/set-status/"

# La lectura: ruta relativa a `SOLARVIEW_BASE_URL`. El parámetro es
# `project_id`, no `recloser` como en el histórico (la documentación no lo dice).
RELAY_ACTUAL = "/solarview/config/recloser/"

# ~175 ms por planta; 8 en paralelo para que la pantalla cargue rápido.
HILOS = 8

# Medida del proveedor -> campo de la respuesta. Son las mismas columnas del
# panel "Reconectadores", y las mismas claves en Solenium y en SolarView.
TELEMETRIA = {
    "corriente_a": "i_a", "corriente_b": "i_b", "corriente_c": "i_c",
    "corriente_n": "i_n",
    "voltaje_a": "u_a", "voltaje_b": "u_b", "voltaje_c": "u_c",
    "voltaje_r": "u_r", "voltaje_s": "u_s", "voltaje_t": "u_t",
    "frecuencia_hz": "f_abc", "reactiva_kva": "kva", "potencia_kw": "kw",
    "factor_potencia": "pf",
}


class SolarViewNoConfigurado(RuntimeError):
    pass


class CredencialesInvalidas(RuntimeError):
    pass


class SoleniumNoResponde(RuntimeError):
    pass


class RespuestaInesperada(RuntimeError):
    pass


_cliente = None


def cliente():
    """El `SolarViewClient` del servidor, creado una vez."""
    global _cliente
    if _cliente is None:
        from app.services.mgs.solarview_client import SolarViewClient

        _cliente = SolarViewClient()
    if not _cliente.enabled:
        raise SolarViewNoConfigurado(
            "SolarView no configurado en el servidor (SOLARVIEW_TOKEN)")
    return _cliente


def url_relay(c) -> str:
    return f"{c._base_url}{RELAY_ACTUAL}"


def _numero(valor) -> float | None:
    """El proveedor a veces manda las medidas como texto o como null."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def leer_relay(sv_id: int) -> tuple[bool, dict]:
    """Devuelve (tiene reconectador, medidas), con el id de SolarView.

    `False` cubre DOS casos que no se pueden distinguir desde acá: SolarView
    respondió 404 (la planta no tiene relay físico) o hubo error/timeout (no se
    pudo confirmar). En ambos el proyecto se omite del listado, porque mostrarlo
    "sin dato" sugeriría que tiene relay y está caído.
    """
    try:
        c = cliente()
        datos = c._get(url_relay(c), params={"project_id": sv_id})
        if not datos:
            return False, {}
        return True, (datos.get("results") or {})
    except Exception as exc:
        logger.warning("relay_get sv_id=%d error=%s", sv_id, exc)
        return False, {}


def build_estado(proyecto_id: int, nombre: str, sol_id: int, medidas: dict) -> dict:
    """Traduce el `results` del proveedor a la forma que consume el móvil.

    `sol_id` es el id de SolarView de la planta. El nombre del campo se
    conserva por compatibilidad; el ON/OFF no lo usa (va por el id del proyecto).
    """
    momento = medidas.get("time")
    estado = {
        "proyecto_id": proyecto_id,
        "nombre": nombre,
        "sol_id": sol_id,
        # True=ON, False=OFF, None=sin dato.
        "active": medidas.get("active"),
        "ultima_actualizacion": str(momento) if momento is not None else None,
    }
    estado.update(
        {campo: _numero(medidas.get(clave)) for campo, clave in TELEMETRIA.items()}
    )
    return estado


def estados_de(proyectos) -> list[dict]:
    """El estado de cada proyecto, consultados en paralelo.

    Los proyectos sin relay o con `project_id_solarview` no numérico se omiten.
    """
    def uno(proyecto):
        try:
            sv_id = int(proyecto.project_id_solarview)
        except (TypeError, ValueError):
            logger.warning(
                "project_id_solarview inválido proyecto_id=%s valor=%r",
                proyecto.id, proyecto.project_id_solarview,
            )
            return None
        tiene, medidas = leer_relay(sv_id)
        if not tiene:
            return None
        return build_estado(
            proyecto.id, proyecto.nombre_comercial, sv_id, medidas
        )

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        return [e for e in pool.map(uno, proyectos) if e is not None]


def token_de_usuario(usuario: str, clave: str) -> str:
    """JWT de Solenium con las credenciales del usuario. No se almacena nada."""
    try:
        with httpx.Client(timeout=15) as http:
            respuesta = http.post(
                AUTH_URL, json={"username": usuario, "password": clave}
            )
    except Exception as exc:
        raise SoleniumNoResponde(f"No se pudo conectar a Solenium: {exc}") from exc

    if respuesta.status_code == 401:
        raise CredencialesInvalidas("Credenciales Solenium incorrectas")
    if respuesta.status_code not in (200, 201):
        raise RespuestaInesperada(f"Solenium auth → HTTP {respuesta.status_code}")

    token = respuesta.json().get("access")
    if not token:
        raise RespuestaInesperada("Solenium no devolvió token")
    return token


def enviar_comando(sol_id: int, accion: str, interrogar: bool, token: str):
    """Manda el ON/OFF al relay. Devuelve la respuesta HTTP de Solenium."""
    try:
        with httpx.Client(timeout=30) as http:
            return http.post(
                RELAY_SET.format(sol_id=sol_id),
                json={"status_to_set": accion, "is_interrogating": interrogar},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        raise SoleniumNoResponde(f"Error de conexión: {exc}") from exc
