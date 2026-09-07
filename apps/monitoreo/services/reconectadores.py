"""Relays (reconectadores) de las plantas, vía Solenium.

Todo el trato con Solenium vive acá: la vista solo elige proyectos y traduce a
HTTP. Dos rutas de credenciales, y la diferencia importa:

- **Leer** el estado usa las credenciales del SERVIDOR (`SoleniumClient`): la
  pantalla debe cargar sin pedirle nada al usuario.
- **Mandar un ON/OFF** exige las credenciales del USUARIO en el cuerpo de la
  petición, se validan contra Solenium en cada llamada y NO se guardan. Abrir o
  cerrar un relay apaga una planta: tiene que quedar atribuido a una persona.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from apps.comun.config import settings

logger = logging.getLogger("operaciones.reconectadores")

# Las URLs salen de la configuracion, no hardcodeadas. Estaban fijas en este
# archivo y cuando Solenium migro de solenium.co a sole.tech (2026-09-07) el
# ON/OFF de los relays quedo roto sin que hubiera forma de arreglarlo sin
# desplegar. Ahora es el mismo `SOLENIUM_*` que usa `SoleniumClient` para leer,
# asi que las dos rutas de credenciales apuntan siempre al mismo servidor -- que
# apuntaran a hosts distintos era un bug esperando.
_AUTH = settings.SOLENIUM_AUTH_URL.rstrip("/").removesuffix("/token")
_DATA = settings.SOLENIUM_DATA_URL.rstrip("/")

AUTH_URL = f"{_AUTH}/token/"
RELAY_SET = f"{_DATA}/project/{{sol_id}}/relay/set-status/"
RELAY_GET = f"{_DATA}/project/{{sol_id}}/relay/"

# Solenium tarda; 8 en paralelo es lo que hace que la pantalla cargue.
HILOS = 8

# Medida de Solenium -> campo de la respuesta. Son las mismas columnas del panel
# "Reconectadores" de Solenium.
TELEMETRIA = {
    "corriente_a": "i_a", "corriente_b": "i_b", "corriente_c": "i_c",
    "corriente_n": "i_n",
    "voltaje_a": "u_a", "voltaje_b": "u_b", "voltaje_c": "u_c",
    "voltaje_r": "u_r", "voltaje_s": "u_s", "voltaje_t": "u_t",
    "frecuencia_hz": "f_abc", "reactiva_kva": "kva", "potencia_kw": "kw",
    "factor_potencia": "pf",
}


class SoleniumNoConfigurado(RuntimeError):
    pass


class CredencialesInvalidas(RuntimeError):
    pass


class SoleniumNoResponde(RuntimeError):
    pass


class RespuestaInesperada(RuntimeError):
    pass


_cliente = None


def cliente():
    """El `SoleniumClient` del servidor, creado una vez."""
    global _cliente
    if _cliente is None:
        from app.services.mgs.solenium_client import SoleniumClient

        _cliente = SoleniumClient()
    if not _cliente.enabled:
        raise SoleniumNoConfigurado("Solenium no configurado en el servidor")
    return _cliente


def _numero(valor) -> float | None:
    """Solenium a veces manda las medidas como texto o como null."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def leer_relay(sol_id: int) -> tuple[bool, dict]:
    """Devuelve (tiene reconectador, medidas).

    `False` cubre DOS casos que no se pueden distinguir desde acá: Solenium
    respondió 404 (la planta no tiene relay físico) o hubo error/timeout (no se
    pudo confirmar). En ambos el proyecto se omite del listado, porque mostrarlo
    "sin dato" sugeriría que tiene relay y está caído.
    """
    try:
        datos = cliente()._get(RELAY_GET.format(sol_id=sol_id))
        if not datos:
            return False, {}
        return True, (datos.get("results") or {})
    except Exception as exc:
        logger.warning("relay_get sol_id=%d error=%s", sol_id, exc)
        return False, {}


def build_estado(proyecto_id: int, nombre: str, sol_id: int, medidas: dict) -> dict:
    """Traduce el `results` de Solenium a la forma que consume el móvil."""
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

    Los proyectos sin relay o con `project_id_solenium` no numérico se omiten.
    """
    def uno(proyecto):
        try:
            sol_id = int(proyecto.project_id_solenium)
        except (TypeError, ValueError):
            logger.warning(
                "project_id_solenium inválido proyecto_id=%s valor=%r",
                proyecto.id, proyecto.project_id_solenium,
            )
            return None
        tiene, medidas = leer_relay(sol_id)
        if not tiene:
            return None
        return build_estado(
            proyecto.id, proyecto.nombre_comercial, sol_id, medidas
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
