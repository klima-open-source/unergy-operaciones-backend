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
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from apps.comun import sole_tech
from apps.comun.config import settings

logger = logging.getLogger("operaciones.reconectadores")

# Ruta relativa a `SOLARVIEW_BASE_URL` (la de lectura vive en el cliente:
# `RUTA_RECLOSER`). El comando SÍ lleva `/api/` delante: sin él el gateway responde 404 "no Route
# matched" (verificado el 2026-09-30). Las lecturas funcionan sin ese prefijo.
RELAY_COMANDO = "/api/solarview/config/recloser/set-status/"

# ~175 ms por planta; 16 en paralelo: con lecturas de máx. 6 s (ver `_leer`),
# ~39 relays tardan en el peor caso ~18 s, no ~30.
HILOS = 16

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
        from apps.comun.integraciones.solarview_client import SolarViewClient

        _cliente = SolarViewClient()
    if not _cliente.enabled:
        raise SolarViewNoConfigurado(
            "SolarView no configurado en el servidor (SOLARVIEW_TOKEN)")
    return _cliente


def _base_url() -> str:
    return settings.SOLARVIEW_BASE_URL.rstrip("/")


def url_relay() -> str:
    """La URL de lectura, para mostrarla en el debug."""
    from apps.comun.integraciones.solarview_client import RUTA_RECLOSER

    return f"{_base_url()}{RUTA_RECLOSER}"


