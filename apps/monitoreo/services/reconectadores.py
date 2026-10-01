"""Relays (reconectadores) de las plantas.

Todo el trato con el proveedor vive acá: la vista solo elige proyectos y
traduce a HTTP. Leer y mandar un comando van por caminos distintos:

- **Leer** el estado sale de SolarView (`api.sole.tech`), con el token del
  servidor y por `project_id_solarview`: `GET /solarview/config/recloser/?project_id=`
  devuelve la medición más reciente con las mismas claves que traía Solenium
  (`active`, `time`, `i_a`… `pf`). Verificado en vivo el 2026-09-29: 25 de las
  39 plantas de SolarView tienen reconectador; las otras responden 404.
- **Mandar un ON/OFF** también va a SolarView con el token del servidor
  (`POST /solarview/config/recloser/set-status/`), ya no a Solenium con las
  credenciales de la persona. Abrir o cerrar un relay energiza o apaga una
  planta y puede haber gente en sitio, así que tiene tres candados: el
  interruptor `RECONECTADORES_COMANDOS_HABILITADOS` (apagado por defecto), el
  rol (`admin` u `operaciones`, en la vista), el usuario y la contraseña de
  sole.tech de quien lo manda (verificados contra `auth.sole.tech`, como pide
  SolarView antes de abrir o cerrar un relay) y el registro en el log de quién,
  qué planta y qué acción. Durante el desarrollo ese endpoint NO se llama nunca,
  ni de prueba.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

from apps.comun.config import settings

logger = logging.getLogger("operaciones.reconectadores")

# Rutas relativas a `SOLARVIEW_BASE_URL`. La lectura usa `project_id`, no
# `recloser` como el histórico (la documentación no lo dice).
RELAY_ACTUAL = "/solarview/config/recloser/"
# El comando SÍ lleva `/api/` delante: sin él el gateway responde 404 "no Route
# matched" (verificado el 2026-09-30). Las lecturas funcionan sin ese prefijo.
RELAY_COMANDO = "/api/solarview/config/recloser/set-status/"

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


class SolarViewNoResponde(RuntimeError):
    pass


class ComandosDeshabilitados(RuntimeError):
    pass


class CredencialesInvalidas(ValueError):
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


def comandos_habilitados() -> bool:
    """El interruptor del ON/OFF. Apagado salvo `RECONECTADORES_COMANDOS_HABILITADOS=true`.

    Abrir o cerrar un reconectador energiza o apaga una planta, y puede haber
    gente trabajando en sitio. Se enciende solo en el servidor, coordinado con
    el equipo de campo; en local no existe, así que desde ahí es imposible
    mandar un comando.
    """
    return settings.RECONECTADORES_COMANDOS_HABILITADOS.strip().lower() == "true"


def verificar_credenciales(usuario: str, contrasena: str) -> None:
    """Confirma el usuario y la contraseña de sole.tech de quien manda el comando.

    Es el mismo login que usa SolarView (`auth.sole.tech/api/token/`). El token
    que devuelve no se usa ni se guarda: el comando sigue saliendo con el token
    del servidor. Lo que se busca es que el ON/OFF lo confirme una persona con
    cuenta en SolarView, y que su usuario quede en el log.
    """
    url = f"{settings.SOLENIUM_AUTH_URL.rstrip('/')}/token/"
    try:
        with httpx.Client(timeout=15) as http:
            respuesta = http.post(url, json={"username": usuario, "password": contrasena})
    except Exception as exc:
        raise SolarViewNoResponde(f"No se pudo conectar con sole.tech: {exc}") from exc

    if respuesta.status_code in (400, 401):
        raise CredencialesInvalidas("Usuario o contraseña de SolarView incorrectos.")
    if respuesta.status_code not in (200, 201) or "access" not in (respuesta.json() or {}):
        raise SolarViewNoResponde(f"sole.tech respondió HTTP {respuesta.status_code} al login.")


def enviar_comando(sv_id: int, accion: str) -> httpx.Response:
    """Manda el ON/OFF al reconectador por SolarView, con el token del servidor.

    Revisa el interruptor ACÁ y no solo en la vista: así ningún camino que
    llame a esta función puede mandar un comando con el interruptor apagado.

    La forma es la que manda la propia plataforma de SolarView al apagar un
    reconectador, capturada en el navegador el 2026-09-30 sobre Valencia Oriente
    (108): cuerpo JSON `{"recloser": <id de SolarView>, "command": "OFF"}`. El
    id NO va en la URL: con `?recloser=&command=OFF` SolarView respondió 500
    `DoesNotExist`.
    """
    if not comandos_habilitados():
        raise ComandosDeshabilitados(
            "Los comandos ON/OFF están deshabilitados en este servidor "
            "(RECONECTADORES_COMANDOS_HABILITADOS)."
        )
    if accion not in ("ON", "OFF"):
        raise ValueError(f"acción inválida: {accion!r}")

    c = cliente()
    try:
        with httpx.Client(timeout=30) as http:
            return http.post(
                f"{c._base_url}{RELAY_COMANDO}",
                json={"recloser": sv_id, "command": accion},
                headers=c._headers(),
            )
    except Exception as exc:
        raise SolarViewNoResponde(f"Error de conexión con SolarView: {exc}") from exc