def _numero(valor) -> float | None:
    """El proveedor a veces manda las medidas como texto o como null."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _leer(sv_id: int) -> tuple[str, dict]:
    """(`"ok"` | `"sin_relay"` | `"error"`, medidas), con el id de SolarView.

    `"sin_relay"` es un 404: la planta no tiene reconectador. `"error"` es que
    no se pudo leer (timeout, 5xx): NO quiere decir que no tenga.
    """
    try:
        estado, datos = cliente().get_recloser_con_estado(sv_id)
    except Exception as exc:
        logger.warning("relay_get sv_id=%d error=%s", sv_id, exc)
        return "error", {}
    if estado == "ok" and datos:
        return "ok", (datos.get("results") or {})
    if estado == "no_existe":
        return "sin_relay", {}
    return "error", {}


def leer_relay(sv_id: int) -> tuple[bool, dict]:
    """Devuelve (tiene reconectador, medidas). `False` si no tiene o si falló."""
    estado, medidas = _leer(sv_id)
    return estado == "ok", medidas


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


# Caché SOLO en Redis (no en la memoria de cada proceso, como la de generación):
# SolarView se consulta una vez por minuto para todos, no una vez por persona
# con la página abierta. Sin esto, /estados tardaba ~9 s y ocupaba los procesos
# web, que dejaban esperando al resto de la pantalla (medido el 2026-10-02).
#
# Por qué sin el nivel de proceso: gunicorn corre varios procesos y cada uno
# tendría su copia. Una copia vieja de `ultimo` (vive 24 h) se mostraba como
# lectura actual y, al guardarse, pisaba en Redis las lecturas más nuevas de los
# otros procesos; y `olvidar_estados` tras un ON/OFF solo limpiaba el proceso
# que mandó el comando. Para un relay, ver el estado real importa más que el
# milisegundo que cuesta ir a Redis.
CACHE_TTL_ESTADOS = 60
# Si alguna lectura falló también se guarda un minuto: insistirle más seguido a
# un SolarView que no responde solo suma procesos esperando.
CACHE_TTL_ESTADOS_CON_ERROR = 60
# La última lectura buena de cada relay: si una lectura falla, se muestra esa,
# marcada `lectura_fallida`, en vez de que el reconectador desaparezca. Y la
# última lista completa, para responder mientras otro proceso consulta.
CACHE_TTL_ULTIMO = 24 * 3600
# Claves nuevas a propósito: las de antes (`relays:*`) guardaban otra forma
# (`{"expira", "datos"}`) y no deben leerse con esta.
CLAVE_ESTADOS = "reconectadores:estados"
CLAVE_ULTIMO = "reconectadores:ultimo"
CLAVE_ULTIMA_LISTA = "reconectadores:ultima_lista"
# Solo un proceso a la vez consulta SolarView; los demas devuelven la ultima
# lista al instante en vez de repetir las ~39 llamadas en paralelo.
CLAVE_CANDADO = "solar_monitoreo:relays:actualizando"
CANDADO_TTL = 90


def _redis():
    """El caché de Django: Redis en el servidor. Quien lo usa atrapa el error
    si Redis no responde."""
    from django.core.cache import cache

    return cache


def _leer_cache(clave: str):
    try:
        return _redis().get(clave)
    except Exception:
        return None


def _guardar_cache(clave: str, ttl: int, datos) -> None:
    try:
        _redis().set(clave, datos, ttl)
    except Exception:
        pass  # sin Redis no hay caché: cada consulta va a SolarView


def _tomar_candado() -> bool:
    """True si este proceso debe consultar SolarView. Sin Redis, siempre True
    (cada proceso consulta)."""
    try:
        return bool(_redis().add(CLAVE_CANDADO, 1, CANDADO_TTL))
    except Exception:
        return True


def _soltar_candado() -> None:
    try:
        _redis().delete(CLAVE_CANDADO)
    except Exception:
        pass


def olvidar_estados() -> None:
    """Tras un comando: el próximo /estados vuelve a leer SolarView, en
    cualquier proceso."""
    try:
        _redis().delete(CLAVE_ESTADOS)
    except Exception:
        pass


def _lista_mientras_otro_consulta() -> list[dict]:
    """Lo que se responde cuando otro proceso tiene el candado.

    La última lista completa, con sus marcas. Si es de hace más de dos ciclos
    (nadie abrió la pantalla en un rato), cada relay sale `lectura_fallida`: no
    es una lectura de ahora y no debe verse como tal.
    """
    guardada = _leer_cache(CLAVE_ULTIMA_LISTA)
    if not guardada:
        return []
    if time.time() - guardada["guardada_en"] <= 2 * CACHE_TTL_ESTADOS:
        return guardada["lista"]
    return [{**e, "lectura_fallida": True} for e in guardada["lista"]]


def estados_de(proyectos) -> list[dict]:
    """El estado de cada proyecto, consultados en paralelo y cacheados.

    Se omiten los proyectos sin relay (404) y los de `project_id_solarview` no
    numérico. Si la lectura de uno FALLA, se devuelve su última lectura buena
    con `lectura_fallida: True`; si nunca hubo una, se omite.
    """
    if (cacheado := _leer_cache(CLAVE_ESTADOS)) is not None:
        return cacheado

    if not _tomar_candado():
        return _lista_mientras_otro_consulta()
    try:
        # `ultimo` se lee DESPUÉS de tomar el candado: así es el que dejó el
        # último proceso que consultó, no uno anterior.
        return _consultar(proyectos, _leer_cache(CLAVE_ULTIMO) or {})
    finally:
        _soltar_candado()


def _consultar(proyectos, ultimo: dict) -> list[dict]:

    def uno(proyecto):
        try:
            sv_id = int(proyecto.project_id_solarview)
        except (TypeError, ValueError):
            logger.warning(
                "project_id_solarview inválido proyecto_id=%s valor=%r",
                proyecto.id, proyecto.project_id_solarview,
            )
            return "invalido", proyecto.id, None
        estado, medidas = _leer(sv_id)
        if estado != "ok":
            return estado, proyecto.id, None
        return "ok", proyecto.id, build_estado(
            proyecto.id, proyecto.nombre_comercial, sv_id, medidas
        )

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        resultados = list(pool.map(uno, proyectos))

    lista: list[dict] = []
    nuevo_ultimo = dict(ultimo)
    hubo_error = False
    for tipo, proyecto_id, estado in resultados:
        clave = str(proyecto_id)
        if tipo == "ok":
            estado["lectura_fallida"] = False
            lista.append(estado)
            nuevo_ultimo[clave] = estado
        elif tipo == "error":
            hubo_error = True
            if clave in ultimo:
                lista.append({**ultimo[clave], "lectura_fallida": True})
        else:  # sin_relay o id inválido: ya no tiene relay que recordar
            nuevo_ultimo.pop(clave, None)

    _guardar_cache(CLAVE_ULTIMO, CACHE_TTL_ULTIMO, nuevo_ultimo)
    _guardar_cache(CLAVE_ULTIMA_LISTA, CACHE_TTL_ULTIMO,
                   {"guardada_en": time.time(), "lista": lista})
    _guardar_cache(
        CLAVE_ESTADOS,
        CACHE_TTL_ESTADOS_CON_ERROR if hubo_error else CACHE_TTL_ESTADOS,
        lista,
    )
    return lista


def _env_habilitado() -> bool:
    return settings.RECONECTADORES_COMANDOS_HABILITADOS.strip().lower() == "true"


def _fila_interruptor():
    from apps.monitoreo.models import InterruptorReconectadores

    return InterruptorReconectadores.objects.filter(pk=1).first()


def comandos_habilitados() -> bool:
    """El interruptor del ON/OFF. Apagado salvo que lo enciendan.

    Abrir o cerrar un reconectador energiza o apaga una planta, y puede haber
    gente trabajando en sitio. Se enciende de dos formas, coordinado con el
    equipo de campo:

    - `RECONECTADORES_COMANDOS_HABILITADOS=true` en el `.env` del servidor;
    - o un `admin` desde la plataforma (`POST /reconectadores/interruptor`),
      que queda en la tabla `interruptor_reconectadores` con quién y cuándo.

    Sin la fila, o si la base no responde, cuenta como apagado: ante la duda,
    no sale ningún comando.
    """
    if _env_habilitado():
        return True
    try:
        fila = _fila_interruptor()
    except Exception as exc:
        logger.warning("no se pudo leer el interruptor de reconectadores: %s", exc)
        return False
    return bool(fila and fila.habilitado)


def estado_interruptor() -> dict:
    """Lo que muestra la plataforma: si está encendido y por qué."""
    fila = _fila_interruptor()
    return {
        "habilitado": _env_habilitado() or bool(fila and fila.habilitado),
        # Encendido por el `.env`: desde la plataforma no se puede apagar.
        "forzado_por_servidor": _env_habilitado(),
        "actualizado_por": fila.actualizado_por if fila else None,
        "actualizado_en": fila.actualizado_en if fila else None,
    }


def cambiar_interruptor(habilitado: bool, quien: str) -> dict:
    from django.utils import timezone

    from apps.monitoreo.models import InterruptorReconectadores

    InterruptorReconectadores.objects.update_or_create(
        pk=1,
        defaults={
            "habilitado": habilitado,
            "actualizado_por": quien,
            "actualizado_en": timezone.now(),
        },
    )
    return estado_interruptor()


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


def enviar_comando(sv_id: int, accion: str, usuario: str, contrasena: str) -> httpx.Response:
    """Manda el ON/OFF al reconectador por SolarView, con el token del servidor.

    Los candados van ACÁ y no en la vista, en este orden, para que ningún
    camino que llame a esta función se los salte:

    1. el interruptor (una sola revisión, antes de todo: con los comandos
       apagados la contraseña de nadie sale hacia sole.tech);
    2. la acción, ON u OFF;
    3. el usuario y la contraseña de SolarView de quien lo manda.

    La forma es la que manda la propia plataforma de SolarView al apagar un
    reconectador, capturada en el navegador el 2026-09-30 sobre Valencia Oriente
    (108): cuerpo JSON `{"recloser": <id de SolarView>, "command": "OFF"}`. El
    id NO va en la URL: con `?recloser=&command=OFF` SolarView respondió 500
    `DoesNotExist`.
    """
    if not comandos_habilitados():
        raise ComandosDeshabilitados(
            "Los comandos ON/OFF están deshabilitados en este servidor "
            "(un admin los enciende en Generación Solar)."
        )
    if accion not in ("ON", "OFF"):
        raise ValueError(f"acción inválida: {accion!r}")
    verificar_credenciales(usuario, contrasena)

    if not sole_tech.configurado():
        raise SolarViewNoConfigurado(
            "SolarView no configurado en el servidor (SOLARVIEW_TOKEN)")
    try:
        with httpx.Client(timeout=30) as http:
            return http.post(
                f"{_base_url()}{RELAY_COMANDO}",
                json={"recloser": sv_id, "command": accion},
                headers=sole_tech.cabeceras(),
            )
    except Exception as exc:
        raise SolarViewNoResponde(f"Error de conexión con SolarView: {exc}") from exc
